import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode

import requests

SEEN_FILE = Path(__file__).with_name("seen.json")
MAX_SEEN = 20000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def page_url(search_url):
    u = urlparse(search_url.strip())
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False)
         if k not in ("time", "search_id", "page", "order")]
    q.append(("order", "newest_first"))
    return f"{u.scheme}://{u.netloc}", f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


def get_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                      "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
    return s


def extract_items(html):
    full = ""
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            arr = json.loads(m.group(1))
            if len(arr) > 1 and isinstance(arr[1], str):
                full += arr[1]
        except ValueError:
            pass
    key = '"items":{"items":['
    p = full.find(key)
    if p < 0:
        return None
    arr, _ = json.JSONDecoder().raw_decode(full, p + len(key) - 1)
    return arr


def fetch_items(session, base, url):
    r = session.get(url, timeout=30)
    print(f"[diag] page recherche : HTTP {r.status_code} | {len(r.text)} caractères")
    if r.status_code in (401, 403):
        raise PermissionError(f"Vinted a refusé l'accès (HTTP {r.status_code})")
    r.raise_for_status()
    raw = extract_items(r.text)
    if raw is None:
        raise RuntimeError("Annonces introuvables dans la page (Vinted a peut-être changé son site)")
    items = []
    for x in raw:
        p = x.get("productItem") or x
        box = p.get("itemBox") or {}
        photos = p.get("photos") or []
        price = p.get("price") or {}
        items.append({
            "id": p.get("id") or x.get("id"),
            "title": p.get("title") or "",
            "url": base + (p.get("url") or ""),
            "price": f"{price.get('amount', '?')} {price.get('currencyCode', 'EUR')}",
            "brand": box.get("firstLine") or "?",
            "details": box.get("secondLine") or "?",
            "photo": (photos[0].get("url") if photos else None) or p.get("thumbnailUrl"),
        })
    return items


def is_excluded(item, words):
    t = norm(item.get("title"))
    return any(norm(w) in t for w in words if w)


def send_discord(webhook, item):
    photo = item.get("photo")
    embed = {
        "title": item.get("title", "Annonce")[:250],
        "url": item.get("url"),
        "color": 0x007782,
        "author": {"name": item.get("search", "Vinted")},
        "fields": [
            {"name": "Prix", "value": item["price"], "inline": True},
            {"name": "Taille · état", "value": item["details"], "inline": True},
            {"name": "Marque", "value": item["brand"], "inline": True},
        ],
    }
    if photo:
        embed["image"] = {"url": photo}
    r = requests.post(webhook, json={"embeds": [embed]}, timeout=20)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        requests.post(webhook, json={"embeds": [embed]}, timeout=20)
    time.sleep(1)


def parse_searches(raw):
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("http"):
            parts = ["", line]
        else:
            parts = [p.strip() for p in line.split("|")]
        name = parts[0] or f"Recherche {len(out) + 1}"
        url = parts[1] if len(parts) > 1 else ""
        words = [w.strip() for w in (parts[2] if len(parts) > 2 else "").split(",") if w.strip()]
        required = [w for w in words if not w.startswith("-")]
        excluded = [w[1:].strip() for w in words if w.startswith("-") and w[1:].strip()]
        out.append((name, url, required, excluded))
    return out


def load_state():
    if not SEEN_FILE.exists():
        return None
    data = json.loads(SEEN_FILE.read_text())
    if isinstance(data, list):
        return {"ids": data, "searches": None}
    return data


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    searches = parse_searches(os.environ.get("SEARCH_URLS", ""))
    exclude = [w.strip() for w in (os.environ.get("EXCLUDE") or "blazer").split(",") if w.strip()]
    if not webhook or not searches:
        sys.exit("Il manque DISCORD_WEBHOOK ou SEARCH_URLS.")

    state = load_state()
    first_run = state is None
    state = state or {"ids": [], "searches": []}
    if state.get("searches") is None:
        state["searches"] = [page_url(u)[1] for _, u, _, _ in searches]
    seen = set(state["ids"])
    known = set(state["searches"])
    new_ids, sent, skipped, errors = [], 0, 0, 0
    session = get_session()

    for idx, (name, url, required, excl_here) in enumerate(searches):
        if idx:
            time.sleep(3)
        base, purl = page_url(url)
        new_search = purl not in known
        try:
            items = fetch_items(session, base, purl)
        except Exception as e:
            errors += 1
            print(f"ERREUR [{name}] : {e}")
            continue
        print(f"[{name}] {len(items)} annonces lues" + (" (nouvelle recherche : mémorisées sans envoi)" if new_search else ""))
        for item in reversed(items):
            iid = str(item.get("id"))
            if iid in seen:
                continue
            seen.add(iid)
            new_ids.append(iid)
            if first_run or new_search:
                continue
            if is_excluded(item, exclude + excl_here):
                skipped += 1
                continue
            if required and not is_excluded(item, required):
                skipped += 1
                continue
            item["search"] = name
            send_discord(webhook, item)
            sent += 1
        known.add(purl)
        if new_search and not first_run:
            requests.post(webhook, json={"content": f"🆕 Nouvelle recherche ajoutée : **{name}**"}, timeout=20)

    if first_run:
        print(f"Premier lancement : {len(new_ids)} annonces mémorisées, rien envoyé.")
        requests.post(webhook, json={"content": "✅ Bot Vinted connecté. Les nouvelles annonces arriveront ici."}, timeout=20)
    print(f"Envoyées : {sent} | exclues : {skipped} | recherches en erreur : {errors}")

    state["ids"] = (state["ids"] + new_ids)[-MAX_SEEN:]
    state["searches"] = sorted(known)
    SEEN_FILE.write_text(json.dumps(state))
    if errors == len(searches):
        sys.exit("Toutes les recherches ont échoué.")


if __name__ == "__main__":
    main()
