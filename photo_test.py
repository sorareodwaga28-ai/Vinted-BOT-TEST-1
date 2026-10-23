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

BASE_URL = ("https://www.vinted.fr/catalog?search_text=jordan&catalog[]=1242&currency=EUR"
            + "".join(f"&size_ids[]={s}" for s in range(780, 796))
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
    "autre Jordan": "Jordan sneakers of another model or colorway",
    "pas une paire": "a photo that does not show sneakers",
}

# Mots obligatoires actuels du bot, pour comparer titre et photo.
MOTS = {
    "Vivid Sulfur": "jordan 4 + sulfur, jordan 4 + sulphur, jordan4 + sulfur, jordan4 + sulphur, j4 + sulfur, j4 + sulphur, aj4 + sulfur, aj4 + sulphur, jordan iv + sulfur, jordan iv + sulphur",
    "Fire Red": "jordan 4 + fire + red, jordan 4 + firered, jordan4 + fire + red, jordan4 + firered, j4 + fire + red, j4 + firered, aj4 + fire + red, aj4 + firered, jordan iv + fire + red, jordan iv + firered",
    "Infrared": "jordan 4 + infrared, jordan 4 + infra + red, jordan4 + infrared, jordan4 + infra + red, j4 + infrared, j4 + infra + red, aj4 + infrared, aj4 + infra + red, jordan iv + infrared, jordan iv + infra + red",
    "Black Canvas": "jordan 4 + black + canvas, jordan4 + black + canvas, j4 + black + canvas, aj4 + black + canvas, jordan iv + black + canvas",
    "Midnight Navy": "jordan 4 + midnight + navy, jordan4 + midnight + navy, j4 + midnight + navy, aj4 + midnight + navy, jordan iv + midnight + navy",
    "Seafoam": "jordan 4 + seafoam, jordan 4 + sea + foam, jordan4 + seafoam, jordan4 + sea + foam, j4 + seafoam, j4 + sea + foam, aj4 + seafoam, aj4 + sea + foam, jordan iv + seafoam, jordan iv + sea + foam",
    "Canyon Purple": "jordan 4 + canyon, jordan4 + canyon, j4 + canyon, aj4 + canyon, jordan iv + canyon",
    "Craft Olive": "jordan 4 + craft + olive, jordan4 + craft + olive, j4 + craft + olive, aj4 + craft + olive, jordan iv + craft + olive",
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", bot.norm(s)).strip("-")


def post(webhook, payload):
    r = requests.post(webhook, json=payload, timeout=20)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        requests.post(webhook, json=payload, timeout=20)
    time.sleep(1)


def main():
    modele = os.environ.get("MODELE") or "Vivid Sulfur"
    prix_max = (os.environ.get("PRIX_MAX") or "").strip()
    pages = int(os.environ.get("PAGES") or 3)
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+",
                  os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None
    if modele not in COLORIS:
        sys.exit(f"Modèle inconnu : {modele}")

    # 1. Lire les annonces
    url = BASE_URL + (f"&price_to={prix_max}" if prix_max else "")
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
    print(f"{len(items)} annonces avec photo")
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
    mode = f"{len(refs)} photo(s) de référence" if refs else "description texte seule (aucune photo de référence)"
    print(f"Mode : {mode}")

    # 4. Analyser chaque annonce
    groupes = [g.strip() for g in MOTS[modele].split(",")]
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

    key = "ref" if ref_emb is not None else "prob"
    res.sort(key=lambda x: x[key], reverse=True)

    # 5. Résultats complets (fichier téléchargeable)
    with open("resultats.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rang", "score_reference", f"proba_{modele}", "clip_pense", "titre_mentionne_modele", "prix", "titre", "lien"])
        for n, x in enumerate(res, 1):
            w.writerow([n, "" if x["ref"] is None else f"{x['ref']:.3f}", f"{x['prob']:.2f}",
                        x["devine"], "oui" if x["titre_ok"] else "non", x["price"], x["title"], x["url"]])
    for n, x in enumerate(res[:30], 1):
        print(f"{n:>3} | ref {x['ref'] if x['ref'] is None else round(x['ref'], 3)} | {x['devine']} {x['devine_prob']:.0%} | titre {'OK' if x['titre_ok'] else '--'} | {x['title'][:60]}")

    if not webhook:
        print("Pas de webhook Discord : résultats seulement dans les logs.")
        return

    # 6. Discord
    devine_cible = [x for x in res if x["devine"] == modele]
    caches = [x for x in devine_cible if not x["titre_ok"]]
    titre_ok = sum(x["titre_ok"] for x in res)
    lignes = [
        f"**Test photo — {modele}**",
        f"Annonces analysées : {len(res)} (recherche « jordan », {pages} page(s), "
        + (f"prix max {prix_max} €)" if prix_max else "tous les prix)"),
        f"Mode : {mode}",
        f"Titre qui mentionne le modèle : {titre_ok}",
        f"CLIP pense que c'est une {modele} : {len(devine_cible)}, dont {len(caches)} avec un titre qui ne le dit pas",
    ]
    if ref_emb is not None:
        for s in (0.90, 0.85, 0.80, 0.75):
            lignes.append(f"Score de ressemblance ≥ {s:.2f} : {sum(x['ref'] >= s for x in res)}")
    lignes.append("Ci-dessous : les 10 annonces les plus ressemblantes, puis les 5 suivantes (zone de doute).")
    post(webhook, {"content": "\n".join(lignes)})

    def embed(x, n, couleur):
        champs = [
            {"name": "Prix", "value": x["price"], "inline": True},
            {"name": "CLIP pense", "value": f"{x['devine']} ({x['devine_prob']:.0%})", "inline": True},
            {"name": "Titre", "value": "mentionne le modèle" if x["titre_ok"] else "ne le dit pas", "inline": True},
        ]
        if x["ref"] is not None:
            champs.insert(0, {"name": "Ressemblance", "value": f"{x['ref']:.3f}", "inline": True})
        return {"title": f"#{n} · {x['title']}"[:250], "url": x["url"], "color": couleur,
                "fields": champs, "thumbnail": {"url": x["photo"]}}

    post(webhook, {"embeds": [embed(x, n, 0x2ECC71) for n, x in enumerate(res[:10], 1)]})
    if len(res) > 10:
        post(webhook, {"content": "**Zone de doute (rangs 11 à 15)**",
                       "embeds": [embed(x, n, 0xF39C12) for n, x in enumerate(res[10:15], 11)]})


if __name__ == "__main__":
    main()
