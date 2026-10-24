"""Batterie de tests d'un modèle, en 5 manches — n'envoie PAS d'alertes.

Chaque manche apporte de NOUVELLES annonces (jamais déjà jugées), choisies grâce aux
réponses OUI / NON des manches d'avant (références et pièges ajoutés, seuil recalculé,
recherches tirées des titres des OUI). Bastien répond OUI / NON dans la page
« Vérification batterie ».

  V1 Seuil      témoins + baskets sans texte + « jordan 4 » : où placer le seuil photo ?
  V2 Sans titre annonces dont le titre NE nomme PAS la paire : la photo seule les trouve-t-elle ?
  V3 Titres     titres qui nomment la paire + titres d'autres coloris : le titre trompe-t-il ?
  V4 Prix       ce que la surveillance enverrait vraiment, sous le prix max
  V5 Étiquette  tous les OUI + nouvelles alertes, avec lecture du code de l'étiquette

Les OUI des manches d'avant sont repris dans la manche (« déjà validée »).

Fichiers : batteries/<slug>/<V>.json, vignettes dans batteries/<slug>/img/<V>/,
état dans batteries/<slug>/etat.json.
Réglages : MODELE, ETAPE (V1..V5, sinon la manche en cours), SEUIL (sinon seuils.json, sinon 0.84).
"""
import io
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import open_clip
import requests
import torch
from PIL import Image

import bot
import photo_test as pt

ICI = Path(__file__).parent
IMG = (".jpg", ".jpeg", ".png", ".webp")
ETAPES = ["V1", "V2", "V3", "V4", "V5"]
NOMS = {"V1": "Seuil", "V2": "Sans le bon titre", "V3": "Titres", "V4": "Prix max", "V5": "Étiquette"}
TAILLE = 20
VIDES = set("jordan air nike retro aj4 j4 jordan4 iv og taille size pointure eu us uk fr homme femme neuf "
            "neuve tres bon etat vintage paire baskets basket sneakers sneaker chaussures chaussure de du la le "
            "et a en avec sans boite original originale originales authentique".split())


def charger(dossier):
    out = []
    for p in sorted(dossier.glob("*")) if dossier.exists() else []:
        if p.suffix.lower() in IMG:
            try:
                out.append(Image.open(p).convert("RGB"))
            except Exception as e:
                print(f"Image illisible {p} : {e}")
    return out


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


def mots_des_oui(titres, modele):
    """Mots qui reviennent dans les titres des vraies paires, hors nom du modèle : idées de recherches."""
    exclus = VIDES | set(bot.norm(modele).split())
    c = Counter()
    for t in titres:
        for w in set(re.sub(r"[^a-z0-9 ]+", " ", bot.norm(t)).split()):
            if len(w) >= 3 and not w.isdigit() and w not in exclus:
                c[w] += 1
    return [w for w, n in c.most_common(4) if n >= 2]


def autre_modele(it, modele):
    for x, g in pt.MOTS.items():
        if x != modele and bot.matches_required(it, [s.strip() for s in g.split(",") if s.strip()]):
            return x
    return None


