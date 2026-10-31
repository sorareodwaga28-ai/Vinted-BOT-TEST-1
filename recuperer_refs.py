"""Télécharge toutes les photos d'annonces Vinted (liens envoyés par Bastien) dans a_trier/<slug>/.
Claude les regarde ensuite et range les bonnes dans references/<slug>/ (+ etiquette/, boite/).

Variables : SLUG (dossier), LIENS (liens Vinted séparés par des espaces, virgules ou retours à la ligne).
"""
import os
import re
import sys
import time
import types
from pathlib import Path

# photo_test importe CLIP et torch : inutiles ici, on les remplace par des modules vides (installation rapide)
for m in ("open_clip", "torch"):
    sys.modules.setdefault(m, types.ModuleType(m))

import bot           # noqa: E402
import photo_test as pt   # noqa: E402

slug = re.sub(r"[^a-z0-9-]+", "-", os.environ.get("SLUG", "").lower()).strip("-")
liens = [l for l in re.split(r"[\s,]+", os.environ.get("LIENS", "")) if "vinted." in l]
if not slug or not liens:
    sys.exit("SLUG et LIENS obligatoires")

dossier = Path("a_trier") / slug
dossier.mkdir(parents=True, exist_ok=True)
session = bot.get_session()
total = 0
for lien in liens:
    m = re.search(r"/items/(\d+)", lien)
    if not m:
        print(f"::warning::lien sans numéro d'annonce : {lien}")
        continue
    iid = m.group(1)
    url = lien.split("?")[0]
    try:
        page = session.get(url, timeout=30).text
    except Exception as ex:
        print(f"::warning::annonce {iid} illisible : {ex}")
        continue
    og = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
    item = {"id": iid, "url": url, "photo": og.group(1).replace("&amp;", "&") if og else ""}
    urls = pt.photos_annonce(session, item) or ([item["photo"]] if item["photo"] else [])
    print(f"annonce {iid} : {len(urls)} photo(s)")
    for n, u in enumerate(urls, 1):
        try:
            r = session.get(u, timeout=30)
            if r.status_code == 200 and len(r.content) > 2000:
                (dossier / f"{iid}-{n:02d}.jpg").write_bytes(r.content)
                total += 1
        except Exception as ex:
            print(f"::warning::photo {u} : {ex}")
        time.sleep(0.3)
    time.sleep(1)

print(f"{total} photo(s) dans {dossier}")
if not total:
    sys.exit("aucune photo récupérée")
