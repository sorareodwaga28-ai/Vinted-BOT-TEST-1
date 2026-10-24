"""Batterie de tests d'un modèle — n'envoie PAS d'alertes.

Rassemble des annonces Vinted, les note, et les enregistre dans
batteries/<modele>/resultats.json (+ vignettes) pour que Bastien réponde
OUI / NON à chacune dans la page « Vérification batterie » du projet Claude.

Trois sources d'annonces :
  temoin      vraies paires (« jordan 4 <modèle> », tous prix, titre qui nomme la paire)
  sans_texte  baskets à ses tailles, sans texte, sous le prix max (ce que voit la surveillance)
  jordan4     « jordan 4 » sous le prix max (titres vagues ou faux)

Pour chaque annonce : ressemblance photo, ressemblance aux pièges (pas-ca/),
titre qui nomme la paire ou non, prix dans la limite ou non, et pour les plus
proches : lecture du code de l'étiquette.

Les vues V1 à V5 de la page sont calculées à partir de ces champs :
  V1 seuil · V2 photo sans le bon titre · V3 photo + bon / mauvais titre
  V4 + prix max · V5 + étiquette

Réglages : MODELE (obligatoire sauf lancement auto), SEUIL (départ ; sinon
seuils.json, sinon 0.84), PAGES (défaut 3).
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import open_clip
import requests
import torch
from PIL import Image

import bot
import photo_test as pt

ICI = Path(__file__).parent
IMG = (".jpg", ".jpeg", ".png", ".webp")


def charger(dossier):
    out = []
    for p in sorted(dossier.glob("*")) if dossier.exists() else []:
        if p.suffix.lower() in IMG:
            try:
                out.append(Image.open(p).convert("RGB"))
            except Exception as e:
                print(f"Image illisible {p} : {e}")
    return out


def seuil_depart(modele):
    txt = (os.environ.get("SEUIL") or "").strip().replace(",", ".")
    if txt:
        return float(txt), "choisi au lancement"
    f = ICI / "seuils.json"
    if f.exists():
        s = json.loads(f.read_text()).get(modele)
        if s:
            return float(s), "appris (seuils.json)"
    return 0.84, "par défaut"


def lire(session, url, pages, source, max_prix=None):
    base, purl = bot.page_url(url)
    out = []
    for pg in range(1, pages + 1):
        try:
            lot = bot.fetch_items(session, base, purl + (f"&page={pg}" if pg > 1 else ""))
        except Exception as e:
            print(f"{source} page {pg} illisible : {e}")
            break
        for it in lot:
            if it.get("photo") and (max_prix is None or (bot.price_value(it) or 0) <= max_prix):
                it["source"] = source
                out.append(it)
        time.sleep(3)
    print(f"{source} : {len(out)} annonce(s)")
    return out


def main():
    modele = os.environ.get("MODELE") or pt.modele_depuis_commit()
    if not modele or modele not in pt.PRIX:
        sys.exit(f"Modèle inconnu ou absent : {modele!r}")
    s = pt.slug(modele)
    prix = float(pt.PRIX[modele])
    pages = int(os.environ.get("PAGES") or 3)
    seuil, origine = seuil_depart(modele)
    groupes = [g.strip() for g in pt.MOTS.get(modele, "").split(",") if g.strip()]
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+", os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None

    # 1. Annonces
    session = bot.get_session()
    q = requests.utils.quote
    temoins = lire(session, "https://www.vinted.fr/catalog?catalog[]=1242&order=relevance&search_text="
                   + q(f"jordan 4 {modele}"), 2, "temoin")
    temoins = [it for it in temoins if bot.matches_required(it, groupes)]
    sans_texte = lire(session, pt.BASE_URL + f"&price_to={int(prix)}", pages, "sans_texte", prix)
    jordan4 = lire(session, pt.BASE_URL + f"&price_to={int(prix)}&search_text=" + q("jordan 4"), 2, "jordan4", prix)
    items, ids = [], set()
    for it in temoins + sans_texte + jordan4:
        if it["id"] not in ids:
            ids.add(it["id"])
            items.append(it)
    if not items:
        sys.exit("Aucune annonce lue.")

    # 2. Notes photo
    model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    tok = open_clip.get_tokenizer("ViT-B-32")
    model.eval()

    def emb(imgs):
        with torch.no_grad():
            f = model.encode_image(torch.stack([prep(i) for i in imgs]))
        return f / f.norm(dim=-1, keepdim=True)

    d = ICI / "references" / s
    refs = charger(d)
    if not refs:
        sys.exit(f"Aucune photo de référence dans {d}")
    ref_e = emb(refs)
    pieges = charger(d / "pas-ca")
    piege_e = emb(pieges) if pieges else None
    etis = charger(d / "etiquette")
    eti_e = emb(etis) if etis else None
    with torch.no_grad():
        te = model.encode_text(tok(["a close-up photo of the size label tag inside a sneaker, with barcode and small printed text",
                                    "a photo of sneakers", "a photo of the sole of a sneaker", "a photo of a shoe box"]))
    te = te / te.norm(dim=-1, keepdim=True)

    imgs = {}
    for n, it in enumerate(items, 1):
        try:
            img = Image.open(io.BytesIO(session.get(it["photo"], timeout=20).content)).convert("RGB")
        except Exception as e:
            print(f"Photo illisible {it['id']} : {e}")
            it["score"] = None
            continue
        e = emb([img])
        it["score"] = round(float((e @ ref_e.T).max()), 4)
        it["piege"] = round(float((e @ piege_e.T).max()), 4) if piege_e is not None else None
        it["titre_ok"] = bot.matches_required(it, groupes)
        it["prix_val"] = bot.price_value(it) or 0
        imgs[it["id"]] = img
        if n % 50 == 0:
            print(f"{n}/{len(items)} photos notées")
    items = [it for it in items if it.get("score") is not None]

    # 3. Annonces à juger : témoins + celles proches du seuil + titres qui nomment la paire
    marge = 0.08
    choisies, vus = [], set()

    def ajouter(lst, n):
        for it in lst[:n]:
            if it["id"] not in vus:
                vus.add(it["id"])
                choisies.append(it)

    par_score = sorted(items, key=lambda x: x["score"], reverse=True)
    ajouter([x for x in par_score if x["source"] == "temoin"], 15)
    ajouter([x for x in par_score if x["source"] != "temoin" and x["score"] >= seuil - marge], 35)
    ajouter([x for x in par_score if x["source"] != "temoin" and x["titre_ok"]], 10)
    ajouter([x for x in par_score if x["source"] != "temoin"], 5)   # toujours un peu de contexte

    # 4. Étiquette (V5) sur les plus proches
    codes = pt.CODES.get(modele, [])
    a_ouvrir = [x for x in choisies if x["score"] >= seuil - 0.04 or x["titre_ok"]][:20]
    for x in a_ouvrir:
        x["etiquette"], x["code_lu"] = "absente", ""
        time.sleep(2)
        try:
            urls = pt.photos_annonce(session, x)
        except Exception as e:
            print(f"Annonce illisible {x['id']} : {e}")
            x["etiquette"] = "annonce illisible"
            continue
        notes = []
        for u in urls:
            try:
                im = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
            except Exception:
                continue
            f = emb([im])
            p_eti = float((100 * f @ te.T).softmax(dim=-1)[0][0])
            s_eti = float((f @ eti_e.T).max()) if eti_e is not None else 0
            notes.append((max(p_eti, s_eti), p_eti >= 0.5 or s_eti >= 0.80, im))
        notes.sort(key=lambda t: t[0], reverse=True)
        if notes and notes[0][1]:
            x["etiquette"] = "illisible"
        if codes:
            for _, _, im in notes[:3]:
                try:
                    et, lu = pt.verifier_code(pt.lire_texte(im), codes)
                except Exception as e:
                    print(f"OCR impossible : {e}")
                    break
                if et == "ok":
                    x["etiquette"], x["code_lu"] = "bon code", lu
                    break
                if et == "autre":
                    x["etiquette"], x["code_lu"] = "autre code", lu

    # 5. Fichiers : resultats.json + vignettes
    out = ICI / "batteries" / s
    vig = out / "img"
    if vig.exists():
        for p in vig.glob("*"):
            p.unlink()
    vig.mkdir(parents=True, exist_ok=True)
    lignes = []
    for x in choisies:
        im = imgs.get(x["id"])
        if im is not None:
            im = im.copy()
            im.thumbnail((320, 320))
            im.save(vig / f"{x['id']}.jpg", "JPEG", quality=78)
        lignes.append({
            "id": str(x["id"]), "titre": x["title"], "prix": x["prix_val"], "prix_txt": x["price"],
            "url": x["url"], "photo": x["photo"], "source": x["source"],
            "score": x["score"], "piege": x.get("piege"), "titre_ok": bool(x["titre_ok"]),
            "prix_ok": x["prix_val"] <= prix, "etiquette": x.get("etiquette"), "code_lu": x.get("code_lu", ""),
            "details": x.get("details", ""),
        })
    batterie = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    res = {"modele": modele, "slug": s, "batterie": batterie, "seuil_depart": seuil, "origine_seuil": origine,
           "prix_max": prix, "codes": codes, "nb_references": len(refs), "nb_pieges": len(pieges),
           "nb_lues": {"temoin": len(temoins), "sans_texte": len(sans_texte), "jordan4": len(jordan4)},
           "annonces": lignes}
    (out / "resultats.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(f"{len(lignes)} annonces à juger enregistrées dans {out}")

    if webhook:
        t = [x for x in lignes if x["source"] == "temoin"]
        au_dessus = [x for x in lignes if x["source"] != "temoin" and x["score"] >= seuil]
        msg = (f"**Batterie de tests — {modele}** ({batterie} UTC)\n"
               f"Seuil de départ {seuil:.2f} ({origine}) · prix max {prix:g} €\n"
               f"Témoins : {sum(x['score'] >= seuil for x in t)}/{len(t)} au-dessus du seuil · "
               f"autres annonces au-dessus du seuil : {len(au_dessus)}\n"
               f"**{len(lignes)} annonces à juger OUI / NON** dans la page « Vérification batterie » du projet Claude.")
        requests.post(webhook, json={"content": msg}, timeout=20)


if __name__ == "__main__":
    main()
