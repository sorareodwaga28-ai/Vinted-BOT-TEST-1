"""Test de reconnaissance photo (CLIP) — n'envoie PAS d'alertes, sert à apprendre.

Lit les annonces Vinted « jordan » (sans filtre sur le titre), analyse la 1re photo
de chaque annonce et envoie sur Discord un classement avec les scores.

Deux façons de juger une photo :
1. Photos de référence : dossier references/<modele>/ (jpg, png, webp).
   Score = ressemblance avec la photo de référence la plus proche (0 à 1).
2. Description texte : CLIP choisit, parmi les coloris décrits plus bas,
   celui qui colle le mieux à la photo (pourcentage).
"""
import csv
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

# Baskets Hommes (1242) ET Femmes (2632), toutes tailles : beaucoup de Jordan 4 (Frozen Moments...)
# sont rangées en Femmes ou en petites tailles (36-39).
BASE_URL = ("https://www.vinted.fr/catalog?catalog[]=1242&catalog[]=2632&currency=EUR"
            + "&status_ids[]=6&status_ids[]=1&status_ids[]=2&status_ids[]=3&order=newest_first")

# Description de chaque coloris pour CLIP (en anglais : CLIP a appris en anglais).
COLORIS = {
    "Vivid Sulfur": "Air Jordan 4 Vivid Sulfur sneakers, white and grey with bright yellow details",
    "Fire Red": "Air Jordan 4 Fire Red sneakers, white leather with red details",
    "Infrared": "Air Jordan 4 Infrared sneakers, grey and black with bright pinkish red details",
    "Black Canvas": "Air Jordan 4 Black Canvas sneakers, black canvas with red details",
    "Midnight Navy": "Air Jordan 4 Midnight Navy sneakers, white with navy blue details",
    "Seafoam": "Air Jordan 4 Seafoam sneakers, white with pale mint green details",
    "Canyon Purple": "Air Jordan 4 Canyon Purple sneakers, purple suede",
    "Craft Olive": "Air Jordan 4 Craft Olive sneakers, olive green and cream",
    "Fear": "Air Jordan 4 Fear sneakers, black and dark grey nubuck with white speckled midsole and white wings",
    "Frozen Moments": "Air Jordan 4 Frozen Moments sneakers, light grey suede and patent leather with chrome silver wings and sail midsole",
    "autre Jordan": "Jordan sneakers of another model or colorway",
    "pas une paire": "a photo that does not show sneakers",
}

# Prix max de chaque paire (ceux du bot).
PRIX = {"Vivid Sulfur": 30, "Fire Red": 60, "Infrared": 40, "Black Canvas": 40,
        "Midnight Navy": 40, "Seafoam": 50, "Canyon Purple": 50, "Craft Olive": 50, "Fear": 50, "Frozen Moments": 60}

# Code produit imprimé sur l'étiquette intérieure (doit correspondre exactement).
CODES = {"Vivid Sulfur": ["AQ9129-101"], "Fire Red": ["DC7770-160"], "Midnight Navy": ["DH6927-140"], "Black Canvas": ["DH7138-006"], "Fear": ["FQ8138-002"], "Frozen Moments": ["AQ9129-001"]}

# Lettres et chiffres que l'OCR confond souvent.
CONFUS = {"0": "0OQD", "1": "1IL", "2": "2Z", "5": "5S", "8": "8B", "6": "6G",
          "O": "O0", "I": "I1L", "S": "S5", "B": "B8", "Z": "Z2", "Q": "Q0O"}


def motif_code(code):
    parts = []
    for c in code.upper():
        if c == "-":
            parts.append(r"[-\s._]?")
        else:
            parts.append("[" + CONFUS.get(c, c) + "]")
    return re.compile("".join(parts))


_ocr = None


