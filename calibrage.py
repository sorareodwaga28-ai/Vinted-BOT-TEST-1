"""Calibrage automatique d'un modèle (remplace les batteries en manches lentes).

1. Exemples automatiques : annonces dont le titre nomme la paire (bonnes) ou un autre coloris (mauvaises),
   photos de référence (bonnes) et pièges pas-ca/ (mauvaises), plus toutes les réponses OUI / NON de Bastien
   (verdicts/<slug>.json, poids 3).
2. Entraîne un classifieur sur les images (CLIP + régression logistique) et choisit seul son seuil :
   le plus de vraies paires possible avec au moins 90 % de bonnes alertes.
3. Prépare un lot d'annonces à faire juger (calibrage/<slug>.json), dans lequel l'onglet « Calibrage »
   d'Odwagasnkrs pioche les manches V1 à V5 instantanément. Jamais une annonce déjà jugée.
4. Enregistre le classifieur pour la surveillance photo (modeles/<slug>.json).

Réglage : MODELE (nom exact).
"""
import hashlib
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
from titres import MARQUES, autre_marque, norm_titre

ICI = Path(__file__).parent
IMG = (".jpg", ".jpeg", ".png", ".webp")
PRECISION = 0.90
COLORIS = ("military", "cement", "oreo", "fire", "bred", "black cat", "thunder", "lightning", "sail", "seafoam", "canyon",
           "craft", "olive", "infrared", "black canvas", "sulfur", "sulphur", "university", "unc", "pine", "frozen",
           "metallic", "sb", "travis", "off white", "kaws",
           "union", "taupe", "lakers", "cool grey", "photon", "cactus", "denim", "pure money", "midnight navy")
VIDES = set("jordan air nike retro aj4 j4 jordan4 iv og taille size pointure eu us uk fr homme femme neuf "
            "neuve tres bon etat vintage paire baskets basket sneakers sneaker chaussures chaussure de du la le "
            "et a en avec sans boite original originale originales authentique".split())


def autre_coloris(titre, modele):
    """Le titre nomme un autre coloris (et pas les mots du modèle)."""
    t = norm_titre(titre)
    mots = set(bot.norm(modele).split())
    return any(f" {c} " in t for c in COLORIS if not (set(c.split()) & mots))


def autre_modele(it, modele):
    for x, g in pt.MOTS.items():
        if x != modele and bot.matches_required(it, [s.strip() for s in g.split(",") if s.strip()]):
            return x
    return ""


def lire(session, url, pages, source, max_prix=None):
    base, purl = bot.page_url(url)
    out = []
    for pg in range(1, pages + 1):
        try:
            lot = bot.fetch_items(session, base, purl + (f"&page={pg}" if pg > 1 else ""))
        except Exception as e:
            print(f"{source} page {pg} : {e}")
            break
        for it in lot:
            if it.get("photo") and (max_prix is None or (bot.price_value(it) or 0) <= max_prix):
                it["source"] = source
                out.append(it)
        time.sleep(2)
    print(f"{source} : {len(out)}")
    return out


def charger_verdicts(s):
    """{id: {"rep", "photo", "titre", ...}} ; migre l'ancien format « V1:id » avec batteries/<slug>/V*.json."""
    f = ICI / "verdicts" / f"{s}.json"
    if not f.exists():
        return {}
    brut = json.loads(f.read_text()).get("verdicts", {})
    anciens = {}
    for p in sorted((ICI / "batteries" / s).glob("V*.json")):
        for a in json.loads(p.read_text())["annonces"]:
            anciens.setdefault(a["id"], a)
    out = {}
    for k, x in brut.items():
        if isinstance(x, dict):
            out[k] = x
            continue
        e, i = k.split(":", 1) if ":" in k else ("V1", k)
        a = anciens.get(i)
        if a and str(x).lower() in ("oui", "non"):
            out[i] = {"rep": str(x).lower(), "etape": e, "photo": a["photo"], "titre": a["titre"], "url": a["url"],
                      "prix": a.get("prix"), "titre_ok": a.get("titre_ok", False)}
    return out


