import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qsl, urlencode

import requests

SEEN_FILE = Path(__file__).with_name("seen.json")
MAX_SEEN = 20000
# Annonces « fraîches » seulement : les numéros d'annonce Vinted augmentent avec le temps.
# Une annonce dont le numéro est plus petit que le plus grand numéro vu au passage précédent
# (moins une marge d'environ 2 h) existait déjà : vieille annonce remontée, baisse de prix
# ou Vinted qui mélange ses résultats. On la mémorise sans l'envoyer.
MARGE_IDS = 1_000_000
STATS_FILE = Path(__file__).with_name("stats.json")
TZ = ZoneInfo("Europe/Paris")
RECAP_HOUR = 21
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def page_url(search_url):
    u = urlparse(search_url.strip())
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=False)
         if k not in ("time", "search_id", "page", "order")]
    q.append(("order", "newest_first"))
    return f"{u.scheme}://{u.netloc}", f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


def get_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                      "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8"})
    return s


def extract_items(html):
    full = ""
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", html, re.S):
        try:
            arr = json.loads(m.group(1))
            if len(arr) > 1 and isinstance(arr[1], str):
                full += arr[1]
        except ValueError:
            pass
    key = '"items":{"items":['
    p = full.find(key)
    if p < 0:
        return None
    arr, _ = json.JSONDecoder().raw_decode(full, p + len(key) - 1)
    return arr


def fetch_items(session, base, url):
    r = session.get(url, timeout=30)
    print(f"[diag] page recherche : HTTP {r.status_code} | {len(r.text)} caractères")
    if r.status_code in (401, 403):
        raise PermissionError(f"Vinted a refusé l'accès (HTTP {r.status_code})")
    r.raise_for_status()
    raw = extract_items(r.text)
    if raw is None:
        raise RuntimeError("Annonces introuvables dans la page (Vinted a peut-être changé son site)")
    items = []
    for x in raw:
        p = x.get("productItem") or x
        box = p.get("itemBox") or {}
        photos = p.get("photos") or []
        price = p.get("price") or {}
        items.append({
            "id": p.get("id") or x.get("id"),
            "title": p.get("title") or "",
            "url": base + (p.get("url") or ""),
            "price": f"{price.get('amount', '?')} {price.get('currencyCode', 'EUR')}",
            "brand": box.get("firstLine") or "?",
            "details": box.get("secondLine") or "?",
            "photo": (photos[0].get("url") if photos else None) or p.get("thumbnailUrl"),
        })
    return items


def is_excluded(item, words):
    t = norm(item.get("title"))
    return any(norm(w) in t for w in words if w)


def tokens(s):
    return re.sub(r"[^a-z0-9]+", " ", norm(s)).split()


# Tolérance aux fautes de frappe dans les titres.
# Mots de 4 lettres : 1 lettre remplacée ou 2 lettres inversées. 5 à 7 lettres : 1 faute
# (lettre en trop, en moins, remplacée ou inversée). 8 lettres et plus : 2 fautes.
# Mots de 3 lettres ou moins et chiffres : toujours exacts (jordan 4 ≠ jordan 1).
# Un mot du titre qui est lui-même un mot d'une autre recherche (black/blank, noise/noire,
# pink/pine…) n'est jamais pris pour un autre.
VOCAB = set()


def distance(a, b, limite):
    """Distance de Damerau-Levenshtein, arrêtée dès qu'elle dépasse la limite."""
    if abs(len(a) - len(b)) > limite:
        return limite + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for k in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[k - 1] else 1
            cur[k] = min(prev[k] + 1, cur[k - 1] + 1, prev[k - 1] + c)
            if i > 1 and k > 1 and a[i - 1] == b[k - 2] and a[i - 2] == b[k - 1]:
                cur[k] = min(cur[k], prev2[k - 2] + 1)
        if min(cur) > limite:
            return limite + 1
        prev2, prev = prev, cur
    return prev[-1]