def lire_texte(img):
    """Texte lu sur l'image (majuscules, sans espaces superflus)."""
    global _ocr
    if _ocr is None:
        import easyocr
        _ocr = easyocr.Reader(["en"], gpu=False, verbose=False)
    import numpy as np
    from PIL import ImageEnhance, ImageFilter, ImageOps
    w, h = img.size
    if max(w, h) < 1200:   # agrandir les petites photos aide la lecture
        k = 1200 / max(w, h)
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    # 2 lectures : photo telle quelle, puis en gris + contraste + netteté
    net = ImageEnhance.Contrast(ImageOps.autocontrast(ImageOps.grayscale(img))).enhance(1.5)
    net = net.filter(ImageFilter.UnsharpMask(4, 200, 2))
    textes = []
    for version in (img, net):
        lignes = _ocr.readtext(np.array(version), detail=0, paragraph=False,
                               allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
        textes.append(" ".join(lignes).upper())
    return " | ".join(textes)


def verifier_code(texte, codes):
    """('ok', code) si un code attendu est lu, ('autre', code lu) si un autre code produit, sinon (None, '')."""
    compact = texte.replace(" ", "")
    for c in codes:
        if motif_code(c).search(texte) or motif_code(c).search(compact):
            return "ok", c
    m = re.search(r"[A-Z0-9]{2}\d{4}[-\s]?\d{3}", texte)
    return ("autre", m.group(0)) if m else (None, "")


# Mots obligatoires actuels du bot, pour comparer titre et photo.
MOTS = {
    "Vivid Sulfur": "jordan + sulfur, jordan + sulphur, jordan 4 + sulfur, jordan 4 + sulphur, jordan4 + sulfur, jordan4 + sulphur, j4 + sulfur, j4 + sulphur, aj4 + sulfur, aj4 + sulphur, jordan iv + sulfur, jordan iv + sulphur",
    "Fire Red": "jordan + fire + red, jordan + firered, jordan 4 + fire + red, jordan 4 + firered, jordan4 + fire + red, jordan4 + firered, j4 + fire + red, j4 + firered, aj4 + fire + red, aj4 + firered, jordan iv + fire + red, jordan iv + firered",
    "Infrared": "jordan + infrared, jordan + infra + red, jordan 4 + infrared, jordan 4 + infra + red, jordan4 + infrared, jordan4 + infra + red, j4 + infrared, j4 + infra + red, aj4 + infrared, aj4 + infra + red, jordan iv + infrared, jordan iv + infra + red",
    "Black Canvas": "jordan + black + canvas, jordan 4 + black + canvas, jordan4 + black + canvas, j4 + black + canvas, aj4 + black + canvas, jordan iv + black + canvas",
    "Midnight Navy": "jordan + midnight + navy, jordan + midnightnavy, jordan 4 + midnight + navy, jordan4 + midnight + navy, j4 + midnight + navy, aj4 + midnight + navy, jordan iv + midnight + navy",
    "Seafoam": "jordan + seafoam, jordan + sea + foam, jordan 4 + seafoam, jordan 4 + sea + foam, jordan4 + seafoam, jordan4 + sea + foam, j4 + seafoam, j4 + sea + foam, aj4 + seafoam, aj4 + sea + foam, jordan iv + seafoam, jordan iv + sea + foam",
    "Canyon Purple": "jordan + canyon, jordan 4 + canyon, jordan4 + canyon, j4 + canyon, aj4 + canyon, jordan iv + canyon",
    "Craft Olive": "jordan + craft + olive, jordan 4 + craft + olive, jordan4 + craft + olive, j4 + craft + olive, aj4 + craft + olive, jordan iv + craft + olive",
    "Fear": "jordan + fear, jordan 4 + fear, jordan4 + fear, j4 + fear, aj4 + fear, jordan iv + fear",
    "Frozen Moments": "jordan + frozen, jordan 4 + frozen, jordan4 + frozen, j4 + frozen, aj4 + frozen, jordan iv + frozen",
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", bot.norm(s)).strip("-")


def post(webhook, payload):
    r = requests.post(webhook, json=payload, timeout=20)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        requests.post(webhook, json=payload, timeout=20)
    time.sleep(1)


def modele_depuis_commit():
    """Lancement auto : retrouve le modèle d'après le dossier où des photos ont été ajoutées."""
    try:
        c = json.loads(os.environ.get("HEAD_COMMIT") or "null") or {}
    except ValueError:
        c = {}
    par_slug = {slug(k): k for k in COLORIS}
    fichiers = [l.strip() for l in (os.environ.get("CHANGES") or "").splitlines() if l.strip()]
    for f in fichiers + (c.get("added") or []) + (c.get("modified") or []):
        parts = f.split("/")
        if len(parts) >= 3 and parts[0] == "references" and parts[1] in par_slug:
            return par_slug[parts[1]]
    return None


def jeton(url):
    m = re.search(r"/t/([^/]+)/", url or "")
    return m.group(1) if m else url


def photos_annonce(session, item):
    """Toutes les photos d'une annonce (on ouvre sa page). Liste vide si échec."""
    r = session.get(item["url"], timeout=30)
    if r.status_code != 200:
        print(f"[diag] annonce {item['id']} : HTTP {r.status_code}")
        return []
    full = r.text
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", r.text, re.S):
        try:
            arr = json.loads(m.group(1))
            if len(arr) > 1 and isinstance(arr[1], str):
                full += arr[1]
        except ValueError:
            pass
    cible = jeton(item["photo"])
    dec = json.JSONDecoder()
    for m in re.finditer(r'"photos":\[', full):
        try:
            arr, _ = dec.raw_decode(full, m.end() - 1)
        except ValueError:
            continue
        urls = [p.get("full_size_url") or p.get("url") for p in arr if isinstance(p, dict)]
        urls = [u for u in urls if u]
        # On garde la liste qui contient la photo de l'annonce (et pas celles des annonces suggérées).
        if urls and any(jeton(u) == cible for u in urls):
            return urls
    # Plan B : adresses d'images dans la page, en partant de la photo de l'annonce.
    brut = full.replace("\\u0026", "&").replace("\\/", "/")
    urls, vus = [], set()
    for u in re.findall(r"https://images\d*\.vinted\.net/t/[^\"\\\s<>]+", brut):
        j = jeton(u)
        if j not in vus:
            vus.add(j)
            urls.append(u)
    if cible in vus:
        i = [jeton(u) for u in urls].index(cible)
        return urls[i:i + 20]
    return []


def main():
    auto = not os.environ.get("MODELE")
    modele = os.environ.get("MODELE") or modele_depuis_commit() or "Vivid Sulfur"
    prix_max = (os.environ.get("PRIX_MAX") or "").strip().lower()
    recherche = (os.environ.get("RECHERCHE") or "").strip()
    pages = int(os.environ.get("PAGES") or 3)
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+",
                  os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None
    if modele not in COLORIS:
        sys.exit(f"Modèle inconnu : {modele}")

    # 1. Lire les annonces
    if prix_max in ("", "defaut", "défaut"):
        prix_max = str(PRIX.get(modele, ""))
    elif prix_max in ("aucun", "0"):
        prix_max = ""
    url = (BASE_URL + (f"&price_to={prix_max}" if prix_max else "")
           + (f"&search_text={requests.utils.quote(recherche)}" if recherche else ""))
    base, purl = bot.page_url(url)
    session = bot.get_session()
    items, ids = [], set()
    for pg in range(1, pages + 1):
        try:
            for it in bot.fetch_items(session, base, purl + (f"&page={pg}" if pg > 1 else "")):
                if it["id"] not in ids and it.get("photo"):
                    ids.add(it["id"])
                    items.append(it)
        except Exception as e:
            print(f"Page {pg} illisible : {e}")
        time.sleep(3)
    # Sécurité : rien au-dessus du prix max, même si Vinted en renvoie.
    if prix_max:
        avant = len(items)
        items = [it for it in items if (bot.price_value(it) or 0) <= float(prix_max)]
        if avant != len(items):
            print(f"{avant - len(items)} annonce(s) au-dessus de {prix_max} € retirées")
    groupes = [g.strip() for g in MOTS[modele].split(",")]
    # Témoins (option, désactivée par défaut) : vraies annonces du modèle, SANS limite
    # de prix, pour calibrer le seuil. Jamais comptées comme résultats.
    if (os.environ.get("TEMOINS") or "non").strip().lower() == "oui":
        turl = ("https://www.vinted.fr/catalog?catalog[]=1242&order=relevance&search_text="
                + requests.utils.quote(f"jordan 4 {modele}"))
        try:
            for it in bot.fetch_items(session, "https://www.vinted.fr", turl):
                if it["id"] not in ids and it.get("photo") and bot.matches_required(it, groupes):
                    ids.add(it["id"])
                    it["temoin"] = True
                    items.append(it)
        except Exception as e:
            print(f"Témoins illisibles : {e}")
    nb_temoins = sum(1 for x in items if x.get("temoin"))
    print(f"{len(items)} annonces avec photo, dont {nb_temoins} témoins")
    if not items:
        sys.exit("Aucune annonce lue.")

    # 2. Charger le modèle CLIP
    model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    tok = open_clip.get_tokenizer("ViT-B-32")
    model.eval()

    def embed_images(imgs):
        with torch.no_grad():
            f = model.encode_image(torch.stack([prep(i) for i in imgs]))
        return f / f.norm(dim=-1, keepdim=True)

    labels = list(COLORIS)
    with torch.no_grad():
        t = model.encode_text(tok([f"a photo of {COLORIS[k]}" for k in labels]))
    t = t / t.norm(dim=-1, keepdim=True)
    cible = labels.index(modele)

    # 3. Photos de référence
    ref_dir = Path("references") / slug(modele)
    refs = []
    for p in sorted(ref_dir.glob("*")) if ref_dir.exists() else []:
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                refs.append(Image.open(p).convert("RGB"))
            except Exception as e:
                print(f"Référence illisible {p.name} : {e}")
    ref_emb = embed_images(refs) if refs else None
    # Pièges : photos de modèles qui ressemblent mais qui ne sont PAS le bon.
    pieges, noms_pieges = [], []
    piege_dir = ref_dir / "pas-ca"
    for p in sorted(piege_dir.glob("*")) if piege_dir.exists() else []:
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                pieges.append(Image.open(p).convert("RGB"))
                noms_pieges.append(p.stem)
            except Exception as e:
                print(f"Piège illisible {p.name} : {e}")
    piege_emb = embed_images(pieges) if pieges else None
    mode = f"{len(refs)} photo(s) de référence" if refs else "description texte seule (aucune photo de référence)"
    if pieges:
        mode += f" + {len(pieges)} photo(s) piège"
    print(f"Mode : {mode}")

    # 4. Analyser chaque annonce
    res = []
    for i, it in enumerate(items, 1):
        try:
            r = session.get(it["photo"], timeout=20)
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            print(f"Photo illisible {it['id']} : {e}")
            continue
        e = embed_images([img])
        probs = (100 * e @ t.T).softmax(dim=-1)[0]
        top = int(probs.argmax())
        if piege_emb is not None:
            sp = (e @ piege_emb.T)[0]
            it["piege"] = float(sp.max())
            it["piege_nom"] = noms_pieges[int(sp.argmax())]
        it.update({
            "ref": float((e @ ref_emb.T).max()) if ref_emb is not None else None,
            "prob": float(probs[cible]),
            "devine": labels[top],
            "devine_prob": float(probs[top]),
            "titre_ok": bot.matches_required(it, groupes),
        })
        res.append(it)
        if i % 25 == 0:
            print(f"{i}/{len(items)} photos analysées")

    for x in res:
        if x["ref"] is not None and "piege" in x:
            x["marge"] = x["ref"] - x["piege"]
    if ref_emb is not None and piege_emb is not None:
        key = "marge"
    elif ref_emb is not None:
        key = "ref"
    else:
        key = "prob"
    res.sort(key=lambda x: x[key], reverse=True)

    # Étape 1 : est-ce le bon modèle ? Seuil choisi à la main, sinon calé sur les témoins
    # (80 % des vraies paires témoins sont au-dessus du seuil automatique).
    seuil_txt = (os.environ.get("SEUIL") or "").strip().replace(",", ".")
    sc_t = sorted((x[key] for x in res if x.get("temoin")), reverse=True)
    if seuil_txt:
        seuil, origine_seuil = float(seuil_txt), "choisi à la main"
    elif len(sc_t) >= 3:
        seuil, origine_seuil = sc_t[int(0.8 * (len(sc_t) - 1))], "automatique : 80 % des témoins au-dessus"
    else:
        seuil = {"marge": 0.0, "ref": 0.84, "prob": 0.5}[key]
        origine_seuil = "par défaut"
    for x in res:
        x["oui"] = x[key] >= seuil

    # 5. Résultats complets (fichier téléchargeable)
    with open("resultats.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rang", "temoin", "score_reference", "score_piege", "piege_le_plus_proche", "marge",
                    f"proba_{modele}", "clip_pense", "titre_mentionne_modele", "prix", "titre", "lien"])
        for n, x in enumerate(res, 1):
            w.writerow([n, "oui" if x.get("temoin") else "", "" if x["ref"] is None else f"{x['ref']:.3f}",
                        f"{x['piege']:.3f}" if "piege" in x else "", x.get("piege_nom", ""),
                        f"{x['marge']:+.3f}" if "marge" in x else "", f"{x['prob']:.2f}",
                        x["devine"], "oui" if x["titre_ok"] else "non", x["price"], x["title"], x["url"]])
    for n, x in enumerate(res[:30], 1):
        print(f"{n:>3} | ref {x['ref'] if x['ref'] is None else round(x['ref'], 3)} | {x['devine']} {x['devine_prob']:.0%} | titre {'OK' if x['titre_ok'] else '--'} | {x['title'][:60]}")

    if not webhook:
        print("Pas de webhook Discord : résultats seulement dans les logs.")
        return

    # 6. Discord
    rangs_t = [n for n, x in enumerate(res, 1) if x.get("temoin")]
    devine_cible = [x for x in res if x["devine"] == modele]
    caches = [x for x in devine_cible if not x["titre_ok"]]
    titre_ok = sum(x["titre_ok"] for x in res)
    lignes = [
        f"**Test photo — {modele}**" + (" (lancé automatiquement après ajout de photos)" if auto else ""),
        f"Annonces analysées : {len(res)} (recherche : " + (f"« {recherche} »" if recherche else "aucun texte, toutes les baskets")
        + f", {pages} page(s), "
        + (f"prix max {prix_max} €)" if prix_max else "tous les prix)"),
        f"Mode : {mode}",
        f"Titre qui mentionne le modèle : {titre_ok}",
        f"CLIP pense que c'est une {modele} : {len(devine_cible)}, dont {len(caches)} avec un titre qui ne le dit pas",
    ]
    if rangs_t:
        sc = sorted((x[key] for x in res if x.get("temoin")), reverse=True)
        lignes.append(f"Témoins (vraies {modele} ajoutées exprès) : {len(rangs_t)}, classés aux rangs "
                      f"{', '.join(map(str, rangs_t[:12]))}{'…' if len(rangs_t) > 12 else ''}")
        lignes.append(f"Score des témoins ({'marge' if key == 'marge' else 'ressemblance' if key == 'ref' else 'proba'}) : "
                      f"du plus haut {sc[0]:.3f} au plus bas {sc[-1]:.3f}")
        autres = [x[key] for x in res if not x.get("temoin")]
        if autres:
            lignes.append(f"Meilleur score parmi les autres annonces : {max(autres):.3f}")
    else:
        lignes.append("Aucun témoin trouvé sur Vinted pour ce modèle.")
    if ref_emb is not None:
        for s in (0.90, 0.85, 0.80):
            lignes.append(f"Ressemblance ≥ {s:.2f} : {sum(x['ref'] >= s for x in res)}")
    if key == "marge":
        lignes.append(f"Marge positive (plus proche de tes références que des pièges) : {sum(x['marge'] > 0 for x in res)}")
    oui_autres = sum(bool(x["oui"] and not x.get("temoin")) for x in res)
    oui_t = sum(bool(x["oui"] and x.get("temoin")) for x in res)
    lignes.append(f"**Étape 1 — est-ce une {modele} ?** Seuil {seuil:.3f} ({origine_seuil}).")
    lignes.append(f"OUI : {oui_autres} annonce(s) hors témoins"
                  + (f" · témoins reconnus : {oui_t}/{len(rangs_t)}" if rangs_t else ""))
    lignes.append("Ci-dessous : les 10 annonces les plus ressemblantes, même si elles sont sous le seuil "
                  "(vert = OUI, gris = NON), puis les rangs 11 à 15. [T] = témoin.")
    post(webhook, {"content": "\n".join(lignes)})

    def embed(x, n, couleur):
        champs = [
            {"name": "Verdict étape 1", "value": "OUI" if x["oui"] else "NON", "inline": True},
            {"name": "Prix", "value": x["price"], "inline": True},
            {"name": "CLIP pense", "value": f"{x['devine']} ({x['devine_prob']:.0%})", "inline": True},
            {"name": "Titre", "value": "mentionne le modèle" if x["titre_ok"] else "ne le dit pas", "inline": True},
        ]
        if "marge" in x:
            champs.insert(0, {"name": "Piège le plus proche", "value": f"{x['piege_nom']} ({x['piege']:.3f})", "inline": True})
            champs.insert(0, {"name": "Marge", "value": f"{x['marge']:+.3f}", "inline": True})
        if x["ref"] is not None:
            champs.insert(0, {"name": "Ressemblance", "value": f"{x['ref']:.3f}", "inline": True})
        tag = "[T] " if x.get("temoin") else ""
        return {"title": f"#{n} · {tag}{x['title']}"[:250], "url": x["url"], "color": couleur,
                "fields": champs, "thumbnail": {"url": x["photo"]}}

    coul = lambda x: 0x2ECC71 if x["oui"] else 0x95A5A6
    post(webhook, {"embeds": [embed(x, n, coul(x)) for n, x in enumerate(res[:10], 1)]})
    if len(res) > 10:
        post(webhook, {"content": "**Zone de doute (rangs 11 à 15)**",
                       "embeds": [embed(x, n, coul(x)) for n, x in enumerate(res[10:15], 11)]})

    # 7. Contrôle de l'étiquette intérieure sur les meilleures candidates
    nb = int(os.environ.get("VERIF") or 20)
    if nb <= 0:
        return
    eti_dir = ref_dir / "etiquette"
    etis = []
    for p in sorted(eti_dir.glob("*")) if eti_dir.exists() else []:
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                etis.append(Image.open(p).convert("RGB"))
            except Exception as e:
                print(f"Étiquette de référence illisible {p.name} : {e}")
    eti_emb = embed_images(etis) if etis else None
    codes = CODES.get(modele, [])
    controle_ref = ""
    if codes and etis:
        try:
            etat = verifier_code(lire_texte(etis[0]), codes)
            controle_ref = ("Lecture de ton étiquette de référence : code " + etat[1] + " lu correctement."
                            if etat[0] == "ok" else
                            "Lecture de ton étiquette de référence : code NON lu (photo trop floue pour l'OCR ?).")
        except Exception as e:
            controle_ref = f"OCR indisponible : {e}"
        print(controle_ref)
    textes_eti = ["a close-up photo of the size label tag inside a sneaker, with barcode and small printed text",
                  "a photo of sneakers", "a photo of the sole of a sneaker", "a photo of a shoe box"]
    with torch.no_grad():
        te = model.encode_text(tok(textes_eti))
    te = te / te.norm(dim=-1, keepdim=True)

    # Étape 2 : seulement les annonces jugées OUI à l'étape 1
    candidates = [x for x in res if x["oui"] and not x.get("temoin")][:nb]
    candidates += [x for x in res if x["oui"] and x.get("temoin")][:2]   # 2 témoins pour comparer
    if not candidates:
        post(webhook, {"content": f"**Étape 2 — étiquette** : aucune annonce n'a passé l'étape 1, rien à ouvrir."})
        return
    bilan = []
    for x in candidates:
        time.sleep(2)
        try:
            urls = photos_annonce(session, x)
        except Exception as e:
            print(f"Annonce illisible {x['id']} : {e}")
            urls = []
        x["nb_photos"] = len(urls)
        meilleur, best_ref, notes = None, x["ref"], []
        for u in urls:
            try:
                img = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
            except Exception:
                continue
            e = embed_images([img])
            p_eti = float((100 * e @ te.T).softmax(dim=-1)[0][0])
            s_eti = float((e @ eti_emb.T).max()) if eti_emb is not None else None
            if ref_emb is not None:
                best_ref = max(best_ref or 0, float((e @ ref_emb.T).max()))
            note = s_eti if s_eti is not None else p_eti
            notes.append((note, u, img))
            if meilleur is None or note > meilleur[0]:
                meilleur = (note, u, p_eti, s_eti)
        x["ref_toutes"] = best_ref
        x["eti"] = meilleur
        x["eti_ok"] = bool(meilleur and (meilleur[2] >= 0.5 or (meilleur[3] or 0) >= 0.80))
        # Lecture du code sur les 3 photos qui ressemblent le plus à une étiquette
        x["code"] = (None, "")
        if codes:
            for note, u, img in sorted(notes, key=lambda t: t[0], reverse=True)[:3]:
                try:
                    etat = verifier_code(lire_texte(img), codes)
                except Exception as e:
                    print(f"OCR impossible : {e}")
                    break
                if etat[0] == "ok":
                    x["code"] = etat
                    x["eti"] = (note, u) + tuple(x["eti"][2:]) if x["eti"] else (note, u, 0, None)
                    break
                if etat[0] == "autre" and x["code"][0] is None:
                    x["code"] = etat
        bilan.append(x)
        print(f"[étiquette] {x['id']} : {len(urls)} photos, étiquette {'oui' if x['eti_ok'] else 'non'}"
              + (f" (proba {meilleur[2]:.2f}, ressemblance étiquette {meilleur[3]})" if meilleur else ""))

    ordre = {"ok": 0, None: 1, "autre": 2}
    bilan.sort(key=lambda x: (ordre[x["code"][0]], not x["eti_ok"], -(x["ref_toutes"] or 0)))
    avec = sum(x["eti_ok"] for x in bilan)
    bons = sum(x["code"][0] == "ok" for x in bilan)
    lues = sum(x["nb_photos"] > 0 for x in bilan)
    post(webhook, {"content": (
        f"**Étape 2 — étiquette** : {len(bilan)} annonce(s) jugées OUI ouvertes "
        f"({lues} pages lues), {avec} avec une photo d'étiquette repérée.\n"
        + (f"Code attendu : {', '.join(codes)} — trouvé dans {bons} annonce(s).\n" if codes else "")
        + (controle_ref + "\n" if controle_ref else "") +
        "Grande image = la photo qui ressemble le plus à une étiquette. Petite image = 1re photo. "
        "« Ressemblance (toutes photos) » = meilleure ressemblance sur toutes les photos de l'annonce.")})
    embeds = []
    for x in bilan:
        champs = [
            {"name": "Prix", "value": x["price"], "inline": True},
            {"name": "Photos", "value": str(x["nb_photos"]), "inline": True},
        ]
        et, lu = x["code"]
        statut = (f"trouvée, lisible : {lu} = BON CODE" if et == "ok" else
                  f"trouvée, lisible : {lu} = AUTRE CODE" if et == "autre" else
                  "trouvée, mais code illisible : à vérifier à l'oeil" if x["eti_ok"] else
                  "pas de photo d'étiquette : à demander au vendeur")
        champs.append({"name": "Étiquette", "value": statut, "inline": False})
        if x["ref_toutes"] is not None:
            champs.insert(1, {"name": "Ressemblance (toutes photos)", "value": f"{x['ref_toutes']:.3f}", "inline": True})
        if x["eti"]:
            v = f"{x['eti'][2]:.0%}" + (f" · proche de ton étiquette {x['eti'][3]:.3f}" if x["eti"][3] is not None else "")
            champs.append({"name": "Score étiquette", "value": v, "inline": False})
        e = {"title": (("[T] " if x.get("temoin") else "") + x["title"])[:250], "url": x["url"],
             "color": 0x2ECC71 if x["code"][0] == "ok" else 0xE74C3C if x["code"][0] == "autre"
                      else 0x3498DB if x["eti_ok"] else 0x95A5A6, "fields": champs,
             "thumbnail": {"url": x["photo"]}}
        if x["eti"]:
            e["image"] = {"url": x["eti"][1]}
        embeds.append(e)
    for i in range(0, len(embeds), 4):   # 4 par message pour garder des grandes images lisibles
        post(webhook, {"embeds": embeds[i:i + 4]})


if __name__ == "__main__":
    main()
