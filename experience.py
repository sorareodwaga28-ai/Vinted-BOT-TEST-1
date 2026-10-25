"""Expérience : un classifieur entraîné reconnaît-il mieux la paire que la ressemblance actuelle ?

Vérité = réponses OUI / NON de Bastien (verdicts/<slug>.json).
Compare :
  A. ressemblance actuelle (photo la plus proche d'une référence)
  B. classifieur entraîné SANS les réponses de Bastien (titres auto-étiquetés + références) = calibrage tout seul
  C. classifieur entraîné AVEC ses réponses (validation croisée)
Variantes : image seule, image + couleurs.
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import open_clip
import requests
import torch
from PIL import Image
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import StratifiedKFold

import bot
import photo_test as pt

MODELE = os.environ.get("MODELE") or "Midnight Navy"
s = pt.slug(MODELE)
ICI = Path(__file__).parent
COLORIS = ("military", "cement", "oreo", "fire", "bred", "black cat", "thunder", "lightning", "sail", "seafoam", "canyon",
           "craft", "olive", "infrared", "black canvas", "sulfur", "sulphur", "university", "unc", "pine", "frozen",
           "metallic", "purple", "red", "green", "orange", "pink", "yellow", "sb", "travis", "off white", "kaws",
           "union", "taupe", "lakers", "cool grey", "net", "photon", "white thunder", "cactus", "denim", "pure money")


def autre_coloris(titre):
    t = " " + re.sub(r"[^a-z0-9 ]+", " ", bot.norm(titre)) + " "
    return any(f" {c} " in t for c in COLORIS)


def lire(session, url, pages):
    base, purl = bot.page_url(url)
    out = []
    for pg in range(1, pages + 1):
        try:
            out += [it for it in bot.fetch_items(session, base, purl + (f"&page={pg}" if pg > 1 else "")) if it.get("photo")]
        except Exception as e:
            print(f"page {pg} : {e}")
            break
        time.sleep(2)
    return out


def main():
    groupes = [g.strip() for g in pt.MOTS[MODELE].split(",") if g.strip()]
    v = json.loads((ICI / "verdicts" / f"{s}.json").read_text())["verdicts"]
    vrai = {}
    for e in ["V1", "V2", "V3", "V4", "V5"]:
        f = ICI / "batteries" / s / f"{e}.json"
        if f.exists():
            for a in json.loads(f.read_text())["annonces"]:
                r = v.get(f"{e}:{a['id']}")
                if r:
                    vrai[a["id"]] = (a, r == "oui")
    print(f"Réponses de Bastien : {len(vrai)} ({sum(o for _, o in vrai.values())} OUI)")

    # Annonces auto-étiquetées par le titre (jamais celles jugées par Bastien)
    session = bot.get_session()
    q = requests.utils.quote
    auto = {}
    cat = "https://www.vinted.fr/catalog?catalog[]=1242&order=relevance&search_text="
    for it in lire(session, cat + q(f"jordan 4 {MODELE}"), 4):
        if bot.matches_required(it, groupes) and not autre_coloris(it["title"]):
            auto[str(it["id"])] = (it, True)
    for txt in ("jordan 4", "jordan 4 military blue", "jordan 4 white", "jordan 4 retro"):
        for it in lire(session, cat + q(txt), 3):
            if not bot.matches_required(it, groupes) and autre_coloris(it["title"]):
                auto.setdefault(str(it["id"]), (it, False))
    for i in list(auto):
        if i in vrai:
            del auto[i]
    print(f"Auto-étiquetées : {len(auto)} ({sum(o for _, o in auto.values())} positives)")

    model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    model.eval()

    def couleurs(img):
        h = np.asarray(img.convert("HSV").resize((96, 96))).reshape(-1, 3).astype(float)
        hh = np.histogram2d(h[:, 0], h[:, 1], bins=[12, 3], range=[[0, 256], [0, 256]])[0].ravel()
        return hh / hh.sum()

    def feats(urls):
        E, C, ok = [], [], []
        for k, u in enumerate(urls):
            try:
                img = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
            except Exception:
                ok.append(False)
                continue
            with torch.no_grad():
                f = model.encode_image(prep(img).unsqueeze(0))
            E.append((f / f.norm(dim=-1, keepdim=True))[0].numpy())
            C.append(couleurs(img))
            ok.append(True)
            if k % 50 == 0:
                print(f"{k}/{len(urls)}")
        return np.array(E), np.array(C), np.array(ok)

    refs = [Image.open(p).convert("RGB") for p in sorted((ICI / "references" / s).glob("*")) if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    pieges = [Image.open(p).convert("RGB") for p in sorted((ICI / "references" / s / "pas-ca").glob("*")) if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    with torch.no_grad():
        R = model.encode_image(torch.stack([prep(i) for i in refs])); R = (R / R.norm(dim=-1, keepdim=True)).numpy()
    Rc = np.array([couleurs(i) for i in refs])

    vi = list(vrai)
    Ev, Cv, okv = feats([vrai[i][0]["photo"] for i in vi])
    yv = np.array([vrai[i][1] for i in vi])[okv]
    ai = list(auto)
    Ea, Ca, oka = feats([auto[i][0]["photo"] for i in ai])
    ya = np.array([auto[i][1] for i in ai])[oka]

    print("\n=== Résultats (AUC : 0,5 = hasard, 1 = parfait) ===")
    base = (Ev @ R.T).max(1)
    print(f"A. ressemblance actuelle : AUC {roc_auc_score(yv, base):.3f}")

    def rapport(nom, p):
        auc = roc_auc_score(yv, p)
        # meilleur seuil gardant >= 90 % de bonnes alertes
        best = None
        for t in sorted(set(np.round(p, 3))):
            m = p >= t
            if m.sum() == 0:
                continue
            prec = yv[m].mean()
            rec = yv[m].sum() / yv.sum()
            if prec >= 0.9 and (best is None or rec > best[1]):
                best = (t, rec, int(m.sum()), int((~yv[m]).sum()))
        b = (f"à 90 % de bonnes alertes : {best[1]*100:.0f} % des vraies paires trouvées ({best[2]} alertes, {best[3]} fausses)"
             if best else "jamais 90 % de bonnes alertes")
        print(f"{nom} : AUC {auc:.3f} · {b}")

    rapport("A. ressemblance actuelle", base)
    for nom, Xa, Xv, Xr in (("image", Ea, Ev, R), ("image+couleurs", np.hstack([Ea, Ca * 3]), np.hstack([Ev, Cv * 3]), np.hstack([R, Rc * 3]))):
        X = np.vstack([Xa, Xr]); y = np.concatenate([ya, np.ones(len(Xr), bool)])
        clf = LogisticRegression(C=2.0, max_iter=2000, class_weight="balanced").fit(X, y)
        rapport(f"B. calibrage tout seul ({nom})", clf.predict_proba(Xv)[:, 1])
        p = np.zeros(len(yv))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(Xv, yv):
            Xt = np.vstack([X, Xv[tr]]); yt = np.concatenate([y, yv[tr]])
            w = np.concatenate([np.ones(len(y)), np.full(len(tr), 3.0)])
            c = LogisticRegression(C=2.0, max_iter=2000, class_weight="balanced").fit(Xt, yt, sample_weight=w)
            p[te] = c.predict_proba(Xv[te])[:, 1]
        rapport(f"C. avec tes réponses ({nom})", p)


if __name__ == "__main__":
    main()