def proche(mot, cible):
    if mot == cible:
        return True
    if re.sub(r"[^0-9]", "", mot) != re.sub(r"[^0-9]", "", cible):
        return False
    lettres = len(re.sub(r"[0-9]", "", cible))
    if lettres < 4 or mot in VOCAB:
        return False
    if lettres == 4:
        return len(mot) == len(cible) and distance(mot, cible, 1) <= 1
    return distance(mot, cible, 1 if lettres < 8 else 2) <= (1 if lettres < 8 else 2)


def has_term(toks, term):
    """Mot ou expression entière ; « airmax » = « air max » ; petites fautes de frappe tolérées."""
    parts = tokens(term)
    target = "".join(parts)
    if not target:
        return True
    for i in range(len(toks)):
        acc = ""
        for j in range(i, min(len(toks), i + len(parts) + 1)):
            acc += toks[j]
            if proche(acc, target):
                return True
            if len(acc) > len(target) + 2:
                break
    return False


def matches_required(item, groups):
    """groups = ['jordan 4 + fire + red', 'j4 + firered'] : un groupe OU l'autre, tous les mots d'un groupe."""
    toks = tokens(item.get("title"))
    return any(all(has_term(toks, t) for t in g.split("+")) for g in groups)


SITE = []     # alertes envoyées pendant ce passage, ajoutées à alertes.json (onglet COP sneakers du site)
FAMILLES = ["Air Max 1", "Jordan 4", "Jordan 1", "Sacai", "Nocta", "NB 2002R", "Hot Step"]


def pour_site(item):
    nom = re.sub(r"\s*\d+\s*€\s*$", "", item.get("search", "")).strip()
    fam = next((f for f in FAMILLES if f.lower() in nom.lower()), "Autre")
    if "patta" in nom.lower():
        fam = "Air Max 1"
    taille, _, etat = (item.get("details") or "?").partition(" · ")
    SITE.append({"id": str(item.get("id")), "date": datetime.now(TZ).isoformat(timespec="seconds"), "type": "titre",
                 "paire": nom or "?", "famille": fam, "titre": item.get("title", ""), "prix": item.get("price"),
                 "taille": taille.strip() or "?", "etat": etat.strip() or "?", "url": item.get("url"),
                 "photos": [item["photo"]] if item.get("photo") else [], "etiquette": None, "ia": None,
                 "ressemblance": None, "seuil": None, "titre_dit": True, "vendeur": None})


def lien_achat(item_id):
    """Page de paiement Vinted de l'annonce (Bastien confirme lui-même le paiement)."""
    return ("https://www.vinted.fr/transaction/buy/new?source_screen=item&transaction%5Bitem_id%5D="
            + str(item_id))


def boutons(item):
    """Boutons sous l'alerte : Acheter (page de paiement) + Voir l'annonce."""
    b = []
    if item.get("id"):
        b.append({"type": 2, "style": 5, "label": "Acheter", "url": lien_achat(item["id"])})
    if item.get("url"):
        b.append({"type": 2, "style": 5, "label": "Voir l'annonce", "url": item["url"]})
    return [{"type": 1, "components": b}] if b else []


def envoyer(webhook, payload):
    """Envoie sur Discord avec les boutons ; si Discord les refuse, renvoie sans boutons (l'alerte part quand même)."""
    url = webhook + ("&" if "?" in webhook else "?") + "with_components=true" if payload.get("components") else webhook
    for _ in range(2):
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        if r.status_code >= 400 and payload.get("components"):
            print(f"::warning::boutons refusés par Discord ({r.status_code}) : {r.text[:200]}")
            payload = {k: v for k, v in payload.items() if k != "components"}
            url = webhook
            continue
        break
    time.sleep(1)


