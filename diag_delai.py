"""11/11 : diagnostic — heure de publication Vinted et chemins d'API. Lancé à la main, ne touche à rien."""
import re, time, json
import bot

s = bot.get_session()
s.get("https://www.vinted.fr/", timeout=30)
tok = s.cookies.get("access_token_web")
H = {"Accept": "application/json, text/plain, */*"}
if tok:
    H2 = dict(H, Authorization="Bearer " + tok)

def essai(nom, url, h=H):
    t0 = time.time()
    x = s.get(url, timeout=30, headers=h)
    print(f"\n=== {nom} : HTTP {x.status_code} en {time.time()-t0:.2f} s, {len(x.text)} car. -> {x.text[:250]!r}")
    return x

# Page HTML (celle du bot) : y a-t-il une heure quelque part ?
t0 = time.time()
html = s.get("https://www.vinted.fr/catalog?catalog[]=1242&brand_ids[]=53&order=newest_first", timeout=30).text
print("html", round(time.time()-t0, 2), "s")
for k in ("high_resolution", "timestamp", "created_at", "createdAt", "photo_uploaded", "uploaded"):
    print(k, [m.start() for m in re.finditer(k, html)][:5])
items = bot.extract_items(html) or []
iid = str((items[0].get("productItem") or items[0])["id"]) if items else "0"
print("1re annonce", iid)

q = "page=1&per_page=20&order=newest_first&catalog_ids=1242&brand_ids=53"
for u in (f"/api/v2/catalog/items?{q}", f"/web/api/core/catalog/items?{q}", f"/api/v2/items?{q}",
          f"/api/v2/items/{iid}", f"/api/v2/items/{iid}/details", f"/web/api/core/items/{iid}",
          f"/api/v2/item_upload/items/{iid}", f"/web/api/core/items/{iid}/details"):
    for h in ([H, H2] if tok else [H]):
        x = essai(u + (" +bearer" if h is not H else ""), "https://www.vinted.fr" + u, h)
        if x.ok and x.headers.get("content-type", "").startswith("application/json"):
            t = x.text
            for k in ("timestamp", "created_at_ts", "created_at", "updated_at_ts", "last_push_up_at"):
                v = re.findall(r'"' + k + r'"\s*:\s*"?([^",}]+)', t)[:3]
                if v:
                    print(f"   {k} = {v}  (maintenant {int(time.time())})")
            break
