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


def has_term(toks, term):
    """Mot ou expression entière ; « airmax » = « air max », « firered » = « fire red »."""
    target = "".join(tokens(term))
    if not target:
        return True
    for i in range(len(toks)):
        acc = ""
        for j in range(i, len(toks)):
            acc += toks[j]
            if acc == target:
                return True
            if len(acc) >= len(target) or not target.startswith(acc):
                break
    return False


def matches_required(item, groups):
    """groups = ['jordan 4 + fire + red', 'j4 + firered'] : un groupe OU l'autre, tous les mots d'un groupe."""
    toks = tokens(item.get("title"))
    return any(all(has_term(toks, t) for t in g.split("+")) for g in groups)


def send_discord(webhook, item):
    photo = item.get("photo")
    embed = {
        "title": item.get("title", "Annonce")[:250],
        "url": item.get("url"),
        "color": 0x007782,
        "author": {"name": item.get("search", "Vinted")},
        "fields": [
            {"name": "Prix", "value": item["price"], "inline": True},
            {"name": "Taille · état", "value": item["details"], "inline": True},
            {"name": "Marque", "value": item["brand"], "inline": True},
        ],
    }
    if photo:
        embed["image"] = {"url": photo}
    r = requests.post(webhook, json={"embeds": [embed]}, timeout=20)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        requests.post(webhook, json={"embeds": [embed]}, timeout=20)
    time.sleep(1)


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
        lines = ["**Récap de lancement** — annonces en ligne en ce moment (3 premières pages de chaque recherche, sans prix max). Le récap du jour arrivera ensuite chaque soir à 21h."]
    else:
        lines = [f"**Récap du jour — {datetime.now(TZ):%d/%m}**"]
    for name, url, _, _ in searches:
        d = stats["day"].get(name, {})
        alerts = d.get("alerts", 0)
        ok = d.get("in_budget", 0)
        over = sorted(d.get("over", {}).values())
        mp = max_price(url)
        line = f"**{name}** : {alerts} alerte{'s' if alerts > 1 else ''}"
        if mp is None:
            lines.append(line + " (pas de prix max)")
            continue
        if over:
            med = over[len(over) // 2]
            line += (f" · {len(over)} trop chère{'s' if len(over) > 1 else ''}"
                     f" (moins chère {fmt_eur(over[0])}, médiane {fmt_eur(med)})")
        else:
            line += " · 0 trop chère"
        if launch:
            line = f"**{name}** : {ok} dans ton prix · " + line.split(" · ", 1)[1]
        elif ok > alerts:
            line += f" · {ok - alerts} dans ton prix mais pas envoyée{'s' if ok - alerts > 1 else ''}"
        lines.append(line)
    lines.append("_Trop chère = bonne paire (mots obligatoires OK) mais au-dessus de ton prix max._")
    msg, out = "", []
    for l in lines:
        if len(msg) + len(l) + 1 > 1900:
            out.append(msg)
            msg = ""
        msg += l + "\n"
    out.append(msg)
    return out


def track_market(session, stats, searches, exclude, launch=False):
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
    exclude = [w.strip() for w in (os.environ.get("EXCLUDE") or "blazer").split(",") if w.strip()]
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
            item["search"] = name
            send_discord(webhook, item)
            sent += 1
            d = stats["day"].setdefault(name, {})
            d["alerts"] = d.get("alerts", 0) + 1
        known.add(purl)
        if new_search and not first_run:
            requests.post(webhook, json={"content": f"🆕 Nouvelle recherche ajoutée : **{name}**"}, timeout=20)

    if first_run:
        print(f"Premier lancement : {len(new_ids)} annonces mémorisées, rien envoyé.")
        requests.post(webhook, json={"content": "✅ Bot Vinted connecté. Les nouvelles annonces arriveront ici."}, timeout=20)
    print(f"Envoyées : {sent} | exclues : {skipped} | recherches en erreur : {errors}")

    try:
        track_market(session, stats, searches, exclude, launch=stats.get("fresh", False))
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

    state["alertes"] = state["alertes"] + new_ids
    state.pop("ids", None)
    state["searches"] = sorted(known)
    SEEN_FILE.write_text(json.dumps(state))
    if errors == len(searches):
        sys.exit("Toutes les recherches ont échoué.")


if __name__ == "__main__":
    main()