def main():
    modele = os.environ.get("MODELE") or pt.modele_depuis_commit()
    if not modele or modele not in pt.PRIX:
        sys.exit(f"Modèle inconnu ou absent : {modele!r}")
    s = pt.slug(modele)
    dossier = ICI / "batteries" / s
    dossier.mkdir(parents=True, exist_ok=True)
    etat_f = dossier / "etat.json"
    etat = json.loads(etat_f.read_text()) if etat_f.exists() else {"etape": "V1", "terminees": []}
    if not os.environ.get("MODELE") and etat_f.exists():
        print(f"{modele} a déjà ses manches en cours ({etat['etape']}) : photos ajoutées, rien à relancer.")
        return
    etape = (os.environ.get("ETAPE") or etat["etape"]).upper()
    if etape not in ETAPES:
        sys.exit(f"Manche inconnue : {etape}")
    prix = float(pt.PRIX[modele])
    groupes = [g.strip() for g in pt.MOTS.get(modele, "").split(",") if g.strip()]
    txt = (os.environ.get("SEUIL") or "").strip().replace(",", ".")
    seuils = json.loads((ICI / "seuils.json").read_text()) if (ICI / "seuils.json").exists() else {}
    seuil = float(txt) if txt else float(seuils.get(modele, 0.84))
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+", os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None

    # Réponses déjà données (toutes manches) et annonces des manches d'avant
    vf = ICI / "verdicts" / f"{s}.json"
    rep = json.loads(vf.read_text()).get("verdicts", {}) if vf.exists() else {}
    dernier = {}
    for k, r in rep.items():
        e, i = k.split(":", 1) if ":" in k else ("V1", k)
        dernier[i] = (r, e)
    deja = set(dernier)
    precedentes = {}
    for e in ETAPES:
        f = dossier / f"{e}.json"
        if f.exists():
            for a in json.loads(f.read_text())["annonces"]:
                precedentes[a["id"]] = a
    oui = [precedentes[i] for i, (r, _) in dernier.items() if r == "oui" and i in precedentes]
    print(f"Manche {etape} · seuil {seuil:.3f} · réponses connues {len(rep)} dont {len(oui)} OUI")

    # 1. Annonces de la manche
    session = bot.get_session()
    q = requests.utils.quote
    jordan = pt.BASE_URL + f"&price_to={int(prix)}&search_text=" + q("jordan 4")
    sans = pt.BASE_URL + f"&price_to={int(prix)}"
    idees = mots_des_oui([a["titre"] for a in oui], modele)
    extra = []
    for w in idees:
        extra += lire(session, pt.BASE_URL + f"&price_to={int(prix)}&search_text=" + q(f"jordan 4 {w}"), 1, f"recherche « jordan 4 {w} »", prix)
    if etape == "V1":
        items = (lire(session, "https://www.vinted.fr/catalog?catalog[]=1242&order=relevance&search_text=" + q(f"jordan 4 {modele}"), 2, "temoin")
                 + lire(session, sans, 3, "sans_texte", prix) + lire(session, jordan, 2, "jordan4", prix))
    elif etape == "V2":
        items = lire(session, sans, 5, "sans_texte", prix) + lire(session, jordan, 4, "jordan4", prix) + extra
    elif etape == "V3":
        items = (lire(session, "https://www.vinted.fr/catalog?catalog[]=1242&order=newest_first&search_text=" + q(f"jordan 4 {modele}"), 2, "bon titre")
                 + lire(session, pt.BASE_URL + "&search_text=" + q(f"jordan 4 {modele}"), 2, "bon titre, tes tailles")
                 + lire(session, jordan, 4, "jordan4", prix) + extra)
    else:   # V4, V5
        items = lire(session, sans, 6, "sans_texte", prix) + lire(session, jordan, 4, "jordan4", prix) + extra
    vus, uniq = set(), []
    for it in items:
        i = str(it["id"])
        if i not in vus and i not in deja:
            vus.add(i)
            uniq.append(it)
    items = uniq
    if not items and not oui:
        sys.exit("Aucune nouvelle annonce lue.")

    # 2. Notes photo (références et pièges à jour)
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
            continue
        e = emb([img])
        it["score"] = round(float((e @ ref_e.T).max()), 4)
        it["piege"] = round(float((e @ piege_e.T).max()), 4) if piege_e is not None else None
        it["titre_ok"] = bot.matches_required(it, groupes)
        it["autre"] = autre_modele(it, modele)
        it["prix_val"] = bot.price_value(it) or 0
        imgs[str(it["id"])] = img
        if n % 50 == 0:
            print(f"{n}/{len(items)} photos notées")
    items = [it for it in items if "score" in it]
    photo_ok = lambda x: x["score"] >= seuil and not (x.get("piege") is not None and x["piege"] > x["score"])
    alerte = lambda x: (photo_ok(x) or x["titre_ok"]) and x["prix_val"] <= prix

    # 3. Choix des annonces à juger selon la manche
    tri = lambda lst: sorted(lst, key=lambda x: x["score"], reverse=True)
    choix = []

    def prendre(lst, n, raison):
        for x in lst:
            if len([c for c in choix if c[1] == raison]) >= n:
                break
            if all(c[0]["id"] != x["id"] for c in choix):
                choix.append((x, raison))

    if etape == "V1":
        prendre(tri([x for x in items if x["source"] == "temoin" and x["titre_ok"]]), 6, "Témoin : le titre nomme la paire")
        prendre(tri([x for x in items if x["source"] != "temoin" and x["score"] >= seuil - 0.08]), 10, "Photo proche du seuil")
        prendre(tri([x for x in items if x["source"] != "temoin"]), TAILLE - len(choix), "Photo la plus ressemblante")
    elif etape == "V2":
        sans_nom = [x for x in items if not x["titre_ok"]]
        prendre(tri([x for x in sans_nom if x["source"].startswith("recherche")]), 5, "Trouvée par une recherche tirée de tes OUI")
        prendre(tri(sans_nom), TAILLE - len(choix), "Titre ne nomme pas la paire")
    elif etape == "V3":
        prendre(tri([x for x in items if x["titre_ok"]]), 10, "Titre qui nomme la paire")
        prendre(tri([x for x in items if x["autre"]]), 6, "Titre d'un autre coloris")
        prendre(tri([x for x in items if not x["titre_ok"] and not x["autre"]]), TAILLE - len(choix), "Titre vague")
    elif etape == "V4":
        prendre(tri([x for x in items if alerte(x)]), 16, "Le bot enverrait une alerte")
        prendre(tri([x for x in items if not alerte(x) and x["score"] >= seuil - 0.05]), TAILLE - len(choix), "Juste sous le seuil")
    else:   # V5
        prendre(tri([x for x in items if alerte(x)]), 12, "Nouvelle alerte du bot")

    # Les OUI des manches d'avant reviennent (à rejuger en V5, avec l'étiquette)
    repris = []
    for a in oui:
        a = dict(a, raison=f"Déjà validée ({dernier[a['id']][1]})", repris=True, a_juger=(etape == "V5"))
        repris.append(a)

    # 4. Étiquette (V5 pour tout, sinon les plus proches)
    codes = pt.CODES.get(modele, [])

    def etiquette(x, photo_principale):
        x["etiquette"], x["code_lu"], x["photo_etiquette"] = "absente", "", ""
        time.sleep(2)
        try:
            urls = pt.photos_annonce(session, {"url": x["url"], "photo": photo_principale, "id": x["id"]})
        except Exception as e:
            print(f"Annonce illisible {x['id']} : {e}")
            x["etiquette"] = "annonce illisible"
            return
        notes = []
        for u in urls[:12]:
            try:
                im = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
            except Exception:
                continue
            f = emb([im])
            p_eti = float((100 * f @ te.T).softmax(dim=-1)[0][0])
            s_eti = float((f @ eti_e.T).max()) if eti_e is not None else 0
            notes.append((max(p_eti, s_eti), p_eti >= 0.5 or s_eti >= 0.80, im, u))
        notes.sort(key=lambda t: t[0], reverse=True)
        if notes and notes[0][1]:
            x["etiquette"], x["photo_etiquette"] = "illisible", notes[0][3]
        if codes:
            for _, _, im, u in notes[:3]:
                try:
                    et, lu = pt.verifier_code(pt.lire_texte(im), codes)
                except Exception as e:
                    print(f"OCR impossible : {e}")
                    break
                if et == "ok":
                    x["etiquette"], x["code_lu"], x["photo_etiquette"] = "bon code", lu, u
                    break
                if et == "autre":
                    x["etiquette"], x["code_lu"], x["photo_etiquette"] = "autre code", lu, u

    nouvelles = []
    for x, raison in choix:
        a = {"id": str(x["id"]), "titre": x["title"], "prix": x["prix_val"], "prix_txt": x["price"],
             "url": x["url"], "photo": x["photo"], "source": x["source"], "score": x["score"],
             "piege": x.get("piege"), "titre_ok": bool(x["titre_ok"]), "autre_modele": x.get("autre") or "",
             "prix_ok": x["prix_val"] <= prix, "alerte": bool(alerte(x)), "details": x.get("details", ""),
             "raison": raison, "repris": False, "a_juger": True}
        if etape == "V5" or (etape in ("V3", "V4") and a["alerte"]):
            etiquette(a, x["photo"])
        nouvelles.append(a)
    if etape == "V5":
        for a in repris:
            etiquette(a, a["photo"])

    # 5. Fichiers
    vig = dossier / "img" / etape
    if vig.exists():
        for p in vig.glob("*"):
            p.unlink()
    vig.mkdir(parents=True, exist_ok=True)
    for a in nouvelles:
        im = imgs.get(a["id"])
        if im is not None:
            im = im.copy()
            im.thumbnail((320, 320))
            im.save(vig / f"{a['id']}.jpg", "JPEG", quality=78)
    for a in repris:   # vignette de la manche d'origine
        for e in ETAPES:
            src = dossier / "img" / e / f"{a['id']}.jpg"
            if src.exists() and not (vig / src.name).exists():
                (vig / src.name).write_bytes(src.read_bytes())
                break
    batterie = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    res = {"modele": modele, "slug": s, "etape": etape, "nom_etape": NOMS[etape], "batterie": batterie,
           "seuil": seuil, "prix_max": prix, "codes": codes, "nb_references": len(refs), "nb_pieges": len(pieges),
           "recherches_tirees_des_oui": [f"jordan 4 {w}" for w in idees],
           "annonces": nouvelles + repris}
    (dossier / f"{etape}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    if not etat_f.exists():          # ensuite, c'est l'apprentissage qui fait avancer les manches
        etat_f.write_text(json.dumps({"etape": etape, "terminees": []}, ensure_ascii=False))
    print(f"{len(nouvelles)} nouvelles annonces + {len(repris)} déjà validées → {dossier / (etape + '.json')}")

    if webhook:
        msg = (f"**Batterie {modele} — manche {etape} ({NOMS[etape]})**\n"
               f"Seuil {seuil:.3f} · {len(refs)} références · {len(pieges)} pièges · prix max {prix:g} €\n"
               + (f"Recherches tirées de tes OUI : {', '.join('jordan 4 ' + w for w in idees)}\n" if idees else "")
               + f"**{len(nouvelles)} nouvelles annonces à juger**" + (f" + {len(repris)} déjà validées" if repris else "")
               + " dans la page « Vérification batterie ».")
        requests.post(webhook, json={"content": msg}, timeout=20)


if __name__ == "__main__":
    main()