def send_discord(webhook, item):
    photo = item.get("photo")
    nom = re.sub(r"\s*\d+\s*€\s*$", "", item.get("search", "")).strip() or "Vinted"
    embed = {
        "title": nom[:250],
        "url": item.get("url"),
        "color": 0x9B59B6,
        "author": {"name": "Trouvée par le bot titre (pas de contrôle photo)"},
        "fields": [
            {"name": "Prix", "value": item["price"], "inline": True},
            {"name": "Taille · état", "value": item["details"], "inline": True},
        ],
    }
    if photo:
        embed["image"] = {"url": photo}
    envoyer(webhook, {"embeds": [embed], "components": boutons(item)})


def parse_searches(raw):
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("http"):
            parts = ["", line]
        else:
            parts = [p.strip() for p in line.split("|")]
        name = parts[0] or f"Recherche {len(out) + 1}"
        url = parts[1] if len(parts) > 1 else ""
        words = [w.strip() for w in (parts[2] if len(parts) > 2 else "").split(",") if w.strip()]
        required = [w for w in words if not w.startswith("-")]
        excluded = [w[1:].strip() for w in words if w.startswith("-") and w[1:].strip()]
        out.append((name, url, required, excluded))
    return out


def load_state():
    if not SEEN_FILE.exists():
        return None
    data = json.loads(SEEN_FILE.read_text())
    if isinstance(data, list):
        return {"ids": data, "searches": None}
    return data


def price_value(item):
    try:
        return float(str(item.get("price", "")).split()[0].replace(",", "."))
    except (ValueError, IndexError):
        return None


def max_price(url):
    for k, v in parse_qsl(urlparse(url).query):
        if k == "price_to":
            try:
                return float(v)
            except ValueError:
                return None
    return None


def no_price_url(purl):
    """Même recherche sans le prix maximum, pour voir ce qui part trop cher."""
    u = urlparse(purl)
    q = [(k, v) for k, v in parse_qsl(u.query) if k != "price_to"]
    return f"{u.scheme}://{u.netloc}{u.path}?{urlencode(q)}"


def load_stats(today):
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text())
        except ValueError:
            pass
    # Premier lancement du récap : on part de demain 21h.
    return {"last_recap": today, "seen": [], "bases": [], "day": {}, "fresh": True}


def fmt_eur(x):
    return f"{x:.0f} €" if x == int(x) else f"{x:.2f} €".replace(".", ",")


def build_recap(stats, searches, today, launch=False):
    if launch:
        lines = ["**Récap de lancement** — annonces en ligne en ce moment, toutes tailles et prix. Ensuite, récap chaque soir à 21h."]
    else:
        lines = [f"**Récap du jour — {datetime.now(TZ):%d/%m}** (mises en ligne aujourd'hui, tes tailles et états, tous prix)"]
    for name, url, _, _ in searches:
        d = stats["day"].get(name, {})
        prix = sorted((d.get("prix") or {}).values())
        if not prix:
            lines.append(f"**{name}** : 0 postée")
            continue
        niveaux = {}
        for v in prix:
            niveaux[v] = niveaux.get(v, 0) + 1
        moins_cheres = ", ".join(f"{n} à {fmt_eur(v)}" for v, n in sorted(niveaux.items())[:3])
        lines.append(f"**{name}** : {len(prix)} postée{'s' if len(prix) > 1 else ''} · {moins_cheres}")
    msg, out = "", []
    for l in lines:
        if len(msg) + len(l) + 1 > 1900:
            out.append(msg)
            msg = ""
        msg += l + "\n"
    out.append(msg)
    return out


