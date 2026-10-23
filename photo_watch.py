"""Surveillance photo : toutes les 5 minutes, nouvelles annonces Vinted (sans texte),
comparées aux photos de référence des modèles suivis. Alerte Discord si la photo
atteint le seuil et que le prix est sous le prix max du modèle.

Réglages (variables d'environnement) :
  MODELES   modèles suivis, séparés par des virgules (défaut : Fire Red)
  SEUIL     seuil de ressemblance (défaut 0.84)
  PRIX      prix max propres à la surveillance, ex. "Vivid Sulfur=30; Fire Red=60"
            (sinon le prix du bot)
  PAGES     pages max lues par passage (défaut 3) ; on s'arrête dès qu'on retombe sur du déjà vu
  INTERVALLE secondes entre deux vérifications (défaut 90)
  DUREE     minutes de surveillance continue (défaut 0 = un seul passage)
  GIT_SAVE  1 = enregistrer photo_seen.json dans le repo toutes les 5 min
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

ETAT = Path(__file__).with_name("photo_seen.json")
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


def post(webhook, payload):
    r = requests.post(webhook, json=payload, timeout=20)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        requests.post(webhook, json=payload, timeout=20)
    time.sleep(1)


def git_sauver():
    """Enregistre photo_seen.json dans le repo (pendant les longues surveillances)."""
    import shutil
    import subprocess as sp
    tmp = "/tmp/photo_seen.json"
    shutil.copy(ETAT, tmp)
    for _ in range(5):
        sp.run(["git", "fetch", "-q", "origin", "main"])
        sp.run(["git", "reset", "-q", "--hard", "origin/main"])
        shutil.copy(tmp, ETAT)
        sp.run(["git", "add", ETAT.name])
        if sp.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        sp.run(["git", "commit", "-q", "-m", "maj annonces vues (photo)"])
        if sp.run(["git", "push", "-q", "origin", "HEAD:main"]).returncode == 0:
            return
        time.sleep(3)


def main():
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+",
                  os.environ.get("DISCORD_WEBHOOK", ""))
    if not m:
        sys.exit("DISCORD_WEBHOOK manquant ou invalide.")
    webhook = m.group(0)
    modeles = [x.strip() for x in (os.environ.get("MODELES") or "Fire Red").split(",") if x.strip()]
    for morceau in re.split(r"[;\n]", os.environ.get("PRIX") or ""):
        if "=" in morceau:
            nom, val = (t.strip() for t in morceau.split("=", 1))
            if nom in pt.PRIX:
                pt.PRIX[nom] = float(val.replace(",", "."))
    modeles = [x for x in modeles if x in pt.PRIX]
    seuil = float((os.environ.get("SEUIL") or "0.84").replace(",", "."))
    pages = int(os.environ.get("PAGES") or 3)
    pause = int(os.environ.get("INTERVALLE") or 90)          # secondes entre deux vérifications
    duree = int(os.environ.get("DUREE") or 0) * 60           # minutes de surveillance (0 = un seul passage)
    git_auto = os.environ.get("GIT_SAVE") == "1"
    if not modeles:
        sys.exit("Aucun modèle valide dans MODELES.")

    etat = json.loads(ETAT.read_text()) if ETAT.exists() else {"ids": [], "modeles": []}
    vus = set(etat["ids"])
    prix_haut = int(max(pt.PRIX[x] for x in modeles))
    base, purl = bot.page_url(pt.BASE_URL + f"&price_to={prix_haut}")
    session = bot.get_session()

    def sauver():
        etat["modeles"] = sorted(set(etat["modeles"]) | set(modeles))
        ETAT.write_text(json.dumps(etat))

    def nouvelles(premier):
        items, deja = [], False
        for pg in range(1, pages + 1):
            try:
                lot = bot.fetch_items(session, base, purl + (f"&page={pg}" if pg > 1 else ""))
            except Exception as e:
                print(f"Page {pg} illisible : {e}")
                break
            for it in lot:
                iid = str(it["id"])
                if iid in vus:
                    deja = True
                    continue
                vus.add(iid)
                etat["ids"].append(iid)
                if it.get("photo"):
                    items.append(it)
            if deja or premier:
                break
            time.sleep(3)
        etat["ids"] = etat["ids"][-20000:]
        return items

    nouveaux_modeles = [x for x in modeles if x not in etat["modeles"]]
    if not etat["ids"] or nouveaux_modeles:
        nouvelles(premier=not etat["ids"])
        sauver()
        if git_auto:
            git_sauver()
        post(webhook, {"content": "**Surveillance photo démarrée** : " + ", ".join(
            f"{x} (≤ {pt.PRIX[x]:g} €)" for x in modeles)
            + f" · seuil {seuil:.2f} · vérification toutes les {pause} s, nouvelles annonces seulement."})

    # CLIP + références de chaque modèle (chargés une seule fois)
    model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    tok = open_clip.get_tokenizer("ViT-B-32")
    model.eval()

    def emb(imgs):
        with torch.no_grad():
            f = model.encode_image(torch.stack([prep(i) for i in imgs]))
        return f / f.norm(dim=-1, keepdim=True)

    refs, etis = {}, {}
    for x in modeles:
        d = Path("references") / pt.slug(x)
        r = charger(d)
        if not r:
            print(f"Pas de photo de référence pour {x} : ignoré")
            continue
        refs[x] = emb(r)
        e = charger(d / "etiquette")
        etis[x] = emb(e) if e else None
    with torch.no_grad():
        te = model.encode_text(tok(["a close-up photo of the size label tag inside a sneaker, with barcode and small printed text",
                                    "a photo of sneakers", "a photo of the sole of a sneaker", "a photo of a shoe box"]))
    te = te / te.norm(dim=-1, keepdim=True)

    def passage():
        items = nouvelles(premier=False)
        print(f"{time.strftime('%H:%M:%S')} : {len(items)} nouvelle(s) annonce(s)")
        if not items:
            return
        # 3. Étape 1 : la photo ressemble-t-elle à un modèle suivi, dans son prix ?
        alertes = []
        for it in items:
            try:
                img = Image.open(io.BytesIO(session.get(it["photo"], timeout=20).content)).convert("RGB")
            except Exception as e:
                print(f"Photo illisible {it['id']} : {e}")
                continue
            e = emb([img])
            prix = bot.price_value(it) or 0
            meilleur = None
            for x, r in refs.items():
                s = float((e @ r.T).max())
                if s >= seuil and prix <= pt.PRIX[x] and (meilleur is None or s > meilleur[1]):
                    meilleur = (x, s)
            if meilleur:
                it["modele"], it["score"] = meilleur
                alertes.append(it)
        print(f"{len(alertes)} alerte(s)")

        # 4. Étape 2 : étiquette dans les autres photos, puis envoi
        for it in alertes:
            x = it["modele"]
            codes = pt.CODES.get(x, [])
            eti_url, statut = None, "pas de photo d'étiquette : à demander au vendeur"
            try:
                urls = pt.photos_annonce(session, it)
            except Exception as ex:
                print(f"Annonce illisible {it['id']} : {ex}")
                urls = []
            notes = []
            for u in urls:
                try:
                    im = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
                except Exception:
                    continue
                f = emb([im])
                p_eti = float((100 * f @ te.T).softmax(dim=-1)[0][0])
                s_eti = float((f @ etis[x].T).max()) if etis.get(x) is not None else 0
                notes.append((max(p_eti, s_eti), p_eti >= 0.5 or s_eti >= 0.80, u, im))
            notes.sort(key=lambda t: t[0], reverse=True)
            if notes and notes[0][1]:
                eti_url = notes[0][2]
                statut = "trouvée, mais code illisible : à vérifier à l'oeil"
            if codes:
                for _, _, u, im in notes[:3]:
                    try:
                        et, lu = pt.verifier_code(pt.lire_texte(im), codes)
                    except Exception as ex:
                        print(f"OCR impossible : {ex}")
                        break
                    if et == "ok":
                        eti_url, statut = u, f"trouvée, lisible : {lu} = BON CODE"
                        break
                    if et == "autre":
                        eti_url, statut = u, f"trouvée, lisible : {lu} = AUTRE CODE"
            titre_ok = bot.matches_required(it, [g.strip() for g in pt.MOTS.get(x, "").split(",") if g.strip()])
            embeds = [{
                "title": it["title"][:250], "url": it["url"], "color": 0x2ECC71,
                "author": {"name": f"Photo : {x}"},
                "fields": [
                    {"name": "Prix", "value": it["price"], "inline": True},
                    {"name": "Ressemblance", "value": f"{it['score']:.3f}", "inline": True},
                    {"name": "Taille · état", "value": it.get("details", "?"), "inline": True},
                    {"name": "Titre", "value": "dit le modèle" if titre_ok else "ne le dit pas (trouvée par la photo)", "inline": True},
                    {"name": "Étiquette", "value": statut, "inline": False},
                ],
                "image": {"url": it["photo"]},
            }]
            if eti_url:
                embeds.append({"url": it["url"], "image": {"url": eti_url}})
            post(webhook, {"embeds": embeds})

    debut = time.time()
    dernier_git = time.time()
    while True:
        t0 = time.time()
        try:
            passage()
        except Exception as e:
            print(f"Erreur pendant le passage : {e}")
        sauver()
        if git_auto and time.time() - dernier_git > 300:
            git_sauver()
            dernier_git = time.time()
        if time.time() - debut + pause > duree:
            break
        time.sleep(max(5, pause - (time.time() - t0)))
    if git_auto:
        git_sauver()


if __name__ == "__main__":
    main()
