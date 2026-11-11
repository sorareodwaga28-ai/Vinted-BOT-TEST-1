"""11/11 : diagnostic — peut-on alléger la page de recherche Vinted ? Lancé à la main."""
import time
import bot
s = bot.get_session()
s.get("https://www.vinted.fr/", timeout=30)
base = ("https://www.vinted.fr/catalog?catalog[]=1242&catalog[]=2632&brand_ids[]=53&order=newest_first"
        "&status_ids[]=6&status_ids[]=1&status_ids[]=2&status_ids[]=3&status_ids[]=4&price_to=70")
for suffixe in ("", "&per_page=20", "&per_page=10", "&per_page=24&page=1"):
    for essai in range(2):
        t0 = time.time()
        r = s.get(base + suffixe, timeout=30, headers={"Accept-Encoding": "gzip, br"})
        t = time.time() - t0
        items = bot.extract_items(r.text) or []
        print(f"{suffixe or '(normal)':22} HTTP {r.status_code} {t:.2f} s  {len(r.content)//1024} Ko reçus  "
              f"{len(r.text)//1024} Ko page  {len(items)} annonces  enc={r.headers.get('content-encoding')}")
