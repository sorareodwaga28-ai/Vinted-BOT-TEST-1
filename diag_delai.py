"""11/11 : diagnostic — où trouver l'heure de publication d'une annonce Vinted, et combien de temps
Vinted met à la montrer dans la recherche. Lancé à la main (workflow « Diag délai »), ne touche à rien."""
import json
import re
import time

import requests

import bot

s = bot.get_session()
r = s.get("https://www.vinted.fr/", timeout=30)
print("accueil", r.status_code, "cookies:", sorted(s.cookies.keys()))

def essai(nom, url, **kw):
    t0 = time.time()
    try:
        x = s.get(url, timeout=30, **kw)
        print(f"\n=== {nom} : HTTP {x.status_code} en {time.time() - t0:.2f} s, {len(x.text)} car.")
        return x
    except Exception as e:
        print(f"\n=== {nom} : erreur {e}")

api_h = {"Accept": "application/json, text/plain, */*"}
# 1. API catalogue (Jordan + Nike, plus récentes)
x = essai("api catalogue", "https://www.vinted.fr/api/v2/catalog/items?page=1&per_page=20&order=newest_first"
          "&brand_ids[]=53&catalog_ids[]=1242", headers=api_h)
items = []
if x is not None and x.ok:
    try:
        items = x.json().get("items", [])
    except ValueError:
        print(x.text[:500])
now = time.time()
if items:
    print(json.dumps(items[0], ensure_ascii=False, indent=1)[:4000])
    for it in items[:20]:
        ph = (it.get("photo") or {})
        ts = (ph.get("high_resolution") or {}).get("timestamp")
        print(f"id {it['id']} · photo high_resolution.timestamp={ts} · il y a "
              f"{(now - ts) if ts else '?'} s · {it.get('title')}")
    iid = items[0]["id"]
    # 2. API détail d'une annonce
    for u in (f"https://www.vinted.fr/api/v2/items/{iid}", f"https://www.vinted.fr/api/v2/items/{iid}/details"):
        y = essai("api annonce " + u.rsplit("/", 1)[-1], u, headers=api_h)
        if y is not None:
            t = y.text
            for k in ("created_at_ts", "created_at", "updated_at_ts", "last_push_up_at", "timestamp"):
                m = re.search(r'"' + k + r'"\s*:\s*"?([^",}]+)', t)
                if m:
                    print(f"  {k} = {m.group(1)}")
            if not y.ok:
                print(t[:300])

# 3. Page HTML recherche actuelle (pour comparer la vitesse)
essai("page html recherche", "https://www.vinted.fr/catalog?catalog[]=1242&brand_ids[]=53&order=newest_first")