def track_market(session, stats, searches, exclude, launch=False, frontiere=0):
    """Relit chaque recherche sans prix max et compte les annonces du jour."""
    seen = set(stats["seen"])
    bases = set(stats["bases"])
    cache, new_seen = {}, []
    for name, url, required, excl_here in searches:
        mp = max_price(url)
        if mp is None:
            continue
        base, purl = page_url(url)
        npurl = no_price_url(purl)
        if npurl not in cache:
            time.sleep(3)
            try:
                cache[npurl] = fetch_items(session, base, npurl)
                for pg in ((2, 3) if launch else ()):
                    time.sleep(3)
                    try:
                        cache[npurl] += fetch_items(session, base, f"{npurl}&page={pg}")
                    except Exception as e:
                        print(f"Page {pg} illisible [{name}] : {e}")
            except Exception as e:
                print(f"ERREUR récap [{name}] : {e}")
                cache[npurl] = None
        items = cache[npurl]
        if items is None:
            continue
        key = name + "|" + npurl
        first = key not in bases
        bases.add(key)
        d = stats["day"].setdefault(name, {})
        for item in items:
            iid = str(item.get("id"))
            if (name + "|" + iid) in seen:
                continue
            seen.add(name + "|" + iid)
            new_seen.append(name + "|" + iid)
            if first and not launch:
                continue
            if is_excluded(item, exclude + excl_here):
                continue
            if required and not matches_required(item, required):
                continue
            pv = price_value(item)
            if pv is None:
                continue
            if frontiere and iid.isdigit() and int(iid) < frontiere - MARGE_IDS:
                continue                      # vieille annonce remontée : pas une mise en ligne du jour
            d.setdefault("prix", {})[iid] = pv
            if pv <= mp:
                d["in_budget"] = d.get("in_budget", 0) + 1
            else:
                d.setdefault("over", {})[iid] = pv
    stats["seen"] = (stats["seen"] + new_seen)[-60000:]
    stats["bases"] = sorted(bases)


