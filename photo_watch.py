"""Surveillance photo : toutes les 5 minutes, nouvelles annonces Vinted (sans texte),
comparées aux photos de référence des modèles suivis. Alerte Discord si la photo
atteint le seuil OU si le titre nomme le modèle, et que le prix est sous le prix max
du modèle. Quand un modèle est ajouté, les annonces récentes déjà en ligne sont
relues pour ce modèle (rattrapage), au lieu d'être ignorées.

Réglages (variables d'environnement) :
  MODELES   modèles suivis, séparés par des virgules (défaut : Fire Red)
  SEUIL     seuil de ressemblance (défaut 0.84) ; un seuil appris par la batterie de tests
            (seuils.json) le remplace pour son modèle
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
    def run(*cmd):
        r = sp.run(list(cmd), capture_output=True, text=True, cwd=ETAT.parent)
        if r.returncode not in (0,) and cmd[:2] != ("git", "diff"):
            print(f"::warning::{' '.join(cmd[:3])} -> {r.returncode} {(r.stderr or r.stdout).strip()[:300]}")
        return r.returncode
    for essai in range(5):
        run("git", "fetch", "-q", "origin", "main")
        run("git", "reset", "-q", "--hard", "origin/main")
        shutil.copy(tmp, ETAT)
        run("git", "add", ETAT.name)
        if run("git", "diff", "--cached", "--quiet") == 0:
            print(f"::notice::sauvegarde : rien de nouveau ({time.strftime('%H:%M:%S')})")
            return
        run("git", "commit", "-q", "-m", "maj annonces vues (photo)")
        if run("git", "push", "-q", "origin", "HEAD:main") == 0:
            print(f"::notice::sauvegarde OK ({time.strftime('%H:%M:%S')})")
            return
        time.sleep(3)
    print("::warning::sauvegarde impossible après 5 essais")


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
    f_seuils = Path(__file__).with_name("seuils.json")
    appris = json.loads(f_seuils.read_text()) if f_seuils.exists() else {}
    seuil_de = {x: float(appris.get(x, seuil)) for x in modeles}
    pages = int(os.environ.get("PAGES") or 3)
    pause = int(os.environ.get("INTERVALLE") or 90)          # secondes entre deux vérifications
    duree = int(os.environ.get("DUREE") or 0) * 60           # minutes de surveillance (0 = un seul passage)
    git_auto = os.environ.get("GIT_SAVE") == "1"
    if not modeles:
        sys.exit("Aucun modèle valide dans MODELES.")

    etat = json.loads(ETAT.read_text()) if ETAT.exists() else {"ids": [], "modeles": []}
    vus = set(etat["ids"])
    prix_haut = int(max(pt.PRIX[x] for x in modeles))
    base, purl = bot.page_url(pt.BASE_URL + "&brand_ids[]=2703" + f"&price_to={prix_haut}")   # marque Jordan seulement
    session = bot.get_session()

    def sauver():
        etat["modeles"] = sorted(set(etat["modeles"]) | set(modeles))
        ETAT.write_text(json.dumps(etat))

    def nouvelles(premier, toutes=False):
        """toutes=True : relit les pages sans sauter le déjà vu (rattrapage d'un nouveau modèle)."""
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
                    if toutes and it.get("photo"):
                        items.append(it)
                    continue
                vus.add(iid)
                etat["ids"].append(iid)
                if it.get("photo"):
                    items.append(it)
            if (deja and not toutes) or premier:
                break
            time.sleep(3)
        etat["ids"] = etat["ids"][-20000:]
        return items

    nouveaux_modeles = [x for x in modeles if x not in etat["modeles"]]
    premier_demarrage = not etat["ids"]
    rattrapage = []
    if premier_demarrage:
        nouvelles(premier=True)          # tout premier démarrage : on mémorise sans envoyer
    elif nouveaux_modeles:
        rattrapage = nouvelles(premier=False, toutes=True)   # relu plus bas pour les nouveaux modèles
    if premier_demarrage or nouveaux_modeles:
        sauver()
        if git_auto:
            git_sauver()
        post(webhook, {"content": "**Surveillance photo démarrée** : " + ", ".join(
            f"{x} (≤ {pt.PRIX[x]:g} €, seuil {seuil_de[x]:.2f})" for x in modeles)
            + " · photo OU titre · vérification toutes les {pause} s"
            + (f" · rattrapage de {len(rattrapage)} annonce(s) récentes pour " + ", ".join(nouveaux_modeles)
               if rattrapage else "") + "."})

    # CLIP + références de chaque modèle (chargés une seule fois)
    model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    tok = open_clip.get_tokenizer("ViT-B-32")
    model.eval()

    def emb(imgs):
        with torch.no_grad():
            f = model.encode_image(torch.stack([prep(i) for i in imgs]))
        return f / f.norm(dim=-1, keepdim=True)

    refs, etis, pieges, clfs, refs_img = {}, {}, {}, {}, {}
    import ia
    pre = float((os.environ.get("IA_PRESELECTION") or "0.70").replace(",", "."))   # 1er tri par la photo (gratuit)
    conf_min = int(os.environ.get("IA_CONFIANCE") or 70)
    ia_max = int(os.environ.get("IA_MAX") or 15)
    ia_jour_max = int(os.environ.get("IA_MAX_JOUR") or 150)                         # appels IA max par jour (coût)
    marge = float((os.environ.get("MARGE") or "0.05").replace(",", "."))             # marge sous le seuil calculé
    pre_de = {}
    voir_refus = (os.environ.get("IA_VOIR_REFUS") or "0") == "1"                     # refus de l'IA envoyés en gris                                     # vérifications IA max par passage
    print("IA de vision : " + ("active" if ia.dispo() else "pas de clé ANTHROPIC_API_KEY, alertes par le titre seulement"))
    # Méthode de la Fire Red / Vivid Sulfur pour toutes les paires : ressemblance aux photos de référence,
    # seuil propre à chaque paire (entre 0,60 et 1). Le classifieur du calibrage n'est plus utilisé.
    for x in modeles:
        d = Path("references") / pt.slug(x)
        r = charger(d)
        if not r:
            print(f"Pas de photo de référence pour {x} : ignoré")
            continue
        refs[x] = emb(r)
        refs_img[x] = r[:2]
        # Seuil propre à la paire : ressemblance d'une vraie photo de la paire avec ses autres références
        # (la moins bonne), moins une marge. Recalculé à chaque lancement à partir des photos de référence.
        if len(r) >= 2:
            m = refs[x] @ refs[x].T
            m.fill_diagonal_(-1)
            pre_de[x] = round(min(1.0, max(0.60, float(m.max(dim=1).values.min()) - marge)), 3)
        else:
            pre_de[x] = pre
        p = charger(d / "pas-ca")
        pieges[x] = emb(p) if p else None
        e = charger(d / "etiquette")
        etis[x] = emb(e) if e else None
        # Apprentissage : on repart de zéro si les photos de référence ont changé (les scores ne sont plus comparables)
        if etat.setdefault("auto_refs", {}).get(x) != len(r):
            etat["auto_refs"][x] = len(r)
            etat.setdefault("auto", {})[x] = []
    base_de = dict(pre_de)
    sonde_ecart = float((os.environ.get("SONDE_ECART") or "0.10").replace(",", "."))   # zone testée sous le seuil
    sonde_jour_max = int(os.environ.get("SONDE_MAX_JOUR") or 20)                       # tests silencieux max par jour

    def seuil_auto(x):
        """Seuil appris : la plus basse vraie paire confirmée par l'IA (trouvée par la photo), moins 0,02.
        Descend dès qu'une vraie est vue plus bas ; ne monte au-dessus du seuil de départ qu'après 3 vraies."""
        oui = [o["s"] for o in etat.get("auto", {}).get(x, []) if o["v"] == "oui" and o["c"] >= conf_min]
        if not oui:
            return base_de[x]
        bas = min(oui) - 0.02
        if bas < base_de[x] or len(oui) >= 3:
            return round(min(1.0, max(0.60, bas)), 3)
        return base_de[x]

    def apprendre(x, score, verdict):
        obs = etat.setdefault("auto", {}).setdefault(x, [])
        obs.append({"s": round(score, 3), "v": verdict["verdict"], "c": verdict["confiance"]})
        del obs[:-300]
        avant, pre_de[x] = pre_de[x], seuil_auto(x)
        if pre_de[x] != avant:
            print(f"[auto] seuil {x} : {avant:.3f} -> {pre_de[x]:.3f}")

    for x in pre_de:
        pre_de[x] = seuil_auto(x)
    print("Seuils par paire : " + ", ".join(f"{x} {v:.2f}" for x, v in pre_de.items()))
    relance_manuelle = os.environ.get("GITHUB_ACTOR", "") not in ("", "github-actions[bot]")
    if premier_demarrage or nouveaux_modeles or relance_manuelle:
        n_jour = etat.get("ia_n", 0) if etat.get("ia_jour") == time.strftime("%Y-%m-%d") else 0
        post(webhook, {"content": "**Surveillance relancée** · IA " + ("active" if ia.dispo() else "INACTIVE (pas de clé)")
                       + f" · {n_jour}/{ia_jour_max} vérifications IA aujourd'hui"
                       + " · paires suivies (seuil) : " + ", ".join(f"{x} ≤ {pt.PRIX[x]:g} € ({v:.2f})" for x, v in pre_de.items())})
    with torch.no_grad():
        te = model.encode_text(tok(["a close-up photo of the size label tag inside a sneaker, with barcode and small printed text",
                                    "a photo of sneakers", "a photo of the sole of a sneaker", "a photo of a shoe box"]))
    te = te / te.norm(dim=-1, keepdim=True)

    from titres import autre_marque

    def titre_dit(it, x):
        return bot.matches_required(it, [g.strip() for g in pt.MOTS.get(x, "").split(",") if g.strip()])

    def passage(items=None, mods=None):
        if items is None:
            items = nouvelles(premier=False)
        mods = [x for x in (mods or modeles) if x in refs]
        print(f"{time.strftime('%H:%M:%S')} : {len(items)} annonce(s) à juger ({', '.join(mods)})")
        if not items or not mods:
            return
        # 3. Étape 1 (gratuite) : présélection par la photo ou par le titre, dans le prix
        alertes, sondes = [], []
        for it in items:
            prix = bot.price_value(it) or 0
            if autre_marque(it.get("title")):
                continue
            try:
                img = Image.open(io.BytesIO(session.get(it["photo"], timeout=20).content)).convert("RGB")
            except Exception as e:
                print(f"Photo illisible {it['id']} : {e}")
                continue
            e = emb([img])
            meilleur = sonde = None
            for x in mods:
                if prix > pt.PRIX[x]:
                    continue
                s = float((e @ refs[x].T).max())
                if pieges.get(x) is not None and float((e @ pieges[x].T).max()) > s:
                    s = 0.0      # plus proche d'un piège (paire refusée par Bastien) que des références
                t = titre_dit(it, x)
                if (s >= pre_de.get(x, pre) or t) and (meilleur is None or (t, s) > meilleur[2:]):
                    meilleur = (x, s, t, s)
                # Juste sous le seuil : candidate à un test silencieux (pour apprendre où sont les vraies)
                ecart = pre_de.get(x, pre) - s
                if 0 < ecart <= sonde_ecart and (sonde is None or ecart < sonde[2]):
                    sonde = (x, s, ecart)
            if meilleur:
                it["modele"], it["score"] = meilleur[:2]
                alertes.append(it)
            elif sonde:
                it["modele"], it["score"], it["sonde"] = sonde[0], sonde[1], True
                sondes.append(it)
        alertes.sort(key=lambda it: (titre_dit(it, it["modele"]), it["score"]), reverse=True)
        if len(alertes) > ia_max:
            print(f"{len(alertes) - ia_max} candidate(s) au-delà de {ia_max} non vérifiées ce passage")
            alertes = alertes[:ia_max]
        print(f"{len(alertes)} candidate(s) à vérifier")
        # Tests silencieux : quelques annonces sous le seuil vérifiées par l'IA, rien n'est envoyé si NON
        jour = time.strftime("%Y-%m-%d")
        if etat.get("sonde_jour") != jour:
            etat["sonde_jour"], etat["sonde_n"] = jour, 0
        if ia.dispo() and sondes:
            sondes.sort(key=lambda it: pre_de[it["modele"]] - it["score"])
            n = max(0, min(3, sonde_jour_max - etat["sonde_n"]))
            etat["sonde_n"] += min(n, len(sondes))
            alertes += sondes[:n]
            if n and sondes:
                print(f"[auto] {min(n, len(sondes))} test(s) silencieux sous le seuil")

        # 4. Étape 2 : étiquette dans les autres photos, puis envoi
        for it in alertes:
            x = it["modele"]
            codes = pt.CODES.get(x, [])
            eti_url, statut = None, "pas de photo d'étiquette : à demander au vendeur"
            urls = []
            for essai in range(2):      # 2e essai si la page de l'annonce n'a pas donné ses photos
                try:
                    urls = pt.photos_annonce(session, it)
                except Exception as ex:
                    print(f"Annonce illisible {it['id']} : {ex}")
                if urls:
                    break
                time.sleep(3)
            print(f"[diag] annonce {it['id']} : {len(urls)} photo(s) lue(s)")
            notes = []
            for u in urls[:8]:
                try:
                    im = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
                except Exception:
                    continue
                f = emb([im])
                p_eti = float((100 * f @ te.T).softmax(dim=-1)[0][0])
                s_eti = float((f @ etis[x].T).max()) if etis.get(x) is not None else 0
                notes.append((max(p_eti, s_eti), p_eti >= 0.5 or s_eti >= 0.80, u, im))
            notes.sort(key=lambda t: t[0], reverse=True)
            eti_img = None
            if notes and notes[0][1]:
                eti_url, eti_img = notes[0][2], notes[0][3]
                statut = "trouvée, mais code illisible : à vérifier à l'oeil"
            titre_ok = titre_dit(it, x)

            # Étape 2 : l'IA de vision regarde vraiment les photos
            verdict = None
            jour = time.strftime("%Y-%m-%d")
            if etat.get("ia_jour") != jour:
                etat["ia_jour"], etat["ia_n"] = jour, 0
            if ia.dispo() and etat["ia_n"] >= ia_jour_max:
                if not titre_ok:
                    continue      # plafond du jour atteint : plus d'IA, seulement les titres qui nomment la paire
            elif ia.dispo():
                etat["ia_n"] += 1
                photos = [im for _, _, u, im in sorted(notes, key=lambda t: urls.index(t[2]) if t[2] in urls else 99)
                          if u != eti_url][:4] or [Image.open(io.BytesIO(session.get(it["photo"], timeout=20).content)).convert("RGB")]
                voisins = [m for m in pt.PRIX if m != x][:6]
                verdict = ia.verifier(x, codes, refs_img[x], photos, eti_img, it.get("title", ""), voisins)
                print(f"IA {it['id']} {x} : {verdict}")
                if verdict.get("erreur"):
                    if not titre_ok:
                        continue
                    verdict = None
                else:
                    if not titre_ok:     # trouvée par la photo : sert à apprendre le seuil de la paire
                        apprendre(x, it["score"], verdict)
                    if it.get("sonde") and (verdict["verdict"] != "oui" or verdict["confiance"] < conf_min):
                        continue         # test silencieux refusé : rien sur Discord
                if verdict and (verdict["verdict"] != "oui" or verdict["confiance"] < conf_min):
                    if voir_refus:      # montrer aussi les refus (en gris) pour juger l'IA
                        post(webhook, {"embeds": [{
                            "title": it["title"][:250], "url": it["url"], "color": 0x95A5A6,
                            "author": {"name": f"IA : refusée ({x} ?)"},
                            "fields": [{"name": "Prix", "value": it["price"], "inline": True},
                                       {"name": "IA", "value": f"{verdict['verdict'].upper()} à {verdict['confiance']} %", "inline": True},
                                       {"name": "Pourquoi", "value": (verdict.get("raison") or "-")[:200], "inline": False}],
                            "thumbnail": {"url": it["photo"]}}]})
                    continue
                elif verdict and verdict.get("code_lu"):
                    lu = verdict["code_lu"].upper().replace(" ", "-")
                    bon = any(c.replace("-", "") in lu.replace("-", "") for c in codes)
                    statut = f"lue par l'IA : {verdict['code_lu']} = " + ("BON CODE" if bon else "AUTRE CODE (à vérifier)")
            elif not titre_ok:
                continue          # sans IA : plus d'alerte sur la photo seule (trop de fausses alertes)

            champs = [{"name": "Prix", "value": it["price"], "inline": True},
                      {"name": "Taille · état", "value": it.get("details", "?"), "inline": True}]
            if verdict:
                champs.insert(1, {"name": "IA", "value": f"OUI à {verdict['confiance']} %", "inline": True})
            champs.append({"name": "Titre", "value": "dit le modèle" if titre_ok else "ne le dit pas (trouvée par la photo)", "inline": True})
            champs.append({"name": "Étiquette", "value": statut, "inline": False})
            embeds = [{
                "title": it["title"][:250], "url": it["url"], "color": 0x2ECC71,
                "author": {"name": f"{'IA' if verdict else 'Titre'} : {x}"},
                "fields": champs,
                "image": {"url": it["photo"]},
            }]
            # Les 3 premières photos de l'annonce, dans l'ordre (Discord les regroupe en galerie : même "url")
            for u in [u for u in urls if pt.jeton(u) != pt.jeton(it["photo"])][:2]:
                embeds.append({"url": it["url"], "image": {"url": u}})
            # Étiquette en dessous, dans un bloc à part (url différente = pas mélangée à la galerie)
            if eti_url:
                embeds.append({"url": it["url"] + "#etiquette", "title": "Étiquette",
                               "color": 0x2ECC71, "image": {"url": eti_url}})
            post(webhook, {"embeds": embeds})

    if rattrapage:
        try:
            passage(rattrapage, [x for x in nouveaux_modeles if x in refs])
        except Exception as e:
            print(f"::warning::Erreur pendant le rattrapage : {str(e)[:300]}")

    debut = time.time()
    dernier_git = time.time()
    nb_passages = 0
    while True:
        t0 = time.time()
        try:
            passage()
        except Exception as e:
            print(f"::warning::Erreur pendant le passage : {str(e)[:300]}")
        sauver()
        nb_passages += 1
        if git_auto and time.time() - dernier_git > 300:
            print(f"::notice::{nb_passages} passages, {len(etat['ids'])} annonces vues")
            git_sauver()
            dernier_git = time.time()
        if time.time() - debut + pause > duree:
            break
        time.sleep(max(5, pause - (time.time() - t0)))
    if git_auto:
        git_sauver()


if __name__ == "__main__":
    main()