def mots_des_oui(titres, modele):
    exclus = VIDES | set(bot.norm(modele).split())
    c = {}
    for t in titres:
        for w in set(norm_titre(t).split()):
            if len(w) >= 3 and not w.isdigit() and w not in exclus:
                c[w] = c.get(w, 0) + 1
    return [w for w, n in sorted(c.items(), key=lambda kv: -kv[1])[:3] if n >= 2]


def seuil_auto(p, y):
    """Seuil gardant le plus de vraies paires avec au moins 90 % de bonnes alertes."""
    best = None
    for t in sorted(set(np.round(p, 3))):
        m = p >= t
        if m.sum() < 2:
            continue
        prec, rec = y[m].mean(), y[m].sum() / max(1, y.sum())
        if prec >= PRECISION and (best is None or rec > best[1] or (rec == best[1] and t < best[0])):
            best = (float(t), float(rec), float(prec))
    return best


def main():
    modele = os.environ.get("MODELE", "").strip()
    if modele not in pt.PRIX:
        sys.exit(f"Modèle inconnu : {modele!r} (à ajouter dans photo_test.py : PRIX, COLORIS, MOTS)")
    s = pt.slug(modele)
    prix = float(pt.PRIX[modele])
    groupes = [g.strip() for g in pt.MOTS.get(modele, "").split(",") if g.strip()]
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+", os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None
    verdicts = charger_verdicts(s)
    juges = set(verdicts)
    nb_oui = sum(1 for x in verdicts.values() if x["rep"] == "oui")
    print(f"{modele} : {len(verdicts)} réponses ({nb_oui} OUI)")

    # 1. Annonces
    session = bot.get_session()
    q = requests.utils.quote
    cat = "https://www.vinted.fr/catalog?catalog[]=1242&order=relevance&search_text="
    temoins = lire(session, cat + q(f"jordan 4 {modele}"), 4, "titre de la paire")
    temoins += lire(session, pt.BASE_URL + "&search_text=" + q(f"jordan 4 {modele}"), 2, "titre de la paire, récentes")
    negatifs = []
    for txt in ("jordan 4", "jordan 4 retro", "jordan 4 white", "air jordan"):
        negatifs += lire(session, cat + q(txt), 3, f"autres « {txt} »")
    idees = mots_des_oui([x["titre"] for x in verdicts.values() if x["rep"] == "oui"], modele)
    # Jusqu'au prix max + 20 € : plus d'annonces proches du seuil à faire juger
    pmax2 = prix + 20
    candidats = lire(session, pt.BASE_URL + f"&price_to={int(pmax2)}", 6, "sans texte", pmax2)
    candidats += lire(session, pt.BASE_URL + f"&price_to={int(pmax2)}&search_text=" + q("jordan 4"), 5, "« jordan 4 »", pmax2)
    for w in idees:
        candidats += lire(session, pt.BASE_URL + f"&price_to={int(pmax2)}&search_text=" + q(f"jordan 4 {w}"), 1, f"« jordan 4 {w} » (tiré de tes OUI)", pmax2)

    tous = {}
    for it in temoins + negatifs + candidats:
        tous.setdefault(str(it["id"]), it)
    for i, it in tous.items():
        it["titre_ok"] = bot.matches_required(it, groupes)
        it["autre"] = autre_modele(it, modele) or ("autre coloris" if autre_coloris(it["title"], modele) else "")
        it["marque"] = (not it["titre_ok"]) and autre_marque(it["title"])
        it["prix_val"] = bot.price_value(it) or 0

    # 2. Images → vecteurs (cache entre les lancements)
    model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    tok = open_clip.get_tokenizer("ViT-B-32")
    model.eval()
    cache_f = ICI / ".cache_emb" / f"{s}.npz"
    cache = {}
    if cache_f.exists():
        z = np.load(cache_f)
        cache = {k: z[k] for k in z.files}

    def cle(u):
        return hashlib.md5(u.split("?")[0].encode()).hexdigest()

    def emb_img(img):
        with torch.no_grad():
            f = model.encode_image(prep(img).unsqueeze(0))
        return (f / f.norm(dim=-1, keepdim=True))[0].numpy()

    def emb_url(u):
        k = cle(u)
        if k not in cache:
            try:
                img = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
            except Exception:
                return None
            cache[k] = emb_img(img)
        return cache[k]

    def emb_dossier(d):
        out = []
        for p in sorted(d.glob("*")) if d.exists() else []:
            if p.suffix.lower() in IMG:
                try:
                    out.append(emb_img(Image.open(p).convert("RGB")))
                except Exception:
                    pass
        return out

    refs = emb_dossier(ICI / "references" / s)
    pieges = emb_dossier(ICI / "references" / s / "pas-ca")
    if not refs:
        sys.exit("Aucune photo de référence.")

    n = 0
    for i, it in tous.items():
        it["emb"] = emb_url(it["photo"])
        n += 1
        if n % 100 == 0:
            print(f"{n}/{len(tous)} photos")
    for i, x in verdicts.items():
        x["emb"] = emb_url(x["photo"])
    cache_f.parent.mkdir(exist_ok=True)
    np.savez_compressed(cache_f, **cache)

    # 3. Entraînement
    X, y, w = [], [], []
    def ajouter(e, lab, poids):
        if e is not None:
            X.append(e); y.append(lab); w.append(poids)
    for e in refs:
        ajouter(e, True, 2.0)
    for e in pieges:
        ajouter(e, False, 2.0)
    auto_ids = []
    for i, it in tous.items():
        if i in juges:
            continue
        if it["source"].startswith("titre de la paire") and it["titre_ok"] and not it["autre"]:
            ajouter(it["emb"], True, 1.0); auto_ids.append(i)
        elif (it["autre"] or it["marque"]) and not it["titre_ok"]:
            ajouter(it["emb"], False, 1.0); auto_ids.append(i)
    n_auto = len(X)
    vids = [i for i, x in verdicts.items() if x.get("emb") is not None]
    Xv = np.array([verdicts[i]["emb"] for i in vids]) if vids else np.zeros((0, len(refs[0])))
    yv = np.array([verdicts[i]["rep"] == "oui" for i in vids], bool)
    X, y, w = np.array(X), np.array(y, bool), np.array(w)
    print(f"Exemples auto : {n_auto} ({int(y.sum())} bons) · réponses : {len(vids)} ({int(yv.sum())} OUI)")

    def fit(Xa, ya, wa):
        return LogisticRegression(C=2.0, max_iter=3000, class_weight="balanced").fit(Xa, ya, sample_weight=wa)

    # Probas honnêtes (validation croisée) pour choisir le seuil
    if yv.sum() >= 5 and (~yv).sum() >= 5:
        p_cv = np.zeros(len(yv))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(Xv, yv):
            c = fit(np.vstack([X, Xv[tr]]), np.concatenate([y, yv[tr]]), np.concatenate([w, np.full(len(tr), 3.0)]))
            p_cv[te] = c.predict_proba(Xv[te])[:, 1]
        base_seuil, y_seuil, origine = p_cv, yv, "tes réponses"
        E_eval = Xv
    else:
        p_cv = np.zeros(len(y))
        for tr, te in StratifiedKFold(5, shuffle=True, random_state=0).split(X, y):
            c = fit(X[tr], y[tr], w[tr])
            p_cv[te] = c.predict_proba(X[te])[:, 1]
        # on évalue sans les photos de référence / pièges elles-mêmes
        k = len(refs) + len(pieges)
        base_seuil, y_seuil, origine = p_cv[k:], y[k:], "exemples automatiques"
        E_eval = X[k:]
    auc = float(roc_auc_score(y_seuil, base_seuil)) if len(set(y_seuil)) == 2 else None
    b = seuil_auto(base_seuil, y_seuil)
    seuil = b[0] if b else 0.9

    # Comparaison avec l'ancienne méthode (ressemblance à la photo de référence la plus proche),
    # sur exactement les mêmes annonces : on garde la meilleure pour la surveillance.
    R = np.array(refs)
    s_old = (E_eval @ R.T).max(1)
    if pieges:
        s_old = np.where((E_eval @ np.array(pieges).T).max(1) > s_old, 0.0, s_old)
    auc_old = float(roc_auc_score(y_seuil, s_old)) if len(set(y_seuil)) == 2 else None
    b_old = seuil_auto(s_old, y_seuil)
    note = lambda bb, a: ((bb[1] if bb else 0.0), (a or 0.0))
    methode = "classifieur" if note(b, auc) >= note(b_old, auc_old) else "ressemblance"
    if not b and not b_old:
        methode = "ressemblance"      # aucune n'atteint 90 % de bonnes alertes : on garde l'ancienne réglée à la main
    comparaison = {
        "classifieur": {"auc": round(auc, 3) if auc else None, "rappel": round(b[1], 3) if b else None, "seuil": round(seuil, 3)},
        "ressemblance": {"auc": round(auc_old, 3) if auc_old else None, "rappel": round(b_old[1], 3) if b_old else None,
                         "seuil": round(b_old[0], 3) if b_old else None},
        "evaluee_sur": f"{len(y_seuil)} annonces ({origine})", "choisie": methode}
    print("Comparaison :", comparaison)
    clf = fit(np.vstack([X, Xv]) if len(vids) else X, np.concatenate([y, yv]) if len(vids) else y,
              np.concatenate([w, np.full(len(vids), 3.0)]) if len(vids) else w)
    print(f"Seuil {seuil:.3f} ({origine}) · AUC {auc} · {b}")

    # 4. Lot à juger
    for it in tous.values():
        it["proba"] = float(clf.predict_proba(it["emb"][None])[0, 1]) if it.get("emb") is not None else 0.0
    libres = [it for i, it in tous.items() if i not in juges and it.get("emb") is not None and not it["marque"]]
    # Seulement ce qui sert au calibrage : les annonces proches du seuil (là où le bot hésite)
    proche = lambda it: abs(it["proba"] - seuil)
    zone = [it for it in libres if proche(it) <= 0.15]
    dans_prix = sorted([it for it in zone if it["prix_val"] <= pmax2 and it["source"] != "titre de la paire"], key=proche)
    paire = sorted([it for it in zone if it["titre_ok"] and it["prix_val"] <= pmax2], key=proche)
    autres = sorted([it for it in zone if it["autre"] and not it["titre_ok"]], key=proche)
    lot, vus = [], set()
    for lst, nmax in ((dans_prix, 150), (paire, 30), (autres, 40)):
        for it in lst[:nmax]:
            if it["id"] not in vus:
                vus.add(it["id"]); lot.append(it)

    # Étiquette sur les annonces que le bot enverrait
    codes = pt.CODES.get(modele, [])
    etis = emb_dossier(ICI / "references" / s / "etiquette")
    E_eti = np.array(etis) if etis else None
    with torch.no_grad():
        te = model.encode_text(tok(["a close-up photo of the size label tag inside a sneaker, with barcode and small printed text",
                                    "a photo of sneakers", "a photo of the sole of a sneaker", "a photo of a shoe box"]))
    te = (te / te.norm(dim=-1, keepdim=True)).numpy()
    a_ouvrir = sorted([it for it in lot if it["prix_val"] <= pmax2 and (it["proba"] >= seuil - 0.05 or it["titre_ok"])], key=proche)[:30]
    for it in a_ouvrir:
        it["etiquette"], it["code_lu"], it["photo_etiquette"] = "absente", "", ""
        time.sleep(1.5)
        try:
            urls = pt.photos_annonce(session, it)
        except Exception:
            it["etiquette"] = "annonce illisible"
            continue
        notes = []
        for u in urls[:10]:
            try:
                im = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
            except Exception:
                continue
            f = emb_img(im)
            pe = float(np.exp(100 * f @ te.T)[0] / np.exp(100 * f @ te.T).sum())
            se = float((E_eti @ f).max()) if E_eti is not None else 0.0
            notes.append((max(pe, se), pe >= 0.5 or se >= 0.8, im, u))
        notes.sort(key=lambda t: -t[0])
        if notes and notes[0][1]:
            it["etiquette"], it["photo_etiquette"] = "illisible", notes[0][3]
        for _, _, im, u in notes[:2] if codes else []:
            try:
                et, lu = pt.verifier_code(pt.lire_texte(im), codes)
            except Exception:
                break
            if et == "ok":
                it["etiquette"], it["code_lu"], it["photo_etiquette"] = "bon code", lu, u
                break
            if et == "autre":
                it["etiquette"], it["code_lu"], it["photo_etiquette"] = "autre code", lu, u

    out = {
        "modele": modele, "slug": s, "date": time.strftime("%Y-%m-%d %H:%M", time.gmtime()), "prix_max": prix,
        "seuil": round(seuil, 3), "origine_seuil": origine, "methode": methode, "comparaison": comparaison, "auc": round(auc, 3) if auc else None,
        "rappel": round(b[1], 3) if b else None, "precision": round(b[2], 3) if b else None,
        "nb_reponses": len(verdicts), "nb_oui": nb_oui, "nb_exemples_auto": n_auto, "codes": codes,
        "recherches_tirees_des_oui": [f"jordan 4 {w}" for w in idees],
        "annonces": [{
            "id": str(it["id"]), "titre": it["title"], "url": it["url"], "photo": it["photo"],
            "prix": it["prix_val"], "prix_txt": it["price"], "details": it.get("details", ""),
            "proba": round(it["proba"], 4), "titre_ok": bool(it["titre_ok"]), "autre": it["autre"],
            "prix_ok": it["prix_val"] <= prix, "depasse": max(0.0, round(it["prix_val"] - prix, 2)), "source": it["source"],
            "etiquette": it.get("etiquette"), "code_lu": it.get("code_lu", ""), "photo_etiquette": it.get("photo_etiquette", ""),
        } for it in lot],
    }
    (ICI / "calibrage").mkdir(exist_ok=True)
    (ICI / "calibrage" / f"{s}.json").write_text(json.dumps(out, ensure_ascii=False))
    (ICI / "modeles").mkdir(exist_ok=True)
    (ICI / "modeles" / f"{s}.json").write_text(json.dumps({
        "modele": modele, "seuil": round(seuil, 4), "nb_oui": nb_oui, "auc": out["auc"],
        "coef": [round(float(c), 6) for c in clf.coef_[0]], "biais": float(clf.intercept_[0]), "date": out["date"],
        "methode": methode, "seuil_ressemblance": comparaison["ressemblance"]["seuil"], "comparaison": comparaison}))
    print(f"{len(lot)} annonces prêtes à juger")

    if webhook:
        r = (f"{out['rappel']*100:.0f} % des vraies paires trouvées par la photo" if out["rappel"] is not None else "pas encore assez d'exemples")
        requests.post(webhook, json={"content": (
            f"**Calibrage {modele}** · seuil {seuil:.2f} (calculé sur {origine}) · {r}, au moins 90 % de bonnes alertes\n"
            f"{len(lot)} annonces prêtes dans l'onglet Calibrage d'Odwagasnkrs.")}, timeout=20)


if __name__ == "__main__":
    main()