def main():
    raw_hook = os.environ.get("DISCORD_WEBHOOK", "")
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+", raw_hook)
    webhook = m.group(0) if m else ""
    if raw_hook and not webhook:
        sys.exit("Le secret DISCORD_WEBHOOK ne contient pas d'adresse https://discord.com/api/webhooks/... valide.")
    if m and m.group(0) != raw_hook.strip():
        print("[diag] DISCORD_WEBHOOK contenait des caractères en trop : nettoyé automatiquement.")
    searches = parse_searches(os.environ.get("SEARCH_URLS", ""))
    # Les paires suivies par la surveillance photo ne sont plus cherchées ici (évite les alertes en double)
    try:
        cles = re.findall(r'"([^"]+)": \d+', re.search(r"^PRIX = \{(.*?)\}", Path(__file__).with_name(
            "photo_test.py").read_text(), re.S | re.M).group(1))
        suivies = {" ".join(tokens(c)) for c in cles}
        avant = len(searches)
        def suivie(nom):
            n = " ".join(t for t in tokens(re.sub(r"\d+\s*€", "", nom)))
            return any(f" {c} " in f" {n} " or (len(n.split()) >= 2 and f" {n} " in f" {c} ") for c in suivies)
        searches = [x for x in searches if not suivie(x[0])]
        print(f"[diag] {avant - len(searches)} recherche(s) laissée(s) à la surveillance photo, {len(searches)} gardée(s)")
    except Exception as e:
        print(f"[diag] liste de la surveillance photo illisible : {e}")
    exclude = [w.strip() for w in (os.environ.get("EXCLUDE") or "blazer").split(",") if w.strip()]
    for _, _, req, exc in searches:
        for w in req + exc:
            for t in w.split("+"):
                VOCAB.add("".join(tokens(t)))
                VOCAB.update(tokens(t))
    if not webhook or not searches:
        sys.exit("Il manque DISCORD_WEBHOOK ou SEARCH_URLS.")

    state = load_state()
    first_run = state is None
    state = state or {"ids": [], "searches": []}
    if state.get("searches") is None:
        state["searches"] = [page_url(u)[1] for _, u, _, _ in searches]
    # Annonces déjà traitées : seulement celles qui correspondent à une recherche (envoyées ou mémorisées).
    # Liste jamais raccourcie : une annonce déjà envoyée ne revient jamais. (Avant, on gardait les
    # 20 000 dernières annonces lues de toutes sortes, et les plus anciennes étaient oubliées puis renvoyées.)
    if "alertes" not in state:
        state["alertes"] = list(state.get("ids", []))      # migration : tout ce qui était connu reste connu
    seen = set(state["alertes"])
    frontiere = int(state.get("max_id") or 0) or max((int(x) for x in seen if x.isdigit()), default=0)
    plus_grand = frontiere
    trop_vieilles = 0
    known = set(state["searches"])
    new_ids, sent, skipped, errors = [], 0, 0, 0
    session = get_session()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    stats = load_stats(today)

    for idx, (name, url, required, excl_here) in enumerate(searches):
        if idx:
            time.sleep(3)
        base, purl = page_url(url)
        new_search = purl not in known
        try:
            items = fetch_items(session, base, purl)
        except Exception as e:
            errors += 1
            print(f"ERREUR [{name}] : {e}")
            continue
        print(f"[{name}] {len(items)} annonces lues" + (" (nouvelle recherche : mémorisées sans envoi)" if new_search else ""))
        for item in reversed(items):
            iid = str(item.get("id"))
            if iid.isdigit():
                plus_grand = max(plus_grand, int(iid))
            if iid in seen:
                continue
            if is_excluded(item, exclude + excl_here):
                skipped += 1
                continue
            if required and not matches_required(item, required):
                skipped += 1
                continue
            seen.add(iid)
            new_ids.append(iid)
            if first_run or new_search:
                continue
            if frontiere and iid.isdigit() and int(iid) < frontiere - MARGE_IDS:
                trop_vieilles += 1
                print(f"[{name}] ignorée (ancienne annonce) : {item.get('title')} {item.get('url')}")
                continue
            item["search"] = name
            send_discord(webhook, item)
            try:
                pour_site(item)
            except Exception as e:
                print(f"alerte non préparée pour le site : {e}")
            sent += 1
            d = stats["day"].setdefault(name, {})
            d["alerts"] = d.get("alerts", 0) + 1
        known.add(purl)
        if new_search and not first_run:
            requests.post(webhook, json={"content": f"🆕 Nouvelle recherche ajoutée : **{name}**"}, timeout=20)

    if first_run:
        print(f"Premier lancement : {len(new_ids)} annonces mémorisées, rien envoyé.")
        requests.post(webhook, json={"content": "✅ Bot Vinted connecté. Les nouvelles annonces arriveront ici."}, timeout=20)
    print(f"Envoyées : {sent} | anciennes ignorées : {trop_vieilles} | exclues : {skipped} | recherches en erreur : {errors}")

    try:
        track_market(session, stats, searches, exclude, launch=stats.get("fresh", False), frontiere=frontiere)
    except Exception as e:
        print(f"ERREUR récap : {e}")
    if stats.pop("fresh", False):
        for part in build_recap(stats, searches, today, launch=True):
            requests.post(webhook, json={"content": part}, timeout=20)
            time.sleep(1)
        stats["day"] = {}
        print("Récap de lancement envoyé.")
    now = datetime.now(TZ)
    if now.hour >= RECAP_HOUR and stats.get("last_recap") != today:
        for part in build_recap(stats, searches, today):
            requests.post(webhook, json={"content": part}, timeout=20)
            time.sleep(1)
        stats["last_recap"] = today
        stats["day"] = {}
        print("Récap du jour envoyé.")
    STATS_FILE.write_text(json.dumps(stats))
    if SITE:
        Path("/tmp/alertes_titre.json").write_text(json.dumps(SITE, ensure_ascii=False))

    state["alertes"] = state["alertes"] + new_ids
    state.pop("ids", None)
    state["searches"] = sorted(known)
    state["max_id"] = plus_grand
    SEEN_FILE.write_text(json.dumps(state))
    if errors == len(searches):
        sys.exit("Toutes les recherches ont échoué.")


if __name__ == "__main__":
    main()
