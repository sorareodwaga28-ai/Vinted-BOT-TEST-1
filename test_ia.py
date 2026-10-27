"""Test de l'IA de vision sur les réponses OUI / NON de Bastien (vérité).

Compare, sur exactement les mêmes annonces :
  - ancienne méthode : ressemblance photo >= 0,84
  - IA seule
  - bot réel : présélection photo >= 0,70 (ou titre qui nomme la paire) PUIS IA
Résultat : experiences/test-ia-<slug>.txt (+ Discord).
"""
import glob
import io
import json
import os
import re
import time
from pathlib import Path

import open_clip
import requests
import torch
from PIL import Image

import bot
import ia
import photo_test as pt

MODELE = os.environ.get("MODELE") or "Midnight Navy"
s = pt.slug(MODELE)
ICI = Path(__file__).parent
CONF = int(os.environ.get("IA_CONFIANCE") or 70)
groupes = [g.strip() for g in pt.MOTS[MODELE].split(",") if g.strip()]

v = json.loads((ICI / "verdicts" / f"{s}.json").read_text())["verdicts"]
anc = {}
for p in sorted(glob.glob(f"batteries/{s}/V*.json")):
    for a in json.loads(Path(p).read_text())["annonces"]:
        anc.setdefault(a["id"], a)
cas = {}
for k, x in v.items():
    if isinstance(x, dict):
        cas[k] = {"rep": x["rep"], "photo": x["photo"], "titre": x.get("titre", ""), "url": x.get("url", "")}
    else:
        i = k.split(":")[-1]; a = anc.get(i)
        if a:
            cas[i] = {"rep": x, "photo": a["photo"], "titre": a["titre"], "url": a["url"]}
print(f"{len(cas)} réponses")
_k = os.environ.get("ANTHROPIC_API_KEY", "")
print(f"[diag] clé reçue : {'oui' if _k.strip() else 'NON (secret vide ou absent)'} · longueur {len(_k.strip())} · début {_k.strip()[:7]!r}")
autres = sorted(k for k in os.environ if "ANTHROP" in k.upper() or "CLAUDE" in k.upper())
print(f"[diag] variables liées : {autres}")
if not _k.strip():
    raise SystemExit("Pas de clé : vérifie le secret ANTHROPIC_API_KEY dans Repository secrets")

model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
model.eval()
def emb(im):
    with torch.no_grad():
        f = model.encode_image(prep(im).unsqueeze(0))
    return f / f.norm(dim=-1, keepdim=True)
refs_pil = [Image.open(p).convert("RGB") for p in sorted((ICI / "references" / s).glob("*")) if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
R = torch.cat([emb(r) for r in refs_pil])
session = bot.get_session()
codes = pt.CODES.get(MODELE, [])
voisins = [m for m in pt.PRIX if m != MODELE][:6]

res = []
for n, (i, c) in enumerate(cas.items(), 1):
    try:
        first = Image.open(io.BytesIO(session.get(c["photo"], timeout=20).content)).convert("RGB")
    except Exception:
        continue
    score = float((emb(first) @ R.T).max())
    photos = [first]
    try:
        urls = pt.photos_annonce(session, {"url": c["url"], "photo": c["photo"], "id": i})[1:4]
        for u in urls:
            photos.append(Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB"))
    except Exception:
        pass
    d = ia.verifier(pt.nom(MODELE), codes, refs_pil[:2], photos, None, c["titre"], [pt.nom(m) for m in voisins])
    if d.get("erreur"):
        print("ERREUR IA", d["erreur"])
        if "401" in d["erreur"] or "credit" in d["erreur"].lower() or "403" in d["erreur"]:
            break
        continue
    titre_ok = bot.matches_required({"title": c["titre"]}, groupes)
    res.append({"id": i, "vrai": c["rep"] == "oui", "score": score, "titre_ok": titre_ok, "ia": d})
    if n % 20 == 0:
        print(f"{n}/{len(cas)}")
    time.sleep(0.3)

def bilan(nom, pred):
    vp = sum(1 for r in res if pred(r) and r["vrai"]); fp = sum(1 for r in res if pred(r) and not r["vrai"])
    tot = sum(1 for r in res if r["vrai"])
    trouv = vp / tot if tot else 0; juste = vp / (vp + fp) if vp + fp else 0
    return f"{nom} : {trouv*100:.0f} % des vraies paires trouvées ({vp}/{tot}) · {juste*100:.0f} % d'alertes justes ({fp} fausse(s))"

ia_oui = lambda r: r["ia"]["verdict"] == "oui" and r["ia"]["confiance"] >= CONF
accord = sum(1 for r in res if ia_oui(r) == r["vrai"]) / max(1, len(res))
lignes = [f"**Test IA de vision — {MODELE}** ({len(res)} annonces jugées par Bastien, modèle {os.environ.get('IA_MODELE') or 'claude-haiku-5-5'})",
          bilan("Ancienne méthode (ressemblance ≥ 0,84)", lambda r: r["score"] >= 0.84),
          bilan("IA seule", ia_oui),
          bilan("Bot réel (présélection 0,70 ou titre, puis IA)", lambda r: (r["score"] >= 0.70 or r["titre_ok"]) and ia_oui(r)),
          f"Accord global IA / Bastien : {accord*100:.0f} %",
          "Désaccords (IA dit oui, toi non) : " + "; ".join(f"{cas[r['id']]['titre'][:35]} ({r['ia']['raison'][:40]})" for r in res if ia_oui(r) and not r["vrai"])[:900],
          "Ratées (toi oui, IA non) : " + "; ".join(f"{cas[r['id']]['titre'][:35]} ({r['ia']['verdict']} {r['ia']['confiance']}, {r['ia']['raison'][:40]})" for r in res if r["vrai"] and not ia_oui(r))[:900]]
txt = "\n".join(lignes)
print(txt)
(ICI / "experiences").mkdir(exist_ok=True)
(ICI / "experiences" / f"test-ia-{s}.txt").write_text(txt)
(ICI / "experiences" / f"test-ia-{s}.json").write_text(json.dumps(res, ensure_ascii=False))
m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+", os.environ.get("DISCORD_WEBHOOK", ""))
if m:
    requests.post(m.group(0), json={"content": txt[:1900]}, timeout=20)
