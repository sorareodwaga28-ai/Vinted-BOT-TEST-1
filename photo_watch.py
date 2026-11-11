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
  INTERVALLE secondes entre deux vérifications (défaut 45 via le workflow)
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
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta
import threading
from contextlib import contextmanager

# Verrou des candidates vérifiées en parallèle : une seule à la fois touche l'état, CLIP et Discord ;
# hors_verrou() le relâche pendant les attentes réseau (page Vinted, photos, IA).
verrou = threading.Lock()
_fil = threading.local()


@contextmanager
def sous_verrou():
    verrou.acquire()
    _fil.tient = True
    try:
        yield
    finally:
        _fil.tient = False
        verrou.release()


def avec_verrou(fn):
    """Lance fn sous le verrou, sauf si ce fil le tient déjà."""
    if getattr(_fil, "tient", False):
        return fn()
    with sous_verrou():
        return fn()


@contextmanager
def hors_verrou():
    if not getattr(_fil, "tient", False):
        yield
        return
    _fil.tient = False
    verrou.release()
    try:
        yield
    finally:
        verrou.acquire()
        _fil.tient = True

import open_clip
import requests
import torch
from PIL import Image

import bot
import photo_test as pt

# V4 (1/11) : plusieurs bots en même temps (matrice GitHub). Bot 1 = principal (récap, messages de démarrage).
BOT = int(os.environ.get("BOT") or 1)
BOTS = int(os.environ.get("BOTS") or 1)
ETAT = Path(__file__).with_name("photo_seen.json" if BOT == 1 else f"photo_seen_{BOT}.json")
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
        try:      # 3/11 : petit résumé pour l'onglet Stats du site (les photo_seen*.json font ~800 Ko)
            ecrire_stats(tmp)
            run("git", "add", f"stats_bot{BOT}.json")
        except Exception as ex:
            print(f"::warning::stats du site non écrites : {str(ex)[:200]}")
        if pt.DIAG.exists():      # V4 : relevé des champs date de la page Vinted (une fois)
            run("git", "add", pt.DIAG.name)
        run("git", "add", "references")      # 2/11 : étiquettes fake (references/<paire>/etiquette-fake/)
        if bot.DIAG_CATALOGUE.exists():      # 2/11 : une annonce brute du résultat de recherche (heure de publication)
            run("git", "add", bot.DIAG_CATALOGUE.name)
        if run("git", "diff", "--cached", "--quiet") == 0:
            print(f"::notice::sauvegarde : rien de nouveau ({time.strftime('%H:%M:%S')})")
            return
        run("git", "commit", "-q", "-m", "maj annonces vues (photo)")
        if run("git", "push", "-q", "origin", "HEAD:main") == 0:
            print(f"::notice::sauvegarde OK ({time.strftime('%H:%M:%S')})")
            return
        time.sleep(3)
    print("::warning::sauvegarde impossible après 5 essais")


def ecrire_stats(chemin_etat):
    """3/11 : stats_bot<N>.json, lu par l'onglet Stats d'Odwagasnkrs : compteurs du jour et de la veille,
    coût IA, et les réponses oui / non / fake de Bastien (avec la couleur de l'alerte)."""
    e = json.loads(Path(chemin_etat).read_text())
    pe = float(os.environ.get("IA_PRIX_ENTREE") or 0.10)
    ps = float(os.environ.get("IA_PRIX_SORTIE") or 0.50)
    eur = float(os.environ.get("EUR_PAR_USD") or 0.92)

    def resume(r):
        if not r:
            return None
        return {"jour": r.get("jour"), "annonces": r.get("annonces", 0), "candidates": r.get("candidates", 0),
                "appels": r.get("appels", 0), "vert": r.get("vert", 0), "bleu": r.get("bleu", 0),
                "orange": r.get("orange", 0), "loupees": r.get("loupees", 0),
                "cout_centimes": round((r.get("tok_entree", 0) / 1e6 * pe + r.get("tok_sortie", 0) / 1e6 * ps) * eur * 100, 1)}
    at = e.get("a_trancher", {})
    rep = [{"id": i, "v": v, "type": at[i].get("type", "jaune"), "paire": at[i].get("paire"), "date": at[i].get("date")}
           for i, v in (e.get("verdicts_discord") or {}).items() if i in at]
    Path(__file__).with_name(f"stats_bot{BOT}.json").write_text(json.dumps(
        {"bot": BOT, "maj": datetime.now(ZoneInfo("Europe/Paris")).isoformat(timespec="seconds"),
         "aujourdhui": resume(e.get("recap")), "hier": resume(e.get("recap_hier")), "reponses": rep},
        ensure_ascii=False, indent=1))


ALERTES = Path(__file__).with_name("alertes.json")


def publier_alerte(alerte):
    publier_alertes([alerte])


def publier_alertes(alertes):
    """Ajoute les alertes en tête de alertes.json et les envoie sur GitHub en une fois (lu par l'onglet COP sneakers)."""
    import subprocess as sp
    if os.environ.get("GIT_SAVE") != "1":
        return
    def run(*cmd):
        return sp.run(list(cmd), capture_output=True, text=True, cwd=ALERTES.parent).returncode
    for essai in range(4):
        run("git", "fetch", "-q", "origin", "main")
        run("git", "reset", "-q", "--hard", "origin/main")
        try:
            liste = json.loads(ALERTES.read_text()) if ALERTES.exists() else []
        except ValueError:
            liste = []
        ids = {a.get("id") for a in liste}
        liste = [a for a in alertes if a["id"] not in ids] + liste
        ALERTES.write_text(json.dumps(liste[:300], ensure_ascii=False, indent=1))
        run("git", "add", ALERTES.name)
        run("git", "commit", "-q", "-m", "COP sneakers : " + ", ".join(f"{a['paire']} {a['prix']}" for a in alertes)[:200])
        if run("git", "push", "-q", "origin", "HEAD:main") == 0:
            print(f"::notice::{len(alertes)} alerte(s) publiée(s) pour le site")
            sauver_local()
            return
        time.sleep(2)
    print("::warning::alerte non publiée pour le site après 4 essais")


SAUVER_LOCAL = []


def sauver_local():
    """Après un reset git, réécrit l'état courant (photo_seen.json) qui vient d'être remplacé par celui du repo."""
    for f in SAUVER_LOCAL:
        avec_verrou(f)


def voisins_de(x, n=6):
    """3/11 : paires qui ressemblent à x (même famille d'abord) pour prévenir l'IA. Avant : toujours les
    6 premières paires de la liste (des Jordan 4), même pour une Air Max 1 ou une Sacai."""
    mots = set(bot.tokens(x))
    meme = [m for m in pt.PRIX if m != x and famille(m) == famille(x)]
    meme.sort(key=lambda m: -len(mots & set(bot.tokens(m))))      # Thunder -> Red Thunder en premier
    return meme[:n]      # les autres familles (Air Max pour une Jordan…) ne se confondent pas : inutile de les citer


