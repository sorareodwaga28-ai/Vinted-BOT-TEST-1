"""11/11 : diagnostic — heure de publication Vinted. Lancé à la main, ne touche à rien."""
import re, time
import bot

s = bot.get_session()
s.get("https://www.vinted.fr/", timeout=30)
html = s.get("https://www.vinted.fr/catalog?catalog[]=1242&brand_ids[]=53&order=newest_first", timeout=30).text
for m in list(re.finditer("uploaded|Ajout|il y a", html))[:6]:
    print("RECHERCHE", repr(html[max(0, m.start()-200):m.start()+200]))
items = bot.extract_items(html) or []
for x in items[:3]:
    p = x.get("productItem") or x
    url = "https://www.vinted.fr" + p["url"]
    t0 = time.time()
    page = s.get(url, timeout=30).text
    print("\nANNONCE", p["id"], round(time.time()-t0, 2), "s", len(page))
    for m in list(re.finditer(r"uploaded|upload_date|Ajouté|il y a|created|seconde|minute", page))[:12]:
        print("  ", repr(page[max(0, m.start()-150):m.start()+150]))
