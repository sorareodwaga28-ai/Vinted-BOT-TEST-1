import json
import os
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse, parse_qsl

import requests

SEEN_FILE = Path(__file__).with_name("seen.json")
MAX_SEEN = 3000
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def url_to_api(search_url):
    u = urlparse(search_url.strip())
    params = []
    for k, v in parse_qsl(u.query, keep_blank_values=False):
        key = k.replace("[]", "")
        if key in ("time", "search_id", "page", "disabled_personalization", "order"):
            continue
        if key == "catalog":
            key = "catalog_ids"
        params.append((key, v))
    return f"{u.scheme}://{u.netloc}", params


def get_session(base):
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
    r = s.get(base + "/", timeout=20)
    print(f"[diag] page d'accueil : HTTP {r.status_code} | cookies : {sorted(s.cookies.keys())}")
    s.headers.update({"Accept": "application/json, text/plain, */*",
                      "Referer": base + "/catalog", "Origin": base})
    return s


def fetch_items(session, base, params):
    multi = {}
    for k, v in params:
        multi.setdefault(k, []).append(v)
    extra = [("order", "newest_first"), ("per_page", "48"), ("page", "1")]
    variantes = [
        [((k + "[]") if len(vs) > 1 or k.endswith("_ids") else k, v)
         for k, vs in multi.items() for v in vs] + extra,
        [(k, ",".join(vs)) for k, vs in multi.items()] + extra,
        [(k, v) for k, v in params if k == "search_text"] + extra,
    ]
    for i, p in enumerate(variantes, 1):
        r = session.get(base + "/api/v2/catalog/items", params=p, timeout=20)
        print(f"[diag] essai {i} : HTTP {r.status_code} | {r.text[:200]!r}")
        if r.status_code == 200:
            if i == 3:
                print("[diag] ATTENTION : seule la recherche texte marche, filtres ignorés")
            return r.json().get("items", [])
    raise RuntimeError(f"Vinted refuse toutes les variantes (dernier code HTTP {r.status_code})")


def price_of(item):
    p = item.get("price")
    if isinstance(p, dict):
        return f"{p.get('amount', '?')} {p.get('currency_code', '€')}"
    return f"{p} {item.get('currency', '€')}"


def is_excluded(item, words):
    t = norm(item.get("title"))
    return any(norm(w) in t for w in words if w)


def send_discord(webhook, item):
    photo = (item.get("photo") or {}).get("url")
    embed = {
        "title": item.get("title", "Annonce")[:250],
        "url": item.get("url"),
        "color": 0x007782,
        "fields": [
            {"name": "Prix", "value": price_of(item), "inline": True},
            {"name": "Taille", "value": item.get("size_title") or "?", "inline": True},
            {"name": "Marque", "value": item.get("brand_title") or "?", "inline": True},
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
    sessions = {}

    for url in urls:
        base, params = url_to_api(url)
        if base not in sessions:
            sessions[base] = get_session(base)
        items = fetch_items(sessions[base], base, params)
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