def famille(x):
    for f in ("Air Max 1", "Sacai", "Nocta", "Jordan 1", "Jordan 3"):
        if x.startswith(f):
            return f
    return "Jordan 4"


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
    # 2/11 (Bastien) : prix max SANS la boîte d'origine. PRIX = prix max avec boîte (Og Box).
    # Une annonce entre les deux n'est gardée que si l'annonce dit qu'il y a la boîte d'origine.
    PRIX_SANS = {}
    for morceau in re.split(r"[;\n]", os.environ.get("PRIX_SANS_BOITE") or ""):
        if "=" in morceau:
            nom, val = (t.strip() for t in morceau.split("=", 1))
            if nom in pt.PRIX:
                PRIX_SANS[nom] = float(val.replace(",", "."))
    seuil = float((os.environ.get("SEUIL") or "0.84").replace(",", "."))
    f_seuils = Path(__file__).with_name("seuils.json")
    appris = json.loads(f_seuils.read_text()) if f_seuils.exists() else {}
    seuil_de = {x: float(appris.get(x, seuil)) for x in modeles}
    pages = int(os.environ.get("PAGES") or 3)
    pause = float(os.environ.get("INTERVALLE") or 90)          # secondes entre deux vérifications
    duree = int(os.environ.get("DUREE") or 0) * 60           # minutes de surveillance (0 = un seul passage)
    git_auto = os.environ.get("GIT_SAVE") == "1"
    if not modeles:
        sys.exit("Aucun modèle valide dans MODELES.")

    principal = ETAT.with_name("photo_seen.json")
    if BOT > 1 and not ETAT.exists() and principal.exists():
        # Nouveau bot : il part de la mémoire du bot principal (seuils appris, pièges, annonces vues)…
        etat = json.loads(principal.read_text())
        for k in ("recap", "ia_n", "ia_np", "ia_jour", "ia_periode", "recap_envoye"):
            etat.pop(k, None)      # …mais avec ses propres compteurs du jour
        ETAT.write_text(json.dumps(etat))
        print(f"[bot {BOT}] mémoire copiée depuis photo_seen.json")
    etat = json.loads(ETAT.read_text()) if ETAT.exists() else {"ids": [], "modeles": []}
    vus = set(etat["ids"])
    # 11/11 : la grosse recherche marque est partagée en 3 (un tiers des annonces par bot, selon le numéro d'annonce).
    PARTAGE = os.environ.get("PARTAGE_MARQUE") == "1"
    if PARTAGE and BOT > 1 and not etat.get("partage_marque") and principal.exists():
        # 1re fois : on reprend les annonces déjà vues par le bot 1 pour ne pas ré-alerter les anciennes
        anciens = json.loads(principal.read_text()).get("ids", [])
        vus |= set(anciens)
        etat["ids"] = (anciens + etat["ids"])[-20000:]
        print(f"[bot {BOT}] partage marque : {len(anciens)} annonces déjà vues reprises du bot 1")
    if PARTAGE:
        etat["partage_marque"] = True
    prix_haut = int(max(pt.PRIX[x] for x in modeles))
    # Une recherche Vinted par famille (Jordan : marque Jordan ; Air Max 1 : texte « air max 1 »), au prix max le plus haut
    familles = {}
    for x in modeles:
        familles.setdefault(pt.recherche(x), []).append(x)
    def prix_famille(f, xs):
        # La recherche marque Nike sert à toutes les paires (Air Max, Sacai… mal titrées) : jusqu'au prix max le plus haut
        return prix_haut if pt.NIKE and pt.NIKE in f else int(max(pt.PRIX[x] for x in xs))
    recherches = [bot.page_url(pt.BASE_URL + f + f"&price_to={prix_famille(f, xs)}")
                  for f, xs in familles.items()]
    session = bot.get_session()

    # Vérifie que le numéro de marque utilisé pour « Nike » renvoie bien des annonces Nike ; sinon on le retire.
    if pt.NIKE and any(pt.NIKE in f for f in familles):
        try:
            b, u = bot.page_url(pt.BASE_URL + pt.NIKE)
            marques = [str(it.get("brand", "")).lower() for it in bot.fetch_items(session, b, u)]
            ok = sum("nike" in m for m in marques) >= max(3, len(marques) // 2)
            print(f"[diag] marque {pt.NIKE} : {len(marques)} annonces, Nike = {ok} ({marques[:5]})")
        except Exception as e:
            ok = True      # Vinted injoignable : on garde, la recherche normale dira s'il y a un souci
            print(f"[diag] vérification marque Nike impossible : {e}")
        if not ok:
            post(webhook, {"content": "**Attention** : le numéro de marque Nike du bot est faux, la recherche Jordan "
                                      "reste sur la marque Jordan seule. Préviens Claude."})
            familles = {f.replace(pt.NIKE, ""): xs for f, xs in familles.items()}
            recherches = [bot.page_url(pt.BASE_URL + f + f"&price_to={prix_famille(f, xs)}")
                          for f, xs in familles.items()]

    # V4 : chaque bot ne fait que SES recherches (BOT_RECHERCHES : « marque » = la grosse recherche
    # Jordan + Nike sans mot-clé, sinon les mots-clés, ex. « air+max+1,patta »).
    choix = [c.strip() for c in (os.environ.get("BOT_RECHERCHES") or "").split(",") if c.strip()]
    if choix:
        def garde(f):
            return any(("search_text=" not in f) if c == "marque" else (f"search_text={c}" in f) for c in choix)
        familles = {f: xs for f, xs in familles.items() if garde(f)}
        recherches = [bot.page_url(pt.BASE_URL + f + f"&price_to={prix_famille(f, xs)}")
                      for f, xs in familles.items()]
    # Paires jugées : toutes pour le bot de la grosse recherche (annonces mal titrées « Basket Nike »),
    # sinon seulement celles de ses recherches.
    if any("search_text=" not in f for f in familles):
        modeles_juges = list(modeles)
    else:
        modeles_juges = [x for xs in familles.values() for x in xs]
    # Titres pris en charge par les autres bots : ignorés ici (évite les doublons d'alerte et d'IA)
    mots_exclus = [m.strip() for m in (os.environ.get("EXCLURE_TITRES") or "").split(",") if m.strip()]
    exclure_re = re.compile(r"\b(" + "|".join(r"\s*".join(map(re.escape, m.split())) for m in mots_exclus) + r")\b") if mots_exclus else None
    print(f"[bot {BOT}/{BOTS}] recherches : {len(recherches)} · paires jugées : {len(modeles_juges)}"
          + (f" · titres laissés aux autres bots : {', '.join(mots_exclus)}" if mots_exclus else ""))

    def sauver():
        etat["modeles"] = sorted(set(etat["modeles"]) | set(modeles))
        ETAT.write_text(json.dumps(etat))
    SAUVER_LOCAL.append(sauver)

    from concurrent.futures import ThreadPoolExecutor as _Pool
    pool_rech = _Pool(max(1, len(recherches)))
    verrou_vus = threading.Lock()

    def nouvelles(premier, toutes=False):
        """toutes=True : relit les pages sans sauter le déjà vu (rattrapage d'un nouveau modèle).
        1/11 : toutes les recherches partent EN MÊME TEMPS (avant : l'une après l'autre)."""
        items = []
        for lot in pool_rech.map(lambda r: lire(r[0], r[1], premier, toutes), recherches):
            items += lot
        with verrou_vus:
            etat["ids"] = etat["ids"][-20000:]
        return items

    t_min = float(os.environ.get("TAILLE_MIN") or 40)
    t_max = float(os.environ.get("TAILLE_MAX") or 46)

    def bonne_taille(it):
        """Taille lue dans « 42,5 · Très bon état ». Taille illisible -> gardée (on ne veut rien rater)."""
        m = re.match(r"\s*(?:EU\s*)?(\d{2})(?:[.,](\d))?", (it.get("details") or ""), re.I)
        if not m:
            return True
        t = float(m.group(1) + "." + (m.group(2) or "0"))
        return t_min <= t <= t_max

    def lire(base, purl, premier, toutes):
        items, deja = [], False
        marque = "search_text=" not in purl
        for pg in range(1, pages + 1):
            try:
                lot = bot.fetch_items(session, base, purl + (f"&page={pg}" if pg > 1 else ""))
            except Exception as e:
                print(f"Page {pg} illisible : {e}")
                break
            for it in lot:
                iid = str(it["id"])
                if PARTAGE and marque and int(iid) % BOTS != BOT - 1:
                    # Annonce de la recherche marque qui revient à un autre bot (1/3 chacun)
                    with verrou_vus:
                        if iid in vus:
                            deja = True
                        else:
                            vus.add(iid)
                    continue
                with verrou_vus:      # plusieurs recherches en même temps : une annonce n'est prise qu'une fois
                    nouveau = iid not in vus
                    if nouveau:
                        vus.add(iid)
                        etat["ids"].append(iid)
                if not nouveau:
                    deja = True
                    if toutes and it.get("photo") and bonne_taille(it):
                        items.append(it)
                    continue
                if it.get("photo") and bonne_taille(it):
                    it["t_vu"] = time.time()      # 11/11 : heure de détection (départ du chrono)
                    items.append(it)
            if (deja and not toutes) or premier:
                break
            time.sleep(3)
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
        if BOT == 1:
          post(webhook, {"content": "**Surveillance photo démarrée** : " + ", ".join(
            f"{x} (≤ {pt.PRIX[x]:g} €, seuil {seuil_de[x]:.2f})" for x in modeles)
            + f" · photo OU titre · vérification toutes les {pause} s"
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
    etis_img, fakes_eti = {}, {}
    import ia
    pre = float((os.environ.get("IA_PRESELECTION") or "0.70").replace(",", "."))   # 1er tri par la photo (gratuit)
    conf_min = int(os.environ.get("IA_CONFIANCE") or 70)
    ia_max = int(os.environ.get("IA_MAX") or 15)
    ia_jour_max = int(os.environ.get("IA_MAX_JOUR") or 450)                         # appels IA max par jour (coût)
    h_debut = int(os.environ.get("IA_HEURE_DEBUT") or 0)                             # IA 24h/24 par défaut (heure de Paris)
    h_fin = int(os.environ.get("IA_HEURE_FIN") or 24)
    ia_nuit_max = int(os.environ.get("IA_MAX_NUIT") or 1500)                           # appels réservés à la nuit
    nuit_debut = int(os.environ.get("IA_NUIT_DEBUT") or 23)                            # nuit de 23h à 9h (heure de Paris)
    nuit_fin = int(os.environ.get("IA_NUIT_FIN") or 9)
    prix_entree = float(os.environ.get("IA_PRIX_ENTREE") or 0.10)                    # $ par million de tokens (Haiku 5.5)
    prix_sortie = float(os.environ.get("IA_PRIX_SORTIE") or 0.50)
    eur_par_usd = float(os.environ.get("EUR_PAR_USD") or 0.92)
    paris = ZoneInfo("Europe/Paris")
    lissage = (os.environ.get("IA_LISSAGE") or "1") != "0"                         # 0 = pas de lissage à la minute
    mention = (os.environ.get("DISCORD_MENTION") or "").strip()                      # mention sur les alertes vertes

    def ia_budget():
        """Budget IA réparti par heure de 9h à 23h (heure de Paris), 0 la nuit. Ce qui n'est pas utilisé
        dans une heure se reporte sur les suivantes. Renvoie (possible, raison)."""
        maintenant = datetime.now(paris)
        jour = maintenant.strftime("%Y-%m-%d")
        if etat.get("ia_jour") != jour:
            etat["ia_jour"], etat["ia_n"] = jour, 0
        h = maintenant.hour
        if h < h_debut or h >= h_fin:
            return False, f"nuit : pas d'IA de {h_fin}h à {h_debut}h"
        # Budget de nuit (23h-9h) à part : la nuit ne peut pas manger le budget de la journée, et inversement
        nuit = h >= nuit_debut or h < nuit_fin
        if not nuit:
            cle = "J" + jour
        elif h >= nuit_debut:
            cle = "N" + jour
        else:      # entre minuit et 9h : même nuit que la veille au soir
            cle = "N" + (maintenant - timedelta(days=1)).strftime("%Y-%m-%d")
        if etat.get("ia_periode") != cle:
            premiere = etat.get("ia_periode") is None
            etat["ia_periode"], etat["ia_np"] = cle, (etat["ia_n"] if premiere and not nuit else 0)
        limite = ia_nuit_max if nuit else ia_jour_max - ia_nuit_max
        if etat.get("ia_np", 0) >= limite:
            return False, ("budget IA de la nuit" if nuit else "budget IA de la journée") + f" atteint ({limite})"
        # Lissé à la minute : le budget se remplit en continu (~1 toutes les 2 min), le non-utilisé se reporte
        minutes = (h - h_debut) * 60 + maintenant.minute + 1
        permis = (min(ia_jour_max, int(ia_jour_max * minutes / ((h_fin - h_debut) * 60)) + 1)
                  if lissage else ia_jour_max)
        if etat["ia_n"] >= permis:
            return False, f"budget IA du moment atteint ({etat['ia_n']}/{permis} aujourd'hui)"
        return True, ""
    marge = float((os.environ.get("MARGE") or "0.05").replace(",", "."))             # marge sous le seuil calculé
    pre_de = {}
    seuil_min = float((os.environ.get("SEUIL_MIN") or "0.72").replace(",", "."))      # seuil photo minimum, toutes paires
    ia_paire_max = int(os.environ.get("IA_MAX_PAIRE_JOUR") or 0)   # 0 = budget du jour divisé par le nombre de paires
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
            pre_de[x] = round(min(1.0, max(seuil_min, float(m.max(dim=1).values.min()) - marge)), 3)
        else:
            pre_de[x] = max(pre, seuil_min)
        p = charger(d / "pas-ca")
        pieges[x] = emb(p) if p else None
        e = charger(d / "etiquette")
        etis[x] = emb(e) if e else None
        etis_img[x] = e[:3]
        fakes_eti[x] = charger(d / "etiquette-fake")[-3:]      # 2/11 : étiquettes fake confirmées par Bastien
        # Apprentissage : on repart de zéro si les photos de référence ont changé (les scores ne sont plus comparables)
        if etat.setdefault("auto_refs", {}).get(x) != len(r):
            etat["auto_refs"][x] = len(r)
            etat.setdefault("auto", {})[x] = []
            etat.setdefault("sonde_total", {})[x] = 0     # calibrage à refaire
    if not ia_paire_max:      # part égale pour chaque paire (choix du 29/10) : 8000 / nombre de paires
        ia_paire_max = max(1, ia_jour_max // max(1, len([x for x in modeles_juges if x in refs])))
    print(f"Plafond IA par paire : {ia_paire_max} vérifications par jour ({len(refs)} paires)")
    pre_de = {x: seuil_min for x in pre_de}     # décision du 28/10 : toutes les paires partent du plancher (0,72)
    base_de = dict(pre_de)
    sonde_ecart = float((os.environ.get("SONDE_ECART") or "0.10").replace(",", "."))   # zone testée sous le seuil
    sonde_jour_max = int(os.environ.get("SONDE_PAR_JOUR") or 0)      # tests silencieux max par paire et par jour
    sonde_total_max = int(os.environ.get("SONDE_TOTAL") or 90)       # tests par paire avant de la dire calibrée

    non_min = int(os.environ.get("SEUIL_NON_MIN") or 40)                                # NON avant de monter le seuil
    non_part = float((os.environ.get("SEUIL_NON_PART") or "0.60").replace(",", "."))   # part des NON coupés
    non_plafond = float((os.environ.get("SEUIL_NON_PLAFOND") or "0.80").replace(",", "."))   # sans OUI connu

    # 2/11 (Bastien) : une paire avec moins de 10 vérifications IA dans la journée (3 bots ensemble)
    # voit son seuil baisser de 0,02 le lendemain, cumulable, sans descendre sous 0,68.
    baisse_min_verif = int(os.environ.get("BAISSE_MIN_VERIF") or 10)
    baisse_pas = float((os.environ.get("BAISSE_PAS") or "0.02").replace(",", "."))
    plancher_baisse = float((os.environ.get("BAISSE_PLANCHER") or "0.68").replace(",", "."))

    def seuil_auto(x):
        """Seuil appris. Les OUI : la plus basse vraie paire confirmée, moins 0,02 (jamais au-dessus).
        Les NON (29/10) : après 40 NON, le seuil monte pour couper les 60 % de NON les moins ressemblants,
        sans dépasser 0,80 tant qu'aucun OUI n'est connu. Le calibrage silencieux vérifie ensuite juste en dessous."""
        obs = etat.get("auto", {}).get(x, [])
        oui = [o["s"] for o in obs if o["v"] == "oui" and o["c"] >= conf_min]
        non = sorted(o["s"] for o in obs if o["v"] == "non" and o["c"] >= conf_min)
        s = base_de[x]
        if oui:
            bas = min(oui) - 0.02
            if bas < base_de[x] or len(oui) >= 3:
                s = bas
        if len(non) >= non_min:
            plafond = (min(oui) - 0.02) if oui else non_plafond
            # Jamais au-dessus d'une vraie paire déjà mesurée (mesure.txt) : sinon elle serait écartée avant l'IA
            vraies = [m["photo1"] for m in etat.get("mesures", {}).values()
                      if m.get("paire") == x and m.get("photo1")]
            if vraies:
                plafond = min(plafond, min(vraies) - 0.02)
            s = max(s, min(non[int(non_part * len(non))], plafond))
        r = min(1.0, max(seuil_min, s))
        b = etat.get("baisse", {}).get(x, 0)
        if b:       # 2/11 (Bastien) : paire calme la veille -> seuil baissé, jamais sous le plancher de la baisse
            r = max(plancher_baisse, r - b)
        return round(r, 3)

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
    etat["seuils"] = {x: {"depart": base_de[x], "actuel": pre_de[x]} for x in pre_de}   # lisible dans photo_seen.json
    relance_manuelle = os.environ.get("GITHUB_ACTOR", "") not in ("", "github-actions[bot]")
    if BOT == 1 and (premier_demarrage or nouveaux_modeles or relance_manuelle):
        n_jour = etat.get("ia_n", 0) if etat.get("ia_jour") == datetime.now(paris).strftime("%Y-%m-%d") else 0
        post(webhook, {"content": "**Surveillance relancée** · IA " + ("active" if ia.dispo() else "INACTIVE (pas de clé)")
                       + f" · {n_jour}/{ia_jour_max} vérifications IA aujourd'hui"
                       + f" (nuit 23h-9h : {ia_nuit_max}, journée 9h-23h : {ia_jour_max - ia_nuit_max}, {ia_paire_max} max par paire)"
                       + " · paires suivies (seuil) : " + ", ".join(f"{x} ≤ {pt.PRIX[x]:g} € ({v:.2f})" for x, v in pre_de.items())})
    with torch.no_grad():
        te = model.encode_text(tok(["a close-up photo of the size label tag inside a sneaker, with barcode and small printed text",
                                    "a photo of sneakers", "a photo of the sole of a sneaker", "a photo of a shoe box"]))
    te = te / te.norm(dim=-1, keepdim=True)

    from titres import autre_marque

    def exclu(it, x):
        return bool(set(bot.tokens(it.get("title"))) & set(pt.EXCLUS.get(x, ())))

    def titre_dit(it, x):
        return not exclu(it, x) and bot.matches_required(it, [g.strip() for g in pt.MOTS.get(x, "").split(",") if g.strip()])

    # « aqua » seul est trop courant (plein d'Air Max 1 aqua, contrefaçons) : l'alerte bleue sans IA exige « anniversary »
    GENERIQUES = set("a ma j jordan air max nike 1 3 4 sb high and sacai vaporwaffle ldwaffle aqua".split()) | (set(pt.COURANTS.split()) - {"fire"})
    SYNONYMES = {"anniversary": {"anniversaire"}, "maniere": {"amm", "amamaniere", "sleeping"}, "cement": {"ciment"}, "neon": {"volt", "fluo"}, "nina": {"abney", "bicoastal", "channel"}, "oxidized": {"oxydized", "oxidised", "oxydised", "oxide", "oxyde", "oxydee", "oxydize", "oxidize"}}

    def titre_nomme(it, x):
        """Titre exact ET qui contient le vrai nom du coloris (thunder, pine, fire, limeade…), pas seulement des
        couleurs (« noir + jaune ») : dans ce cas, pas besoin de l'IA."""
        if exclu(it, x):
            return False
        nom_mots = set(bot.tokens(x)) - GENERIQUES
        nom_mots |= {v for m in list(nom_mots) for v in SYNONYMES.get(m, ())}
        toks = bot.tokens(it.get("title"))
        txt = " " + " ".join(toks) + " "
        # « low » dans le titre alors que la paire n'est pas une low (ex. Pine Green Low) : l'IA tranche
        if "low" in toks and "low" not in bot.tokens(x):
            return False
        # Le titre contient aussi le nom d'une autre paire suivie (ex. « Bred Reimagined … fire red ») : l'IA tranche
        for y in pt.PRIX:
            if y != x and (set(bot.tokens(y)) - GENERIQUES - nom_mots) & set(toks):
                return False
        for g in pt.MOTS.get(x, "").split(","):
            mots = [bot.tokens(m) for m in g.split("+") if bot.tokens(m)]
            if mots and mots[0] == ["jordan"]:
                continue      # « jordan + pine » peut être une autre Jordan (Legacy 312…) : seulement via l'IA
            if (mots and any(set(m) & nom_mots for m in mots)
                    and all(f" {' '.join(m)} " in txt or ("".join(m) in toks) for m in mots)):
                return True
        return False

    def titre_exact(it, x):
        """Le titre contient les vrais mots de la paire, sans aucune faute tolérée (alerte sans IA)."""
        if exclu(it, x):
            return False
        toks = bot.tokens(it.get("title"))
        txt = " " + " ".join(toks) + " "
        for g in pt.MOTS.get(x, "").split(","):
            mots = [bot.tokens(m) for m in g.split("+") if bot.tokens(m)]
            if mots and all(f" {' '.join(m)} " in txt or ("".join(m) in toks) for m in mots):
                return True
        return False

    def verifs_hier(r):
        """Vérifications IA par paire pour la journée de r, les 3 bots ensemble (fichiers des autres lus sur GitHub)."""
        import subprocess as sp
        n = {x: v.get("ia", 0) for x, v in (r.get("paires") or {}).items()}
        if BOTS < 2:
            return n
        sp.run(["git", "fetch", "-q", "origin", "main"], capture_output=True, cwd=ETAT.parent)
        for b in range(1, BOTS + 1):
            if b == BOT:
                continue
            f = "photo_seen.json" if b == 1 else f"photo_seen_{b}.json"
            out = sp.run(["git", "show", f"origin/main:{f}"], capture_output=True, text=True, cwd=ETAT.parent)
            try:
                autre = json.loads(out.stdout) if out.returncode == 0 else {}
            except ValueError:
                autre = {}
            ra = next((c for c in (autre.get("recap"), autre.get("recap_hier"))
                       if c and c.get("jour") == r.get("jour")), {})
            for x, v in (ra.get("paires") or {}).items():
                n[x] = n.get(x, 0) + v.get("ia", 0)
        return n

    def baisser_seuils(r, jour):
        """Changement de jour : -0,02 pour chaque paire qui a eu moins de 10 vérifications IA la veille."""
        etat["baisse_jour"] = jour
        try:
            n = verifs_hier(r)
        except Exception as ex:
            print(f"::warning::baisse des seuils impossible : {str(ex)[:200]}")
            return
        b = etat.setdefault("baisse", {})
        for x in list(pre_de):
            if n.get(x, 0) < baisse_min_verif:
                avant = pre_de[x]
                if avant <= plancher_baisse:
                    continue
                b[x] = round(b.get(x, 0) + baisse_pas, 3)
                pre_de[x] = seuil_auto(x)
                print(f"[baisse] {x} : {n.get(x, 0)} vérifications le {r['jour']} -> seuil {avant:.3f} -> {pre_de[x]:.3f}")

    # Récap du soir (21h, heure de Paris) : compteurs de la journée dans photo_seen.json → "recap"
    def rec(x=None):
        jour = datetime.now(paris).strftime("%Y-%m-%d")
        r = etat.get("recap")
        if not r or r.get("jour") != jour:
            if r and r.get("jour") and etat.get("baisse_jour") != jour:
                etat["recap_hier"] = r
                baisser_seuils(r, jour)
            r = etat["recap"] = {"jour": jour, "annonces": 0, "candidates": 0, "loupees": 0,
                                 "vert": 0, "bleu": 0, "orange": 0, "calib": 0, "paires": {}}
        r.setdefault("tok_entree", 0)
        r.setdefault("tok_sortie", 0)
        r.setdefault("appels", 0)
        if x:
            return r["paires"].setdefault(x, {"ia": 0, "oui": 0, "non": 0, "incertain": 0, "loupees": 0})
        return r

    def compter(verdict):
        """Ajoute les tokens d'un appel IA au récap du jour (pour le coût réel)."""
        c = (verdict or {}).get("conso")
        if c:
            r = rec()
            r["tok_entree"] += c["entree"]
            r["tok_sortie"] += c["sortie"]
            r["appels"] += 1

    def cout_centimes(r):
        usd = r.get("tok_entree", 0) / 1e6 * prix_entree + r.get("tok_sortie", 0) / 1e6 * prix_sortie
        return usd * eur_par_usd * 100

    FRAIS_PORT = float((os.environ.get("FRAIS_PORT") or "4.50").replace(",", "."))          # port estimé ajouté au prix
    COEF_REVENTE = float((os.environ.get("COEF_REVENTE") or "1.6").replace(",", "."))   # revente visée = (prix + port) x 1,6

    PROT_FIXE = float((os.environ.get("PROTECTION_FIXE") or "0.70").replace(",", "."))   # protection acheteur Vinted
    PROT_PCT = float((os.environ.get("PROTECTION_PCT") or "5").replace(",", "."))        # … 0,70 € + 5 % du prix

    def prix_champ(it):
        """Champ « Prix » de l'alerte : prix affiché, et en dessous le prix avec frais (protection acheteur + port)."""
        try:
            p = float(str(it.get("price", "")).split()[0].replace(",", "."))
        except (ValueError, IndexError):
            return str(it.get("price", "?"))
        total = p + PROT_FIXE + p * PROT_PCT / 100 + FRAIS_PORT
        return f"{it['price']}\n({total:.2f} € avec frais)"

    def resume(it, x, couleur, verdict=None):
        """Texte court en tête du message : c'est lui qui s'affiche dans la notification de l'iPhone.
        Bot IA : nom de la paire ; titre exact (sans IA) : titre de l'annonce. Puis prix, taille, IA et photo."""
        taille = (it.get("details") or "?").partition(" · ")[0].strip() or "?"
        nom = x if couleur in ("VERT", "JAUNE") else (it.get("title") or x)[:80]
        # 1/11 (Bastien) : couleur · paire · prix · taille (ressemblance) · (prix + port) x 1,6 · délai
        # pas de couleur dans la notification (Bastien, 1/11) : elle reste visible dans l'alerte
        score = f" ({float(it['score']):.2f} / seuil {pre_de.get(x, pre):.2f})" if it.get("score") else ""
        bouts = [nom + (" ?" if couleur == "JAUNE" else ""), str(it.get("price", "?")), f"T{taille}{score}"]
        try:
            p = float(str(it.get("price", "")).split()[0].replace(",", "."))
            bouts.append(f"-> {(p + PROT_FIXE + p * PROT_PCT / 100 + FRAIS_PORT) * COEF_REVENTE:.2f} €")   # prix avec frais x 1,6
        except (ValueError, IndexError):
            pass
        d = noter_delai(it, couleur)
        if d is not None:
            bouts.append(f"détectée il y a {d:.0f} s")
        return " · ".join(bouts)

    def noter_delai(it, couleur="?"):
        """11/11 : Vinted ne donne plus l'heure de publication (ni page, ni recherche, ni API).
        On chronomètre donc de la détection de l'annonce par le bot jusqu'à l'envoi de l'alerte,
        étape par étape (recherche + tri photo, page de l'annonce, photos, IA). Notée une fois par annonce."""
        if "delai" not in it:
            it["delai"] = None
            t0, now = it.get("t_vu"), time.time()
            if t0:
                def ecart(a, b):
                    return round(it[b] - it[a], 1) if it.get(a) and it.get(b) else None
                d = round(now - t0, 1)
                it["delai"] = d
                e = {"c": couleur.lower(), "t": d, "tri": ecart("t_vu", "t_tri"), "page": ecart("t_tri", "t_page"),
                     "photos": ecart("t_page", "t_photos"), "ia": ecart("t_ia0", "t_ia")}
                dl = rec().setdefault("delais", [])
                dl.append(e)
                del dl[:-3000]
                print(f"[délai] {it['id']} {couleur} : {e}")
        return it["delai"]

    # 3/11 : notifications du site Odwagasnkrs (Web Push). Même texte que la notification Discord ;
    # la toucher ouvre la fiche de la paire dans l'onglet COP sneakers. Secret GitHub VAPID_PRIVATE.
    vapid = (os.environ.get("VAPID_PRIVATE") or "").strip()
    site = (os.environ.get("SITE_URL") or "https://odwagasnkrs.netlify.app").rstrip("/")

    def push_envoyer(payload):
        if not vapid:
            return
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            print("::warning::pywebpush absent : pas de notification du site")
            return
        for abo in list(etat.get("push_abos") or []):
            try:
                webpush(abo, json.dumps(payload, ensure_ascii=False), vapid_private_key=vapid,
                        vapid_claims={"sub": "mailto:bot@users.noreply.github.com"}, timeout=10)
            except WebPushException as ex:
                code = getattr(ex.response, "status_code", None)
                print(f"::warning::notification du site refusée ({code}) : {str(ex)[:150]}")
                if code in (404, 410):      # abonnement expiré (icône supprimée, données effacées)
                    with verrou_push:
                        etat["push_abos"] = [a for a in etat.get("push_abos", []) if a.get("endpoint") != abo.get("endpoint")]
            except Exception as ex:
                print(f"::warning::notification du site impossible : {str(ex)[:150]}")

    verrou_push = threading.Lock()

    def notifier_site(texte, it, x, couleur, urls, eti_url=None, verdict=None):
        """texte = le résumé Discord (resume()). Titre = 1re partie (paire ou titre), corps = le reste."""
        if not vapid or not etat.get("push_abos"):
            return
        import base64
        titre, _, corps = texte.partition(" · ")
        taille, _, etat_p = (it.get("details") or "?").partition(" · ")
        fiche = {"paire": x, "titre": it.get("title", ""), "prix": it.get("price"), "taille": taille.strip() or "?",
                 "etat": etat_p.strip() or "?", "type": {"VERT": "vert", "BLEU": "bleu", "ORANGE": "orange", "JAUNE": "jaune"}.get(couleur, "vert"),
                 "famille": famille(x), "url": it["url"], "etiquette": eti_url,
                 "ia": ({"verdict": verdict["verdict"], "confiance": verdict["confiance"]} if verdict else None),
                 "ressemblance": round(float(it.get("score") or 0), 3), "seuil": pre_de.get(x),
                 "date": datetime.now(paris).isoformat(timespec="seconds"), "vendeur": pt.vendeur(it["id"])}
        for n in (5, 3, 1, 0):      # une notification Apple fait 4 Ko max : moins de photos si besoin
            fiche["photos"] = (urls or [it["photo"]])[:n]
            d = base64.urlsafe_b64encode(json.dumps(fiche, ensure_ascii=False, separators=(",", ":")).encode()).decode().rstrip("=")
            payload = {"title": (("? " if couleur == "JAUNE" else "") + titre)[:120], "body": corps[:300], "id": str(it["id"]),
                       "url": f"{site}/?alerte={it['id']}&d={d}"}
            if len(json.dumps(payload, ensure_ascii=False).encode()) < 3300:
                break
        pool.submit(push_envoyer, payload)

    # 2/11 : contrôle de l'étiquette. Tailles Nike homme : US -> (UK, EUR, CM)
    TAILLES_H = {6: (5.5, 38.5, 24), 6.5: (6, 39, 24.5), 7: (6, 40, 25), 7.5: (6.5, 40.5, 25.5), 8: (7, 41, 26),
                 8.5: (7.5, 42, 26.5), 9: (8, 42.5, 27), 9.5: (8.5, 43, 27.5), 10: (9, 44, 28), 10.5: (9.5, 44.5, 28.5),
                 11: (10, 45, 29), 11.5: (10.5, 45.5, 29.5), 12: (11, 46, 30), 12.5: (11.5, 47, 30.5), 13: (12, 47.5, 31)}
    typo_min = int(os.environ.get("TYPO_FAKES_MIN") or 3)      # test de typographie à partir de 3 étiquettes fake

    def coherence_tailles(txt):
        """Texte des tailles lu par l'IA (« US 9 UK 8 EUR 42.5 CM 27 ») -> message si incohérent, '' si OK ou illisible."""
        t = (txt or "").upper().replace(",", ".")
        if not t or re.search(r"\bUS\s*[\d.]+\s*[WYC]\b|WMNS|\bW\b", t):
            return ""      # tailles femme / enfant : pas de table fiable
        def val(cle):
            m = re.search(cle + r"\s*:?\s*(\d+(?:\.\d)?)", t)
            return float(m.group(1)) if m else None
        us, uk, eur, cm = val(r"\bUS"), val(r"\bUK"), val(r"\bEU[R]?"), val(r"\bCM")
        if us is None or us not in TAILLES_H:
            return ""
        a_uk, a_eur, a_cm = TAILLES_H[us]
        faux = [f"{n} {v:g} (attendu {a:g})" for n, v, a in (("UK", uk, a_uk), ("EUR", eur, a_eur), ("CM", cm, a_cm))
                if v is not None and abs(v - a) > 0.01]
        return ("tailles incohérentes pour US " + f"{us:g} : " + ", ".join(faux)) if faux else ""

    def controle_etiquette(it, x, codes, eti_img, verdict):
        """Texte du champ « Étiquette » de l'alerte (ou None). Résultat du test gardé dans it["eti_test"]."""
        bouts = []
        inco = coherence_tailles((verdict or {}).get("tailles"))
        if inco:
            bouts.append("SUSPECTE : " + inco)
            rec()["eti_incoherentes"] = rec().get("eti_incoherentes", 0) + 1
        if eti_img is not None and len(fakes_eti.get(x) or []) >= typo_min and etis_img.get(x) and ia.dispo():
            etat["ia_n"] += 1
            with hors_verrou():
                v = ia.etiquette(pt.nom(x), codes, etis_img[x], fakes_eti[x], eti_img)
            compter(v)
            print(f"[typo] {it['id']} {x} : {v}")
            if not v.get("erreur"):
                it["eti_test"] = v["verdict"]
                r = rec()
                r["typo"] = r.get("typo", 0) + 1
                r["typo_fake"] = r.get("typo_fake", 0) + (v["verdict"] == "fake")
                mot = {"vraie": "ressemble aux vraies", "fake": "FAKE PROBABLE", "incertain": "incertain"}[v["verdict"]]
                bouts.append(f"typo (test) : {mot} à {v['confiance']} %" + (f" · {v['raison']}" if v.get("raison") else ""))
        return " · ".join(bouts)[:1000] or None

    def garder_etiquette_fake(iid, info):
        """Réponse « fake » de Bastien : l'étiquette de l'annonce va dans references/<paire>/etiquette-fake/."""
        x = info["paire"]
        dossier = Path("references") / pt.slug(x) / "etiquette-fake"
        im = None
        try:
            if info.get("eti"):
                im = Image.open(io.BytesIO(session.get(info["eti"], timeout=20).content)).convert("RGB")
            else:      # alerte sans étiquette repérée (bleue…) : on la cherche dans les photos de l'annonce
                url = info.get("url") or f"https://www.vinted.fr/items/{iid}"
                urls = pt.photos_annonce(session, {"id": iid, "url": url, "photo": info.get("photo")})
                ims = [i for i in telecharger(urls[:20]) if i is not None]
                if ims and etis.get(x) is not None:
                    F = emb_lots(ims)
                    sc = [max(p, s2) for p, s2 in zip((100 * F @ te.T).softmax(dim=-1)[:, 0].tolist(),
                                                     (F @ etis[x].T).max(dim=1).values.tolist())]
                    k = max(range(len(sc)), key=lambda i: sc[i])
                    im = ims[k] if sc[k] >= 0.5 else None
        except Exception as ex:
            print(f"[fake] étiquette {iid} illisible : {ex}")
        if im is None:
            print(f"[fake] {iid} {x} : pas d'étiquette trouvée dans l'annonce")
            return
        dossier.mkdir(parents=True, exist_ok=True)
        im.save(dossier / f"{iid}.jpg", "JPEG", quality=90)
        fakes_eti[x] = (fakes_eti.get(x) or []) + [im]
        fakes_eti[x] = fakes_eti[x][-3:]
        n = len(list(dossier.glob("*.jpg")))
        print(f"[fake] {iid} {x} : étiquette gardée ({n} fake pour cette paire)")
        if n == typo_min:
            post(webhook, {"content": f"**{x}** : {n} étiquettes fake enregistrées, le test de typographie démarre pour cette paire."})

    def envoyer_incertaine(it, x, verdict, urls, eti_url):
        """L'IA hésite : alerte jaune sur Discord. Bastien dit oui / non / fake, Claude l'écrit dans
        verdicts/bastien.json, et le bot s'en sert pour son seuil, ses références et ses pièges."""
        iid = str(it["id"])
        a = etat.setdefault("a_trancher", {})
        a[iid] = {"paire": x, "s": round(float(it.get("score") or 0), 3), "photo": it["photo"],
                  "date": datetime.now(paris).isoformat(timespec="minutes"), "url": it["url"], "eti": eti_url,
                  "eti_test": it.get("eti_test")}
        for k in list(a)[:-500]:
            del a[k]
        rec(x)["envoyees_incertaines"] = rec(x).get("envoyees_incertaines", 0) + 1
        vend = pt.vendeur(it["id"]) or {}
        avis = (f"{vend['avis']} avis" + (f" · {vend['etoiles']}/5" if vend.get("etoiles") is not None else "")
                if vend.get("avis") is not None else "?")
        champs = [{"name": "Prix", "value": prix_champ(it), "inline": True},
                  {"name": "Taille · état", "value": it.get("details", "?"), "inline": True},
                      {"name": "Boîte", "value": pt.boite(it["id"], it.get("title", "")), "inline": True},
                  {"name": "Vendeur", "value": pt.champ_vendeur(vend), "inline": True},
                  {"name": "IA", "value": f"{verdict['verdict'].upper()} à {verdict['confiance']} % (seuil {conf_min} %)",
                   "inline": True},
                  {"name": "Ressemblance photo",
                   "value": f"{float(it.get('score') or 0):.2f} (seuil {pre_de.get(x, pre):.2f})", "inline": True}]
        if it.get("eti_champ"):
            champs.append({"name": "Étiquette", "value": it["eti_champ"], "inline": False})
        if verdict.get("raison"):      # seulement quand la réponse de l'IA était illisible ou coupée (pour comprendre)
            champs.append({"name": "Problème IA", "value": verdict["raison"][:200], "inline": False})
        embeds = [{"title": f"{x} ?", "url": it["url"], "color": 0xF1C40F,
                   "author": {"name": "IA incertaine : à toi de trancher (oui / non / fake)"},
                   "fields": champs, "image": {"url": it["photo"]},
                   "footer": {"text": f"Réf {iid} · réponds à ce message : oui / non / fake"}}]
        for u in [u for u in urls if pt.jeton(u) != pt.jeton(it["photo"])][:2]:
            embeds.append({"url": it["url"], "image": {"url": u}})
        if eti_url:
            embeds.append({"url": it["url"] + "#etiquette", "title": "Étiquette", "color": 0xF1C40F,
                           "image": {"url": eti_url}})
        txt = resume(it, x, "JAUNE", verdict)
        bot.envoyer(webhook, {"content": txt, "embeds": embeds, "components": bot.boutons(it)})
        notifier_site(txt, it, x, "JAUNE", urls, eti_url, verdict)

    f_bastien = Path(__file__).parent / "verdicts" / "bastien.json"

    def appliquer_bastien():
        """Réponses de Bastien aux incertaines (verdicts/bastien.json, {"id": "oui" | "non" | "fake"}).
        oui -> compte comme un OUI sûr pour le seuil + photo ajoutée aux références de la paire ;
        non (autre coloris / autre modèle) -> NON sûr + photo ajoutée aux pièges de la paire ;
        fake (contrefaçon) -> NON sûr seulement (une fausse ressemble à la vraie : pas de piège)."""
        try:
            rep_b = json.loads(f_bastien.read_text()) if f_bastien.exists() else {}
        except ValueError:
            rep_b = {}
        rep_b = {**etat.get("verdicts_discord", {}), **rep_b}
        faits = etat.setdefault("bastien_faits", [])
        for iid, v in rep_b.items():
            iid, v = str(iid), str(v).lower().strip()
            info = etat.get("a_trancher", {}).get(iid)
            if iid in faits or not info or v not in ("oui", "non", "fake") or info["paire"] not in refs:
                continue
            x = info["paire"]
            faits.append(iid)
            if v == "fake":
                garder_etiquette_fake(iid, info)
            if info.get("eti_test") in ("vraie", "fake") and v in ("oui", "fake"):
                r = rec()
                r["typo_compare"] = r.get("typo_compare", 0) + 1
                r["typo_accord"] = r.get("typo_accord", 0) + ((info["eti_test"] == "fake") == (v == "fake"))
            if info["s"] > 0:
                apprendre(x, info["s"], {"verdict": "oui" if v == "oui" else "non", "confiance": 100})
            if v in ("oui", "non"):
                try:
                    im = Image.open(io.BytesIO(session.get(info["photo"], timeout=20).content)).convert("RGB")
                    f = emb([im])
                except Exception as ex:
                    print(f"[bastien] photo {iid} illisible : {ex}")
                    continue
                cle = "refs_bastien" if v == "oui" else "pieges_bastien"
                etat.setdefault(cle, {}).setdefault(x, []).append([round(t, 5) for t in f[0].tolist()])
                cible = refs if v == "oui" else pieges
                cible[x] = f if cible.get(x) is None else torch.cat([cible[x], f])
            print(f"[bastien] {iid} {x} : {v} (seuil {pre_de[x]:.3f})")
        del faits[:-1000]

    jeton_bot = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    salon = {}

    def lire_abonnement(m, api, h):
        brut = m.get("content", "").strip()
        ok_abo = False
        try:
            import base64
            b = brut.split(None, 1)[1].strip()
            abo = json.loads(base64.b64decode(b + "=" * (-len(b) % 4)).decode())
            if abo.get("endpoint") and (abo.get("keys") or {}).get("p256dh"):
                abos = [a for a in etat.get("push_abos", []) if a.get("endpoint") != abo["endpoint"]]
                etat["push_abos"] = (abos + [{"endpoint": abo["endpoint"], "keys": abo["keys"]}])[-5:]
                ok_abo = True
                print(f"[push] abonnement enregistré ({len(etat['push_abos'])} téléphone(s))")
                if BOT == 1:
                    pool.submit(push_envoyer, {"title": "Notifications activées",
                                               "body": "Les alertes du bot arriveront aussi ici.", "url": site + "/"})
        except Exception as ex:
            print(f"[push] abonnement illisible : {ex}")
        if BOT == 1 or ok_abo:
            try:
                requests.put(f"{api}/{m['id']}/reactions/{'%E2%9C%85' if ok_abo else '%E2%9D%93'}/@me", headers=h, timeout=20)
            except Exception:
                pass

    def lire_discord():
        """Réponses de Bastien directement dans le salon Discord (bot Discord, secret DISCORD_BOT_TOKEN) :
        « oui / non / fake » en réponse à une alerte verte, bleue, jaune ou orange (on lit « Réf <id> » dans son pied),
        ou « <id> oui » écrit seul. ✅ = pris en compte, ❓ = pas compris / annonce inconnue."""
        if not jeton_bot:
            return
        if not salon:
            salon["id"] = requests.get(webhook, timeout=20).json()["channel_id"]
        api = f"https://discord.com/api/v10/channels/{salon['id']}/messages"
        h = {"Authorization": f"Bot {jeton_bot}"}
        dernier = etat.get("discord_dernier")
        if not salon.get("abo_relu"):
            # 3/11 : au démarrage, relit les 100 derniers messages pour un « abonnement … » envoyé pendant
            # qu'un ancien code tournait (il l'avait ignoré mais avait déjà avancé discord_dernier)
            salon["abo_relu"] = True
            try:
                vieux = requests.get(api, headers=h, timeout=20, params={"limit": 100})
                for m in sorted(vieux.json() if vieux.status_code == 200 else [], key=lambda m: int(m["id"])):
                    if (not m["author"].get("bot") and not m.get("webhook_id")
                            and m.get("content", "").strip().lower().startswith("abonnement ")
                            and (not dernier or int(m["id"]) <= int(dernier))):
                        lire_abonnement(m, api, h)
            except Exception as ex:
                print(f"[push] relecture des abonnements impossible : {ex}")
        r = requests.get(api, headers=h, timeout=20, params={"limit": 100, **({"after": dernier} if dernier else {})})
        if r.status_code != 200:
            print(f"::warning::Lecture Discord : HTTP {r.status_code} {r.text[:200]}")
            return
        msgs = sorted(r.json(), key=lambda m: int(m["id"]))
        if not dernier:
            # 1er démarrage : on ne relit que les 30 dernières minutes
            limite = (int(time.time() * 1000) - 1420070400000 - 30 * 60 * 1000) << 22
            msgs = [m for m in msgs if int(m["id"]) > limite]
        for m in msgs:
            etat["discord_dernier"] = m["id"]
            if m["author"].get("bot") or m.get("webhook_id"):
                continue
            brut = m.get("content", "").strip()
            if brut.lower().startswith("abonnement "):      # 3/11 : code copié depuis le site (Activer les notifications)
                lire_abonnement(m, api, h)
                continue
            txt = brut.lower()
            mot = re.search(r"\b(oui|non|fake)\b", txt)
            if not mot:
                continue
            iid = None
            ref = m.get("referenced_message") or {}
            for e in ref.get("embeds", []):
                f = re.search(r"Réf (\d+)", (e.get("footer") or {}).get("text", ""))
                if f:
                    iid = f.group(1)
                    break
            if not iid:
                f = re.search(r"(\d{6,})", txt)
                iid = f.group(1) if f else None
            if not iid:
                continue  # message normal du salon : on ne touche à rien
            ok = bool(iid) and iid in etat.get("a_trancher", {})
            if ok:
                etat.setdefault("verdicts_discord", {})[iid] = mot.group(1)
                d = etat["verdicts_discord"]
                for k in list(d)[:-500]:
                    del d[k]
                print(f"[discord] {iid} : {mot.group(1)}")
            if not ok and BOTS > 1:
                continue      # alerte d'un autre bot : c'est lui qui répondra ✅
            emoji = "%E2%9C%85" if ok else "%E2%9D%93"
            try:
                requests.put(f"{api}/{m['id']}/reactions/{emoji}/@me", headers=h, timeout=20)
            except Exception:
                pass

    # Réponses déjà données par Bastien les jours précédents : remises en mémoire au démarrage
    for cle, cible in (("refs_bastien", refs), ("pieges_bastien", pieges)):
        for x, liste in etat.get(cle, {}).items():
            if x in refs and liste:
                t = torch.tensor(liste, dtype=refs[x].dtype)
                cible[x] = t if cible.get(x) is None else torch.cat([cible[x], t])

    def recap():
        r = rec()
        if BOT != 1 or datetime.now(paris).hour < 21 or etat.get("recap_envoye") == r["jour"]:
            return
        r = combiner_bots(r)
        lignes = [f"**Récap surveillance photo (IA)** du {r['jour']}",
                  f"Annonces regardées : {r['annonces']} · ressemblantes (photo ou titre) : {r['candidates']}",
                  f"**Coût IA du jour : {cout_centimes(r):.1f} centimes** ({r.get('appels', 0)} vérifications"
                  + (f", ~{cout_centimes(r) / r['appels']:.3f} centime chacune" if r.get("appels") else "")
                  + ", de minuit à 21h, estimation d'après les tokens)",
                  f"Alertes : {r['vert']} vertes (IA OUI) · {r['bleu']} bleues (titre exact) · {r['orange']} oranges (titre, sans IA)",
                  f"IA : {r.get('ia_n_total', etat.get('ia_n', 0))} vérifications" + (f" ({BOTS} bots)" if BOTS > 1 else f"/{ia_jour_max}"),
                  (f"**Test de l'IA sur les alertes bleues** (vraies paires, IA sans le titre) : "
                   f"{r.get('test_bleues_oui', 0)}/{r['test_bleues']} reconnues"
                   + (" · ratées : " + ", ".join(r.get("test_bleues_ratees", [])[:10]) if r.get("test_bleues_ratees") else "")
                   if r.get("test_bleues") else "Test de l'IA sur les alertes bleues : aucune bleue aujourd'hui"),
                  f"**Loupées** (ressemblantes mais pas vérifiées : budget IA du jour atteint, ou plus de {ia_max} d'un coup) : {r['loupees']}"]
        for x in sorted(r["paires"], key=lambda k: -r["paires"][k]["ia"] - r["paires"][k]["loupees"]):
            v = r["paires"][x]
            lignes.append(f"- {x} : {v['ia']} vérifiées ({v['oui']} OUI, {v['non']} NON, {v['incertain']} incertain)"
                          + (f", {v['loupees']} loupées" if v["loupees"] else "")
                          + (f", {v['plafond']} non vérifiées (plafond {ia_paire_max}/jour)" if v.get("plafond") else "")
                          + (f", {v['sans_boite']} trop chères sans boîte" if v.get("sans_boite") else "")
                          + f" · seuil {pre_de.get(x, 0):.2f}")
        if not r["paires"]:
            lignes.append("Aucune annonce ressemblante aujourd'hui.")
        lignes.insert(1, texte_delais(r.get("delais") or []))
        lignes.append(f"**Étiquettes** : {r.get('eti_incoherentes', 0)} avec tailles incohérentes"
                      + (f" · test de typographie : {r.get('typo', 0)} jugées, {r.get('typo_fake', 0)} fake probables"
                         if r.get("typo") else "")
                      + (f" · d'accord avec toi {r.get('typo_accord', 0)}/{r['typo_compare']}" if r.get("typo_compare") else "")
                      + " · étiquettes fake gardées : " + (", ".join(f"{x} {len(v)}" for x, v in fakes_eti.items() if v) or "aucune"))
        texte = "\n".join(lignes)
        for i in range(0, len(texte), 1900):
            post(webhook, {"content": texte[i:i + 1900]})
        etat["recap_envoye"] = r["jour"]


    def texte_delais(dl):
        """Ligne du récap : délai détection -> alerte, par couleur, avec le détail des étapes (11/11)."""
        dl = [e for e in dl if isinstance(e, dict)]
        if not dl:
            return "**Vitesse** : pas encore de délai mesuré aujourd'hui"
        lignes = []
        for c in ("vert", "bleu", "orange", "jaune"):
            es = [e for e in dl if e.get("c") == c]
            if not es:
                continue
            t = sorted(e["t"] for e in es)
            neuf = t[min(len(t) - 1, int(0.9 * len(t)))]
            def moy(k):
                v = [e[k] for e in es if e.get(k) is not None]
                return f"{sum(v) / len(v):.1f} s" if v else "-"
            nom = {"vert": "vertes", "bleu": "bleues", "orange": "orange", "jaune": "jaunes"}[c]
            lignes.append(f"**Vitesse alertes {nom} ({len(t)})** : détection -> alerte **{sum(t) / len(t):.1f} s** en moyenne"
                          f" · plus rapide {t[0]:.1f} s · plus lent {t[-1]:.1f} s · 9 fois sur 10 : {neuf:.1f} s ou moins"
                          f"\n  détail : tri photo {moy('tri')} · page annonce {moy('page')} · photos {moy('photos')} · IA {moy('ia')}")
        return "\n".join(lignes)

    def combiner_bots(r):
        """Bot principal : ajoute au récap les compteurs du jour des autres bots (lus sur GitHub)."""
        import copy
        import subprocess as sp
        r = copy.deepcopy(r)
        r["ia_n_total"] = etat.get("ia_n", 0)
        if BOTS < 2:
            return r
        sp.run(["git", "fetch", "-q", "origin", "main"], capture_output=True, cwd=ETAT.parent)
        for b in range(2, BOTS + 1):
            out = sp.run(["git", "show", f"origin/main:photo_seen_{b}.json"], capture_output=True, text=True, cwd=ETAT.parent)
            try:
                autre = json.loads(out.stdout) if out.returncode == 0 else {}
            except ValueError:
                autre = {}
            ra = autre.get("recap") or {}
            if ra.get("jour") != r["jour"]:
                continue
            r["ia_n_total"] += autre.get("ia_n", 0) if autre.get("ia_jour") == r["jour"] else 0
            for k, v in ra.items():
                if k in ("jour", "paires"):
                    continue
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    r[k] = r.get(k, 0) + v
                elif isinstance(v, list):
                    r[k] = (r.get(k) or []) + v
            for x, vp in (ra.get("paires") or {}).items():
                cible = r["paires"].setdefault(x, {"ia": 0, "oui": 0, "non": 0, "incertain": 0, "loupees": 0})
                for k, v in vp.items():
                    if isinstance(v, (int, float)):
                        cible[k] = cible.get(k, 0) + v
        return r

    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(8)
    pool_cand = ThreadPoolExecutor(int(os.environ.get("CANDIDATES_PARALLELE") or 5))
    pool_ia = ThreadPoolExecutor(int(os.environ.get("CANDIDATES_PARALLELE") or 5))   # 11/11 : appels IA rapides (1re photo)
    a_publier = []      # alertes en attente pour l'onglet COP sneakers du site

    def publier_en_attente():
        """Envoie sur GitHub, en une fois, les alertes du site accumulées (hors verrou : git est lent)."""
        with sous_verrou():
            lot = a_publier[:]
            del a_publier[:]
        if lot:
            try:
                publier_alertes(lot)
            except Exception as ex:
                print(f"::warning::alertes non publiées pour le site : {str(ex)[:200]}")

    def _img(u):
        try:
            return Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
        except Exception as ex:
            print(f"Photo illisible {u[:80]} : {ex}")
            return None

    def telecharger(urls):
        """Télécharge plusieurs photos en même temps (None si illisible)."""
        return list(pool.map(_img, urls)) if urls else []

    def emb_lots(imgs, lot=32):
        if not imgs:
            return []
        return torch.cat([emb(imgs[i:i + lot]) for i in range(0, len(imgs), lot)])

    photo2_max = int(os.environ.get("PHOTO2_MAX") or 15)          # annonces douteuses dont on lit la 2e photo, par passage
    photo2_zone = float((os.environ.get("PHOTO2_ZONE") or "0.08").replace(",", "."))   # « presque au seuil »
    piege_marge = float((os.environ.get("PIEGE_IA_MARGE") or "0.04").replace(",", "."))
    piege_max = int(os.environ.get("PIEGE_IA_MAX") or 200)         # photos refusées gardées par paire

    def pas_une_chaussure(e):
        """La 1re photo ressemble plus à une étiquette, une semelle ou une boîte qu'à une paire de baskets."""
        return int((100 * e[:1] @ te.T).softmax(dim=-1)[0].argmax()) != 1

    def choisir(it, e, mods):
        """e : une ligne par photo de l'annonce. Renvoie (meilleure paire (x, score, photo), proche du seuil, possible)."""
        prix = bot.price_value(it) or 0
        meilleur, proche, possible = None, False, False
        for x in mods:
            if prix > pt.PRIX[x] or autre_marque(it.get("title"), x):
                continue
            possible = True
            t = titre_dit(it, x)
            sims = (e @ refs[x].T).max(dim=1).values
            s, best = 0.0, None
            for k in range(e.shape[0]):
                sk = float(sims[k])
                if pieges.get(x) is not None and float((e[k:k + 1] @ pieges[x].T).max()) > sk:
                    continue      # plus proche d'un piège (paire refusée par Bastien) que des références
                if (not t and pieges_ia.get(x) is not None
                        and float((e[k:k + 1] @ pieges_ia[x].T).max()) >= sk + piege_marge):
                    if sk >= pre_de.get(x, pre):
                        rec()["ecartees_non"] = rec().get("ecartees_non", 0) + 1
                    continue      # ressemble bien plus à une annonce que l'IA a déjà refusée pour cette paire
                if sk > s:
                    s, best = sk, e[k:k + 1]
            if (s >= pre_de.get(x, pre) or t) and (meilleur is None or (t, s) > (meilleur[3], meilleur[1])):
                meilleur = (x, s, best if best is not None else e[:1], t)
            elif s >= pre_de.get(x, pre) - photo2_zone:
                proche = True
                it["proche_s"] = max(it.get("proche_s", 0), s)
        return (meilleur[:3] if meilleur else None), proche, possible

    # Mémoire des refus de l'IA (gratuite) : photos d'annonces jugées NON, par paire. Gardée entre deux
    # passages GitHub par le cache (dossier cache_pieges/, pas dans le repo).
    f_pieges = Path(__file__).with_name("cache_pieges") / "pieges_ia.pt"
    pieges_ia = {}
    try:
        if f_pieges.exists():
            pieges_ia = {x: t.float() for x, t in torch.load(f_pieges).items() if x in refs}
            print(f"Mémoire des refus IA : {sum(len(t) for t in pieges_ia.values())} photos")
    except Exception as ex:
        print(f"::warning::mémoire des refus IA illisible : {ex}")

    def retenir_non(x, e):
        t = pieges_ia.get(x)
        pieges_ia[x] = (e if t is None else torch.cat([t, e]))[-piege_max:]

    def sauver_pieges():
        try:
            f_pieges.parent.mkdir(exist_ok=True)
            torch.save({x: t.half() for x, t in pieges_ia.items()}, f_pieges)
        except Exception as ex:
            print(f"::warning::mémoire des refus IA non sauvée : {ex}")

    test_bleues = (os.environ.get("TEST_BLEUES") or "0") == "1"      # V4 : plus d'IA sur les bleues (Bastien)

    def tester_bleue(it, x, codes, notes, eti_url, eti_img):
        """Test silencieux (remplace le calibrage) : une alerte bleue est une vraie paire (titre exact).
        On demande quand même à l'IA ; le récap dit combien elle en reconnaît. Rien sur Discord."""
        etat["ia_n"] += 1
        etat["ia_np"] = etat.get("ia_np", 0) + 1
        photos = photos_pour_ia(notes, eti_url)
        if not photos:
            return
        with hors_verrou():
            v = ia.verifier(pt.nom(x), codes, refs_img[x], photos, eti_img, "",
                            [pt.nom(m) for m in voisins_de(x)])      # sans le titre : on juge la photo seule
        compter(v)
        if v.get("erreur"):
            return
        r = rec()
        r["test_bleues"] = r.get("test_bleues", 0) + 1
        bon = v["verdict"] == "oui" and v["confiance"] >= conf_min
        r["test_bleues_oui"] = r.get("test_bleues_oui", 0) + bon
        if not bon:
            r.setdefault("test_bleues_ratees", []).append(f"{x} ({v['verdict']} {v['confiance']} %)")
        print(f"[test bleue] {it['id']} {x} : {v}")

    ia_2e_appel = os.environ.get("IA_2E_APPEL", "1") == "1"      # 11/11 : 2e appel gardé (Bastien) ; 0 = incertain -> jaune
    ia_rapide_oui = int(os.environ.get("IA_RAPIDE_OUI") or 85)    # 1er appel (3 photos) : OUI à 85 % ou plus = fini
    ia_rapide_non = int(os.environ.get("IA_RAPIDE_NON") or 90)    # NON à 90 % ou plus = fini

    def urls_utiles(urls):
        """11/11 : 6 photos par annonce (avant 20) : les 5 premières + la dernière (souvent l'étiquette)."""
        return urls if len(urls) <= 6 else urls[:5] + urls[-1:]

    def photos_pour_ia(notes, eti_url):
        """La photo qui ressemble le plus aux références en premier (l'IA regarde la 1re) :
        si la 1re photo de l'annonce est la boîte ou la semelle, l'IA voit quand même la paire."""
        return [t[3] for t in sorted(notes, key=lambda t: -t[4]) if t[2] != eti_url][:int(os.environ.get("IA_PHOTOS") or 6)]

    def cle_doublon(it, x):
        """Même paire, même prix, même taille, même vendeur (ou même titre) : une remise en vente."""
        vend = pt.vendeur(it["id"]) or {}
        taille = (it.get("details") or "?").partition(" · ")[0].strip()
        qui = vend.get("id") or " ".join(bot.tokens(it.get("title")))
        return f"{x}|{bot.price_value(it)}|{taille}|{qui}"

    def deja_alertee(cle):
        d = etat.setdefault("alertes_cles", {})
        limite = time.time() - 7 * 86400
        for k in [k for k, t in d.items() if t < limite]:
            del d[k]
        return cle in d

    def passage(items=None, mods=None):
        if items is None:
            with hors_verrou():
                items = nouvelles(premier=False)
        mods = [x for x in (mods or modeles_juges) if x in refs]
        if exclure_re:
            items = [it for it in items if not exclure_re.search(bot.norm(it.get("title") or ""))]
        print(f"{time.strftime('%H:%M:%S')} : {len(items)} annonce(s) à juger ({', '.join(mods)})")
        rec()["annonces"] += len(items)
        if not items or not mods:
            return
        # 3. Étape 1 (gratuite) : présélection par la photo ou par le titre, dans le prix.
        # Photos téléchargées en parallèle et comparées d'un coup (plus rapide).
        a_juger = [it for it in items if not all(autre_marque(it.get("title"), x) for x in mods)]
        with hors_verrou():
            imgs = telecharger([it["photo"] for it in a_juger])
        ok = [(it, im) for it, im in zip(a_juger, imgs) if im is not None]
        embs = emb_lots([im for _, im in ok])
        alertes, douteuses = [], []
        for (it, im1), e in zip(ok, embs):
            it["im1"] = im1          # 11/11 : 1re photo gardée pour l'appel IA rapide
            e = e.unsqueeze(0)
            meilleur, proche, possible = choisir(it, e, mods)
            if meilleur:
                it["modele"], it["score"], it["e_best"] = meilleur
                alertes.append(it)
            elif possible and (proche or pas_une_chaussure(e)):
                it["e1"] = e
                douteuses.append(it)
        # 1re photo trompeuse (boîte, semelle, étiquette) ou presque au seuil : on regarde aussi la 2e photo
        douteuses.sort(key=lambda it: -it.get("proche_s", 0))
        for it in douteuses[:photo2_max]:
            try:
                with hors_verrou():
                    it["urls"] = pt.photos_annonce(session, it)
            except Exception as ex:
                print(f"Annonce illisible {it['id']} : {ex}")
                continue
            autres = [u for u in it["urls"] if pt.jeton(u) != pt.jeton(it["photo"])][:1]
            with hors_verrou():
                im2 = telecharger(autres)
            if not im2 or im2[0] is None:
                continue
            e = torch.cat([it["e1"], emb([im2[0]])])
            meilleur, _, _ = choisir(it, e, mods)
            if meilleur:
                it["modele"], it["score"], it["e_best"] = meilleur
                alertes.append(it)
                rec()["photo2"] = rec().get("photo2", 0) + 1
                print(f"[photo2] {it['id']} {meilleur[0]} trouvée grâce à la 2e photo ({meilleur[1]:.3f})")
        alertes.sort(key=lambda it: (titre_dit(it, it["modele"]), it["score"]), reverse=True)
        for it in alertes:
            it["t_tri"] = time.time()
        rec()["candidates"] += len(alertes)
        if len(alertes) > ia_max:
            print(f"{len(alertes) - ia_max} candidate(s) au-delà de {ia_max} non vérifiées ce passage")
            for it in alertes[ia_max:]:
                if not titre_dit(it, it["modele"]):
                    rec()["loupees"] += 1
                    rec(it["modele"])["loupees"] += 1
            alertes = alertes[:ia_max]
        print(f"{len(alertes)} candidate(s) à vérifier")
        # 4. Étape 2 : étiquette dans les autres photos, puis envoi
        # 1/11 : jusqu'à 5 candidates vérifiées EN MÊME TEMPS (avant : l'une après l'autre).
        # Les parties lentes (page de l'annonce, photos, IA) tournent en parallèle ; le reste
        # (CLIP, compteurs, envoi Discord) passe une candidate à la fois grâce au verrou.
        def _traiter(it):
            x = it["modele"]
            codes = pt.CODES.get(x, [])
            eti_url, statut = None, "pas de photo d'étiquette : à demander au vendeur"
            # 11/11 : appel IA rapide lancé TOUT DE SUITE sur la 1re photo (déjà téléchargée), pendant que
            # la page de l'annonce se charge. Réponse nette -> l'alerte part sans attendre les autres photos.
            fut1 = None
            if (ia.dispo() and it.get("im1") is not None and not titre_nomme(it, x) and ia_budget()[0]
                    and rec(x)["ia"] < ia_paire_max and not it.get("sonde")):
                it["t_ia0"] = time.time()
                vois1 = [pt.nom(m) for m in voisins_de(x)]

                def _appel1(im1=it["im1"]):
                    v = ia.verifier(pt.nom(x), codes, refs_img[x], [im1], None, it.get("title", ""), vois1, court=True)
                    it["t_ia"] = time.time()
                    return v
                fut1 = pool_ia.submit(_appel1)
            urls = it.get("urls") or []
            for essai in range(0 if urls else 2):      # 2e essai si la page de l'annonce n'a pas donné ses photos
                try:
                    with hors_verrou():
                        urls = pt.photos_annonce(session, it)
                except Exception as ex:
                    print(f"Annonce illisible {it['id']} : {ex}")
                if urls:
                    break
                with hors_verrou():
                    time.sleep(3)
            it["t_page"] = time.time()
            print(f"[diag] annonce {it['id']} : {len(urls)} photo(s) lue(s)")
            cle = cle_doublon(it, x)
            if deja_alertee(cle):
                rec()["doublons"] = rec().get("doublons", 0) + 1
                print(f"[doublon] {it['id']} {x} : déjà alertée (remise en vente), ignorée")
                return
            sans = PRIX_SANS.get(x)
            if sans is not None and (bot.price_value(it) or 0) > sans:
                bx = pt.boite(it["id"], it.get("title", ""))
                if bx != "Og Box":      # « Non précisé » compte comme sans boîte
                    rec()["sans_boite"] = rec().get("sans_boite", 0) + 1
                    rec(x)["sans_boite"] = rec(x).get("sans_boite", 0) + 1
                    print(f"[boîte] {it['id']} {x} : {bot.price_value(it)} € > {sans:g} € sans boîte, boîte : {bx} -> ignorée")
                    return
            notes = []
            v1, net1 = None, False
            if fut1 is not None:
                with hors_verrou():
                    v1 = fut1.result()
                net1 = (not v1.get("erreur") and (
                    (v1["verdict"] == "oui" and v1["confiance"] >= ia_rapide_oui)
                    or (v1["verdict"] == "non" and v1["confiance"] >= ia_rapide_non)))
                print(f"[IA rapide] {it['id']} {x} : {v1} · {'nette' if net1 else 'pas nette'}")
            bleu_direct = titre_nomme(it, x)      # V4 : titre exact -> envoi direct, sans photos ni IA
            if bleu_direct or net1:                # 11/11 : réponse IA nette -> pas besoin des autres photos
                imgs_annonce = []
            else:
                with hors_verrou():
                    imgs_annonce = telecharger(urls_utiles(urls))
            it["t_photos"] = time.time()
            lus = [(u, im) for u, im in zip(urls_utiles(urls), imgs_annonce) if im is not None]
            if lus:
                # Toutes les photos de l'annonce analysées d'un coup (1/11 : plus rapide qu'une par une)
                F = emb_lots([im for _, im in lus])
                p_etis = (100 * F @ te.T).softmax(dim=-1)[:, 0].tolist()
                s_etis = (F @ etis[x].T).max(dim=1).values.tolist() if etis.get(x) is not None else [0] * len(lus)
                s_refs = (F @ refs[x].T).max(dim=1).values.tolist()
                for (u, im), p_eti, s_eti, s_ref in zip(lus, p_etis, s_etis, s_refs):
                    notes.append((max(p_eti, s_eti), p_eti >= 0.5 or s_eti >= 0.80, u, im, float(s_ref)))
            notes.sort(key=lambda t: t[0], reverse=True)
            eti_img = None
            if notes and notes[0][1]:
                eti_url, eti_img = notes[0][2], notes[0][3]
                statut = "trouvée, mais code illisible : à vérifier à l'oeil"
            titre_ok = titre_dit(it, x)

            # Étape 2 : l'IA de vision regarde vraiment les photos
            verdict = None
            sans_ia = "pas de clé IA"
            ia_ok, ia_pourquoi = ia_budget()
            if titre_nomme(it, x):
                sans_ia = "titre exact : pas besoin d'IA"     # le titre dit exactement le nom de la paire
            elif ia.dispo() and not ia_ok:
                if not titre_ok:
                    rec()["loupees"] += 1
                    rec(x)["loupees"] += 1
                    return      # nuit ou budget de l'heure atteint : seulement les titres qui nomment la paire
                sans_ia = ia_pourquoi
            elif ia.dispo() and rec(x)["ia"] >= ia_paire_max and not titre_ok:
                rec(x)["plafond"] = rec(x).get("plafond", 0) + 1
                return          # cette paire a eu sa part d'IA aujourd'hui : on garde le budget pour les autres
            elif ia.dispo():
                etat["ia_n"] += 1
                etat["ia_np"] = etat.get("ia_np", 0) + 1
                voisins = voisins_de(x)
                if v1 is not None and not v1.get("erreur") and (net1 or not ia_2e_appel or not notes):
                    verdict = v1      # 11/11 : réponse de l'appel rapide (sans 2e appel : incertain -> alerte jaune)
                elif v1 is not None and not v1.get("erreur"):
                    compter(v1)       # 2e appel avec les 6 photos (IA_2E_APPEL=1)
                    rec()["ia_2e"] = rec().get("ia_2e", 0) + 1
                    with hors_verrou():
                        verdict = ia.verifier(pt.nom(x), codes, refs_img[x], photos_pour_ia(notes, eti_url), None,
                                              it.get("title", ""), [pt.nom(m) for m in voisins], court=True)
                        it["t_ia"] = time.time()
                else:
                    with hors_verrou():
                        photos = photos_pour_ia(notes, eti_url) or [Image.open(io.BytesIO(session.get(it["photo"], timeout=20).content)).convert("RGB")]
                        it["t_ia0"] = time.time()
                        # 11/11 : IA en 2 temps. 1er appel rapide avec 3 photos ; réponse nette -> on s'arrête là,
                        # sinon 2e appel avec les 6 photos.
                        verdict = ia.verifier(pt.nom(x), codes, refs_img[x], photos[:3], None, it.get("title", ""), [pt.nom(m) for m in voisins], court=True)
                        net = (not verdict.get("erreur") and (
                            (verdict["verdict"] == "oui" and verdict["confiance"] >= ia_rapide_oui)
                            or (verdict["verdict"] == "non" and verdict["confiance"] >= ia_rapide_non)))
                        if len(photos) > 3 and not net and not verdict.get("erreur"):
                            compter(verdict)
                            etat["ia_n"] += 1
                            etat["ia_np"] = etat.get("ia_np", 0) + 1
                            rec()["ia_2e"] = rec().get("ia_2e", 0) + 1
                            verdict = ia.verifier(pt.nom(x), codes, refs_img[x], photos, None, it.get("title", ""), [pt.nom(m) for m in voisins], court=True)
                        it["t_ia"] = time.time()
                compter(verdict)
                print(f"IA {it['id']} {x} : {verdict}")
                if not verdict.get("erreur"):
                    rp = rec(x)
                    rp["ia"] += 1
                    ok = verdict["verdict"] == "oui" and verdict["confiance"] >= conf_min
                    rp["oui" if ok else ("non" if verdict["verdict"] == "non" else "incertain")] += 1
                if verdict.get("erreur"):
                    if not titre_ok:
                        return
                    sans_ia = "erreur : " + str(verdict["erreur"])[:150]
                    verdict = None
                else:
                    if it.get("score", 0) > 0:     # tous les verdicts IA servent à apprendre le seuil de la paire
                        apprendre(x, it["score"], verdict)
                    if (verdict["verdict"] == "non" and verdict["confiance"] >= 80 and not titre_ok
                            and it.get("e_best") is not None):
                        retenir_non(x, it["e_best"])
                    if it.get("sonde") and (verdict["verdict"] != "oui" or verdict["confiance"] < conf_min):
                        return         # test silencieux refusé : rien sur Discord
                if verdict and verdict["verdict"] in ("oui", "incertain") and not it.get("sonde"):
                    it["eti_champ"] = controle_etiquette(it, x, codes, eti_img, verdict)
                if verdict and (verdict["verdict"] != "oui" or verdict["confiance"] < conf_min):
                    if not it.get("sonde") and (verdict["verdict"] == "incertain" or verdict["verdict"] == "oui"):
                        envoyer_incertaine(it, x, verdict, urls, eti_url)
                        etat["alertes_cles"][cle] = time.time()
                        return
                    if voir_refus:      # montrer aussi les refus (en gris) pour juger l'IA
                        post(webhook, {"embeds": [{
                            "title": it["title"][:250], "url": it["url"], "color": 0x95A5A6,
                            "author": {"name": f"IA : refusée ({x} ?)"},
                            "fields": [{"name": "Prix", "value": prix_champ(it), "inline": True},
                                       {"name": "IA", "value": f"{verdict['verdict'].upper()} à {verdict['confiance']} %", "inline": True}],
                            "thumbnail": {"url": it["photo"]}}]})
                    return
                elif verdict and verdict.get("code_lu"):
                    lu = verdict["code_lu"].upper().replace(" ", "-")
                    bon = any(c.replace("-", "") in lu.replace("-", "") for c in codes)
                    statut = f"lue par l'IA : {verdict['code_lu']} = " + ("BON CODE" if bon else "AUTRE CODE (à vérifier)")
            elif not titre_ok:
                return          # sans IA : plus d'alerte sur la photo seule (trop de fausses alertes)
            if not verdict and not titre_exact(it, x):
                print(f"Sans IA, titre pas exact : {it['id']} « {it.get('title')} » ({x}) non envoyée")
                return          # sans IA : seulement si le titre dit exactement la paire (pas de faute tolérée)

            bleu = sans_ia.startswith("titre exact")
            if verdict:
                origine = "Trouvée par le bot IA (photos vérifiées)"
            elif bleu:
                origine = "Trouvée par le bot titre (titre exact, pas de contrôle IA)"
            else:
                origine = f"Trouvée par le bot titre, sans IA ({sans_ia})"
            vend = pt.vendeur(it["id"]) or {}
            if vend.get("avis") is not None:
                avis = f"{vend['avis']} avis" + (f" · {vend['etoiles']}/5" if vend.get("etoiles") is not None else "")
            else:
                avis = "?"
            champs = [{"name": "Prix", "value": prix_champ(it), "inline": True},
                      {"name": "Taille · état", "value": it.get("details", "?"), "inline": True},
                      {"name": "Boîte", "value": pt.boite(it["id"], it.get("title", "")), "inline": True},
                      {"name": "Vendeur", "value": pt.champ_vendeur(vend), "inline": True}]
            if verdict:
                champs.append({"name": "IA", "value": f"OUI à {verdict['confiance']} % (seuil {conf_min} %)",
                               "inline": True})
            else:
                champs.append({"name": "IA", "value": "non vérifiée : regarde les photos", "inline": True})
            if it.get("eti_champ"):
                champs.append({"name": "Étiquette", "value": it["eti_champ"], "inline": False})
            if it.get("score"):
                champs.append({"name": "Ressemblance photo",
                               "value": f"{it['score']:.2f} (seuil {pre_de.get(x, pre):.2f})", "inline": True})
            embeds = [{
                "title": x, "url": it["url"],
                "author": {"name": origine},
                "footer": {"text": f"Réf {it['id']} · ton avis : réponds oui / non / fake"},
                "color": 0x2ECC71 if verdict else (0x3498DB if bleu else 0xE67E22),
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
            comps = bot.boutons(it)
            coul = "VERT" if verdict else ("BLEU" if bleu else "ORANGE")
            texte = resume(it, x, coul, verdict)
            # 11/11 : Discord envoyé sans bloquer les autres candidates ; la notification du site part en arrière-plan
            with hors_verrou():
                if verdict and mention:      # vraie alerte vérifiée par l'IA : notification Discord
                    bot.envoyer(webhook, {"content": texte + f" {mention}", "embeds": embeds, "components": comps,
                                          "allowed_mentions": {"parse": ["everyone", "users", "roles"]}})
                else:
                    bot.envoyer(webhook, {"content": texte, "embeds": embeds, "components": comps})
            pool.submit(notifier_site, texte, it, x, coul, urls, eti_url, verdict)
            rec()["vert" if verdict else ("bleu" if sans_ia.startswith("titre exact") else "orange")] += 1
            etat["alertes_cles"][cle] = time.time()
            # Bastien peut aussi répondre oui / non / fake aux vertes, bleues et oranges (même traitement que les jaunes)
            a_t = etat.setdefault("a_trancher", {})
            a_t[str(it["id"])] = {"paire": x, "s": round(float(it.get("score") or 0), 3), "photo": it["photo"],
                                  "date": datetime.now(paris).isoformat(timespec="minutes"), "url": it["url"],
                                  "eti": eti_url, "eti_test": it.get("eti_test"),
                                  "type": "vert" if verdict else ("bleu" if bleu else "orange")}
            for k in list(a_t)[:-500]:
                del a_t[k]
            if bleu and test_bleues and ia.dispo() and ia_budget()[0]:
                tester_bleue(it, x, codes, notes, eti_url, eti_img)
            try:      # même alerte pour l'onglet COP sneakers du site
                taille, _, etat_paire = (it.get("details") or "?").partition(" · ")
                a_publier.append({
                    "id": str(it["id"]), "date": datetime.now(paris).isoformat(timespec="seconds"),
                    "type": "vert" if verdict else ("bleu" if sans_ia.startswith("titre exact") else "orange"),
                    "paire": x, "famille": famille(x), "titre": it.get("title", ""), "prix": it.get("price"),
                    "taille": taille.strip() or "?", "etat": etat_paire.strip() or "?", "url": it["url"],
                    "photos": (urls or [it["photo"]])[:20], "etiquette": eti_url,
                    "ia": ({"verdict": verdict["verdict"], "confiance": verdict["confiance"]} if verdict else None),
                    "ressemblance": round(float(it.get("score") or 0), 3), "seuil": pre_de.get(x),
                    "titre_dit": bool(titre_ok), "vendeur": pt.vendeur(it["id"])})
            except Exception as ex:
                print(f"::warning::alerte non publiée pour le site : {str(ex)[:200]}")

        def traiter(it):
            try:
                with sous_verrou():
                    _traiter(it)
            except Exception as ex:
                print(f"::warning::Erreur sur l'annonce {it.get('id')} : {str(ex)[:300]}")

        # 1/11 : on n'attend plus la fin des vérifications ; la boucle repart chercher tout de suite
        for it in alertes:
            pool_cand.submit(traiter, it)


    # Calibrage sans prix : une recherche Jordan SANS prix max, de temps en temps. Les annonces juste sous
    # le seuil d'une paire sont vérifiées en silence par l'IA pour apprendre son seuil. Jamais d'alerte ici.
    recherches_c = [(bot.page_url(pt.BASE_URL + f), xs) for f, xs in familles.items()]
    calib_intervalle = int(os.environ.get("CALIB_INTERVALLE") or 900)

    def calibrer():
        jour = time.strftime("%Y-%m-%d")
        if etat.get("sonde_jour") != jour or not isinstance(etat.get("sonde_n"), dict):
            etat["sonde_jour"], etat["sonde_n"] = jour, {}
        besoin = [x for x in modeles if x in refs
                  and pre_de[x] > seuil_min      # au plancher, le calibrage ne peut rien faire descendre
                  and etat.setdefault("sonde_total", {}).get(x, 0) < sonde_total_max
                  and etat["sonde_n"].get(x, 0) < sonde_jour_max]
        if not sonde_jour_max or not besoin or not ia.dispo() or not ia_budget()[0]:
            return
        lot = []
        for (base_c, purl_c), xs in recherches_c:
            if not any(x in besoin for x in xs):
                continue
            try:
                lot += bot.fetch_items(session, base_c, purl_c)
            except Exception as e:
                print(f"[calib] page illisible : {e}")
        deja = set(etat.setdefault("calib_ids", []))
        tests = []
        for it in lot:
            iid = str(it["id"])
            if iid in deja or not it.get("photo"):
                continue
            etat["calib_ids"].append(iid)
            try:
                img = Image.open(io.BytesIO(session.get(it["photo"], timeout=20).content)).convert("RGB")
            except Exception:
                continue
            e = emb([img])
            choix = None
            for x in besoin:
                if autre_marque(it.get("title"), x):
                    continue
                s = float((e @ refs[x].T).max())
                ecart = pre_de[x] - s
                if 0 < ecart <= sonde_ecart and (choix is None or ecart < choix[2]):
                    choix = (x, s, ecart)
            if choix:
                tests.append((it, choix[0], choix[1]))
        etat["calib_ids"] = etat["calib_ids"][-5000:]
        for it, x, s in sorted(tests, key=lambda t: pre_de[t[1]] - t[2])[:3]:
            if (etat["sonde_n"].get(x, 0) >= sonde_jour_max or etat["sonde_total"].get(x, 0) >= sonde_total_max
                    or not ia_budget()[0]):
                continue
            etat["sonde_n"][x] = etat["sonde_n"].get(x, 0) + 1
            etat["sonde_total"][x] = etat["sonde_total"].get(x, 0) + 1
            etat["ia_n"] += 1
            etat["ia_np"] = etat.get("ia_np", 0) + 1
            try:
                urls = pt.photos_annonce(session, it)
            except Exception:
                urls = []
            photos = []
            for u in (urls or [it["photo"]])[:4]:
                try:
                    photos.append(Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB"))
                except Exception:
                    pass
            if not photos:
                continue
            verdict = ia.verifier(pt.nom(x), pt.CODES.get(x, []), refs_img[x], photos, None, it.get("title", ""),
                                  [pt.nom(m) for m in voisins_de(x)])
            compter(verdict)
            print(f"[calib] {it['id']} {x} score {s:.3f} ({it.get('price')}) : {verdict}")
            rec()["calib"] += 1
            if not verdict.get("erreur"):
                apprendre(x, s, verdict)
            if etat["sonde_total"][x] == sonde_total_max:
                print(f"[auto] {x} calibrée : {sonde_total_max} tests faits, seuil {pre_de[x]:.3f}")

    # Mesure : score de vraies paires Vinted (fichier mesure.txt, une ligne « Paire | URL »).
    # Résultats dans photo_seen.json → "mesures". Sert à savoir jusqu'où monter les seuils.
    f_mesure = Path(__file__).with_name("mesure.txt")
    if f_mesure.exists():
        faites = etat.setdefault("mesures", {})
        for ligne in f_mesure.read_text().splitlines():
            if "|" not in ligne:
                continue
            x, url = (t.strip() for t in ligne.split("|", 1))
            url = url.split("?")[0]
            if x not in refs or url in faites:
                continue
            try:
                page = session.get(url, timeout=30).text
                m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page) or \
                    re.search(r'content="([^"]+)"[^>]+property="og:image"', page)
                premiere = m.group(1).replace("&amp;", "&") if m else None
                urls = pt.photos_annonce(session, {"id": url, "url": url, "photo": premiere}) if premiere else []
                urls = urls or ([premiere] if premiere else [])
                scores = []
                for u in urls[:8]:
                    im = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
                    scores.append(round(float((emb([im]) @ refs[x].T).max()), 3))
                faites[url] = {"paire": x, "seuil": pre_de[x], "photo1": scores[0] if scores else None,
                               "toutes": scores}
                print(f"[mesure] {x} {url} : {faites[url]}")
            except Exception as ex:
                faites[url] = {"paire": x, "erreur": str(ex)[:200]}
                print(f"[mesure] {x} {url} : erreur {ex}")
        sauver()
        if git_auto:
            git_sauver()

    if rattrapage:
        try:
            avec_verrou(lambda: passage(rattrapage, [x for x in nouveaux_modeles if x in refs]))
        except Exception as e:
            print(f"::warning::Erreur pendant le rattrapage : {str(e)[:300]}")

    debut = time.time()
    dernier_git = time.time()
    nb_passages = 0
    dernier_calib = 0
    dernier_discord = [0.0]
    while True:
        t0 = time.time()
        # 1/11 : chaque étape qui touche l'état passe sous le verrou (les vérifications IA tournent
        # en arrière-plan en même temps) ; les recherches et téléchargements, eux, le relâchent.
        try:
            if BOT == 1 or time.time() - dernier_discord[0] >= 20:     # bots 2 et 3 : Discord lu toutes les 20 s
                dernier_discord[0] = time.time()
                avec_verrou(lire_discord)
        except Exception as e:
            print(f"::warning::Erreur lecture Discord : {str(e)[:300]}")
        try:
            avec_verrou(appliquer_bastien)
        except Exception as e:
            print(f"::warning::Erreur réponses de Bastien : {str(e)[:300]}")
        try:
            avec_verrou(passage)
        except Exception as e:
            print(f"::warning::Erreur pendant le passage : {str(e)[:300]}")
        if time.time() - dernier_calib >= calib_intervalle:
            dernier_calib = time.time()
            try:
                avec_verrou(calibrer)
            except Exception as e:
                print(f"::warning::Erreur pendant le calibrage : {str(e)[:300]}")
        try:
            avec_verrou(recap)
        except Exception as e:
            print(f"::warning::Erreur pendant le récap : {str(e)[:300]}")
        avec_verrou(sauver)
        avec_verrou(sauver_pieges)
        nb_passages += 1
        publier_en_attente()
        if git_auto and time.time() - dernier_git > 300:
            print(f"::notice::{nb_passages} passages, {len(etat['ids'])} annonces vues")
            git_sauver()
            dernier_git = time.time()
        if time.time() - debut + pause > duree:
            break
        time.sleep(max(0.2, pause - (time.time() - t0)))
    pool_cand.shutdown(wait=True)      # fin du passage : on laisse finir les vérifications en cours
    publier_en_attente()
    avec_verrou(sauver)
    avec_verrou(sauver_pieges)
    if git_auto:
        git_sauver()


if __name__ == "__main__":
    main()
