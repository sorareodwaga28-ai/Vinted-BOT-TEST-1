"""Apprentissage à partir des réponses OUI / NON de Bastien.

Lit verdicts/<modele>.json ({"modele", "batterie", "verdicts": {id: "oui"|"non"}})
et batteries/<modele>/resultats.json (scores, photos), puis :
  - NON avec une photo proche du seuil   -> photo ajoutée dans references/<modele>/pas-ca/
  - OUI avec une photo sous le seuil     -> photo ajoutée dans references/<modele>/ (si c'est bien
                                            la paire en photo : ressemblance >= 0,65)
  - nouveau seuil du modèle               -> seuils.json (lu par la surveillance photo)
  - bilan des vues V1 à V5                -> Discord
Les réponses déjà appliquées sont notées dans batteries/<modele>/appris.json.
"""
import io
import json
import os
import re
import sys
from pathlib import Path

import requests
from PIL import Image

ICI = Path(__file__).parent
MIN_OUI = 3            # réponses OUI nécessaires pour fixer un seuil
COUT_FAUSSE = 2        # une fausse alerte compte double par rapport à une paire ratée
BORNES = (0.72, 0.95)


def meilleur_seuil(notes):
    """notes = [(score, oui)]. Seuil qui garde le plus de OUI en évitant les NON."""
    if sum(1 for _, o in notes if o) < MIN_OUI:
        return None
    best = None
    for t in sorted({round(s, 3) for s, o in notes if o}):
        gain = sum(1 for s, o in notes if o and s >= t) - COUT_FAUSSE * sum(1 for s, o in notes if not o and s >= t)
        if best is None or gain > best[0] or (gain == best[0] and t > best[1]):
            best = (gain, t)
    t = best[1] - 0.005                       # petite marge sous la plus basse vraie paire gardée
    return round(min(max(t, BORNES[0]), BORNES[1]), 3)


def vues(annonces, v, seuil):
    """Bilan V1 à V5 : (vraies paires trouvées, fausses alertes, paires ratées)."""
    j = [a for a in annonces if a["id"] in v]
    oui = lambda a: v[a["id"]] == "oui"
    def bilan(pred, base):
        b = [a for a in j if base(a)]
        return (sum(1 for a in b if pred(a) and oui(a)), sum(1 for a in b if pred(a) and not oui(a)),
                sum(1 for a in b if not pred(a) and oui(a)))
    photo = lambda a: a["score"] >= seuil
    return {
        "V1 seuil photo": bilan(photo, lambda a: True),
        "V2 photo, titre qui ne nomme pas la paire": bilan(photo, lambda a: not a["titre_ok"]),
        "V3 photo OU bon titre": bilan(lambda a: photo(a) or a["titre_ok"], lambda a: True),
        "V4 V3 + prix max": bilan(lambda a: (photo(a) or a["titre_ok"]) and a["prix_ok"], lambda a: a["prix_ok"]),
        "V5 V4 + étiquette sans autre code": bilan(
            lambda a: (photo(a) or a["titre_ok"]) and a["prix_ok"] and a.get("etiquette") != "autre code",
            lambda a: a["prix_ok"]),
    }


def telecharger(url, dest):
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    Image.open(io.BytesIO(r.content)).convert("RGB").save(dest, "JPEG", quality=90)


def main():
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+", os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None
    seuils_f = ICI / "seuils.json"
    seuils = json.loads(seuils_f.read_text()) if seuils_f.exists() else {}
    fichiers = sorted((ICI / "verdicts").glob("*.json"))
    if not fichiers:
        sys.exit("Aucun fichier de réponses.")
    for vf in fichiers:
        data = json.loads(vf.read_text())
        s = vf.stem
        rf = ICI / "batteries" / s / "resultats.json"
        if not rf.exists():
            print(f"{s} : pas de résultats de batterie, ignoré")
            continue
        res = json.loads(rf.read_text())
        modele = res["modele"]
        v = {str(k): str(x).lower() for k, x in data.get("verdicts", {}).items() if str(x).lower() in ("oui", "non")}
        af = ICI / "batteries" / s / "appris.json"
        appris = json.loads(af.read_text()) if af.exists() else {"ids": []}
        annonces = res["annonces"]
        par_id = {a["id"]: a for a in annonces}
        depart = float(res["seuil_depart"])

        # Photos : pièges et nouvelles références
        ajout_p, ajout_r = 0, 0
        d = ICI / "references" / s
        (d / "pas-ca").mkdir(parents=True, exist_ok=True)
        for i, rep in v.items():
            a = par_id.get(i)
            if not a or i in appris["ids"]:
                continue
            try:
                if rep == "non" and a["score"] >= depart - 0.08 and ajout_p < 15:
                    telecharger(a["photo"], d / "pas-ca" / f"vinted-{i}.jpg")
                    ajout_p += 1
                elif rep == "oui" and 0.65 <= a["score"] < depart and ajout_r < 5:
                    telecharger(a["photo"], d / f"vinted-{i}.jpg")
                    ajout_r += 1
            except Exception as e:
                print(f"Photo {i} non téléchargée : {e}")
                continue
            appris["ids"].append(i)
        af.write_text(json.dumps(appris))

        # Seuil
        notes = [(par_id[i]["score"], rep == "oui") for i, rep in v.items() if i in par_id]
        nouveau = meilleur_seuil(notes)
        ancien = seuils.get(modele)
        if nouveau is not None:
            seuils[modele] = nouveau
        seuil = seuils.get(modele, depart)
        b = vues(annonces, v, seuil)
        nb_o = sum(1 for x in v.values() if x == "oui")
        lignes = [f"**Apprentissage — {modele}** (batterie du {res['batterie']} UTC)",
                  f"Réponses : {len(v)} ({nb_o} OUI, {len(v) - nb_o} NON)",
                  (f"Seuil : {ancien if ancien else depart:.3f} → **{nouveau:.3f}**" if nouveau is not None
                   else f"Seuil inchangé ({seuil:.3f}) : il faut au moins {MIN_OUI} OUI"),
                  f"Photos ajoutées : {ajout_p} piège(s) dans pas-ca, {ajout_r} référence(s)",
                  "Bilan au seuil " + f"{seuil:.3f} (trouvées · fausses alertes · ratées) :"]
        lignes += [f"- {k} : {x[0]} · {x[1]} · {x[2]}" for k, x in b.items()]
        print("\n".join(lignes))
        if webhook:
            requests.post(webhook, json={"content": "\n".join(lignes)}, timeout=20)
    seuils_f.write_text(json.dumps(seuils, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
