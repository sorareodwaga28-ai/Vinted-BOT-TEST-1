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
MAX_SEEN = 3000
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


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return None


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    urls = [u for u in os.environ.get("SEARCH_URLS", "").splitlines() if u.strip()]
    exclude = [w.strip() for w in (os.environ.get("EXCLUDE") or "blazer").split(",") if w.strip()]
    if not webhook or not urls:
        sys.exit("Il manque DISCORD_WEBHOOK ou SEARCH_URLS.")

    seen_list = load_seen()
    first_run = seen_list is None
    seen = set(seen_list or [])
    new_ids, sent, skipped = [], 0, 0
    session = get_session()

    for url in urls:
        base, purl = page_url(url)
        items = fetch_items(session, base, purl)
        print(f"{len(items)} annonces lues pour : {url[:80]}")
        for item in reversed(items):
            iid = str(item.get("id"))
            if iid in seen:
                continue
            seen.add(iid)
            new_ids.append(iid)
            if first_run:
                continue
            if is_excluded(item, exclude):
                skipped += 1
                continue
            send_discord(webhook, item)
            sent += 1

    if first_run:
        print(f"Premier lancement : {len(new_ids)} annonces mémorisées, rien envoyé.")
        requests.post(webhook, json={"content": "✅ Bot Vinted connecté. Les nouvelles annonces arriveront ici."}, timeout=20)
    else:
        print(f"Envoyées : {sent} | exclues : {skipped}")

    all_seen = (seen_list or []) + new_ids
    SEEN_FILE.write_text(json.dumps(all_seen[-MAX_SEEN:]))


if __name__ == "__main__":
    main()
