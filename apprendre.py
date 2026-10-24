"""Apprentissage entre les manches, à partir des réponses OUI / NON de Bastien.

Lit verdicts/<slug>.json ({"verdicts": {"<manche>:<id annonce>": "oui"|"non"}}) et les
manches batteries/<slug>/<V>.json, puis :
  - NON avec une photo proche du seuil   -> photo ajoutée dans references/<slug>/pas-ca/
  - OUI avec une photo sous le seuil     -> photo ajoutée dans references/<slug>/ (si >= 0,65)
  - seuil du modèle recalculé sur toutes les réponses -> seuils.json
  - bilan de la manche -> Discord
  - manche entièrement jugée -> manche suivante lancée (workflow « Batterie de tests »)
Réponses déjà appliquées : batteries/<slug>/appris.json.
"""
import io
import json
import os
import re
from pathlib import Path

import requests
from PIL import Image

ICI = Path(__file__).parent
ETAPES = ["V1", "V2", "V3", "V4", "V5"]
NOMS = {"V1": "Seuil", "V2": "Sans le bon titre", "V3": "Titres", "V4": "Prix max", "V5": "Étiquette"}
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
    t = best[1] - 0.005
    return round(min(max(t, BORNES[0]), BORNES[1]), 3)


def telecharger(url, dest):
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    Image.open(io.BytesIO(r.content)).convert("RGB").save(dest, "JPEG", quality=90)


def lancer(modele, etape):
    """Note la manche à lancer ; le workflow la lance APRÈS avoir enregistré pièges, références et seuil."""
    with open("/tmp/a_lancer.txt", "a", encoding="utf-8") as f:
        f.write(f"{modele}|{etape}\n")
    return True


def bilan(annonces, v, seuil):
    """(trouvées, fausses alertes, ratées) avec la règle du bot, sur les annonces jugées dans le prix."""
    t = f = r = 0
    for a in annonces:
        rep = v.get(a["id"])
        if not rep or not a["prix_ok"]:      # au-dessus du prix max : ni trouvée ni ratée
            continue
        al = (a["score"] >= seuil or a["titre_ok"]) and a["prix_ok"] and a.get("etiquette") != "autre code"
        if al and rep == "oui":
            t += 1
        elif al:
            f += 1
        elif rep == "oui":
            r += 1
    return t, f, r


def main():
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+", os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None
    seuils_f = ICI / "seuils.json"
    seuils = json.loads(seuils_f.read_text()) if seuils_f.exists() else {}
    fichiers = sorted((ICI / "verdicts").glob("*.json"))
    if not fichiers:
        print("Aucun fichier de réponses.")
        return
    for vf in fichiers:
        s = vf.stem
        dossier = ICI / "batteries" / s
        etat_f = dossier / "etat.json"
        if not etat_f.exists():
            continue
        etat = json.loads(etat_f.read_text())
        etape = etat["etape"]
        mf = dossier / f"{etape}.json"
        if not mf.exists():
            continue
        manche = json.loads(mf.read_text())
        modele = manche["modele"]
        brut = json.loads(vf.read_text()).get("verdicts", {})
        par_etape = {e: {} for e in ETAPES}
        for k, x in brut.items():
            e, i = k.split(":", 1) if ":" in k else ("V1", k)
            if str(x).lower() in ("oui", "non") and e in par_etape:
                par_etape[e][i] = str(x).lower()
        # Toutes les annonces connues et leur dernière réponse
        toutes, dernier = {}, {}
        for e in ETAPES:
            f = dossier / f"{e}.json"
            if f.exists():
                for a in json.loads(f.read_text())["annonces"]:
                    if not a.get("repris"):
                        toutes[a["id"]] = a
            for i, x in par_etape[e].items():
                dernier[i] = x
        af = dossier / "appris.json"
        appris = json.loads(af.read_text()) if af.exists() else {"ids": []}
        depart = float(manche["seuil"])

        # Photos : pièges et nouvelles références
        ajout_p, ajout_r = 0, 0
        d = ICI / "references" / s
        (d / "pas-ca").mkdir(parents=True, exist_ok=True)
        for i, rep in dernier.items():
            a = toutes.get(i)
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

        # Seuil sur toutes les réponses
        notes = [(toutes[i]["score"], x == "oui") for i, x in dernier.items() if i in toutes]
        nouveau = meilleur_seuil(notes)
        ancien = seuils.get(modele, depart)
        if nouveau is not None:
            seuils[modele] = nouveau
        seuil = seuils.get(modele, depart)

        # Manche terminée ?
        a_juger = [a for a in manche["annonces"] if a.get("a_juger", True)]
        v = par_etape[etape]
        reste = [a for a in a_juger if a["id"] not in v]
        t, f, r = bilan(a_juger, v, seuil)
        nb_o = sum(1 for a in a_juger if v.get(a["id"]) == "oui")
        lignes = [f"**Apprentissage — {modele}, manche {etape} ({NOMS[etape]})**",
                  f"Réponses : {len(a_juger) - len(reste)}/{len(a_juger)} ({nb_o} OUI)",
                  (f"Seuil : {ancien:.3f} → **{nouveau:.3f}**" if nouveau is not None and nouveau != ancien
                   else f"Seuil : {seuil:.3f}"),
                  f"Photos ajoutées : {ajout_p} piège(s), {ajout_r} référence(s)",
                  f"Avec la règle du bot : {t} trouvée(s) · {f} fausse(s) alerte(s) · {r} ratée(s)"]
        if not reste:
            if etape not in etat["terminees"]:
                etat["terminees"].append(etape)
            k = ETAPES.index(etape)
            if k + 1 < len(ETAPES):
                suiv = ETAPES[k + 1]
                etat["etape"] = suiv
                ok = lancer(modele, suiv)
                lignes.append(f"Manche {etape} terminée → **manche {suiv} ({NOMS[suiv]})** "
                              + ("en préparation." if ok else "à lancer à la main (Actions → Batterie de tests)."))
            else:
                etat["fini"] = True
                lignes.append(f"**Les 5 manches sont terminées.** Seuil final {seuil:.3f}, utilisé par la surveillance photo.")
        else:
            lignes.append(f"Il reste {len(reste)} annonce(s) à juger dans cette manche.")
        etat_f.write_text(json.dumps(etat, ensure_ascii=False))
        print("\n".join(lignes))
        if webhook:
            requests.post(webhook, json={"content": "\n".join(lignes)}, timeout=20)
    seuils_f.write_text(json.dumps(seuils, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
