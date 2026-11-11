"""Vérification d'une annonce par une IA de vision (API Claude).

L'IA reçoit 2 photos de référence (512 px) + la 1re photo de l'annonce (512 px) = niveau 1,
+ la photo d'étiquette (1024 px) si elle est trouvée = niveau 2,
et répond : est-ce exactement ce coloris ? Avec la confiance, le code lu sur l'étiquette et une raison.

Variables : ANTHROPIC_API_KEY (secret GitHub), IA_MODELE (défaut claude-haiku-5-5).
"""
import base64
import io
import json
import os
import re

import requests

API = "https://api.anthropic.com/v1/messages"
_S = requests.Session()      # 11/11 : même connexion réutilisée (pas de nouvelle poignée de main à chaque appel)


def dispo():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


_REFS = {}


def _ref(im, cote=512):
    """11/11 : photos de référence préparées une seule fois (elles servent à chaque appel)."""
    k = (id(im), cote)
    if k not in _REFS:
        _REFS[k] = (im, _img(im, cote))     # on garde l'image pour que son id ne soit pas réutilisé
    return dict(_REFS[k][1])


def _img(im, cote=768):
    im = im.convert("RGB")
    im.thumbnail((cote, cote))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(b.getvalue()).decode()}}


_AMORCE = os.environ.get("IA_AMORCE", "1") == "1"


def verifier(modele, codes, refs, photos, etiquette=None, titre="", voisins=(), court=False):
    """court=True (11/11, appel rapide) : l'IA ne répond que verdict + confiance (réponse plus courte, plus rapide)."""
    """refs / photos / etiquette : images PIL. Renvoie {"verdict", "confiance", "code_lu", "raison"} ou {"erreur"}."""
    code = ", ".join(codes) if codes else "inconnu"
    contenu = [{"type": "text", "text": f"REFERENCE photos of a genuine {modele} (style code {code}):"}]
    contenu += [_ref(r, 512) for r in refs[:2]]
    # 11/11 : références + consigne de début gardées en cache chez Anthropic (moins cher, plus rapide).
    # Ignoré sans erreur si le bloc est trop petit pour le cache.
    contenu[-1]["cache_control"] = {"type": "ephemeral"}
    contenu.append({"type": "text", "text": f"PHOTOS from a second-hand listing titled \"{titre}\":"})
    # Photos de l'annonce en 512 px, la plus ressemblante en 1re (11/11 : 6 max, avant 20)
    contenu += [_img(p, 512) for p in photos[:int(os.environ.get("IA_PHOTOS") or 6)]]
    if etiquette is not None:                            # niveau 2 : + l'étiquette, en 1024 px pour lire le code
        contenu.append({"type": "text", "text": "Photo that may show the size tag inside the shoe:"})
        contenu.append(_img(etiquette, 1024))
    autres = (" Be careful with look-alikes such as: " + ", ".join(voisins) + ".") if voisins else ""
    contenu.append({"type": "text", "text": (
        f"Question: is the shoe in the listing photos exactly the {modele} shown in the references "
        f"(same model and same colors on every part: upper, overlays, laces, eyelets, midsole, outsole, heel; not another colorway, "
        f"not another model, not a kids/GS size issue)?{autres} "
        "If a style code is readable on the size tag, read it: a different style code means NO. "
        "Ignore wear, dirt, lighting and box. If the photos do not show the shoe well enough, answer \"incertain\".\n"
        "Reply with JSON only, no other text: "
        + ("{\"verdict\": \"oui\" | \"non\" | \"incertain\", \"confiance\": 0-100}" if court else
           "{\"verdict\": \"oui\" | \"non\" | \"incertain\", \"confiance\": 0-100, \"code_lu\": \"style code read on the tag, or empty\", "
           "\"tailles\": \"sizes printed on the tag exactly as read, e.g. US 9 UK 8 EUR 42.5 CM 27, or empty\"}"))})
    global _AMORCE
    try:
        # 1/11 (V3) : réponse courte. L'IA commence directement par « { » (amorce) et s'arrête au « } »
        # de fin : plus de texte autour du JSON (avant : ~300 mots-machine écrits par vérification, ~60 utiles).
        def appel(amorce):
            msgs = [{"role": "user", "content": contenu}]
            if amorce:
                msgs.append({"role": "assistant", "content": "{"})
            return _S.post(API, timeout=60, headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": os.environ.get("IA_MODELE") or "claude-haiku-5-5",
                      "max_tokens": int(os.environ.get("IA_MAX_TOKENS") or 1500),   # 1/11 : 300 coupait la réponse (INCERTAIN 0 %)
                      "stop_sequences": ["}"], "messages": msgs})
        amorce = _AMORCE
        r = appel(amorce)
        if r.status_code == 400 and amorce:      # modèle qui refuse l'amorce : on continue sans, pour de bon
            print(f"[IA] amorce refusée, on continue sans : {r.text[:200]}")
            _AMORCE = amorce = False
            r = appel(False)
        if r.status_code != 200:
            return {"erreur": f"HTTP {r.status_code} {r.text[:200]}"}
        u = r.json().get("usage") or {}
        conso = {"entree": int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                 + int(u.get("cache_creation_input_tokens") or 0), "sortie": int(u.get("output_tokens") or 0)}
        texte = "".join(b.get("text", "") for b in r.json().get("content", []))
        if amorce:
            texte = "{" + texte
        if r.json().get("stop_reason") == "stop_sequence":
            texte += "}"
        m = re.search(r"\{.*\}", texte, re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
        if not d:
            # Réponse coupée (max_tokens) : on récupère au moins le verdict et la confiance s'ils sont écrits
            v0 = re.search(r'"verdict"\s*:\s*"(oui|non|incertain)"', texte)
            c0 = re.search(r'"confiance"\s*:\s*(\d+)', texte)
            if v0 and (c0 or v0.group(1) == "non"):
                d = {"verdict": v0.group(1), "confiance": int(c0.group(1)) if c0 else 70,
                     "raison": "réponse IA coupée, verdict récupéré"}
        if not d:
            # Réponse illisible : on la garde pour comprendre (affichée dans « Pourquoi »)
            print(f"[IA] réponse illisible : {texte[:300]!r} · stop={r.json().get('stop_reason')}")
            return {"verdict": "incertain", "confiance": 0, "code_lu": "", "conso": conso,
                    "raison": "réponse IA illisible : " + (texte.strip()[:150] or f"vide (stop={r.json().get('stop_reason')})")}
        v = str(d.get("verdict", "")).lower().strip()
        return {"verdict": v if v in ("oui", "non", "incertain") else "incertain",
                "confiance": int(float(d.get("confiance") or 0)), "code_lu": str(d.get("code_lu") or "").strip(),
                "tailles": str(d.get("tailles") or "").strip()[:80],
                "raison": str(d.get("raison") or "").strip()[:200], "conso": conso}
    except Exception as e:
        return {"erreur": str(e)[:200]}


def etiquette(modele, codes, vraies, fakes, photo):
    """2/11 : test de typographie. vraies / fakes : étiquettes de référence (PIL), photo : étiquette de l'annonce.
    Renvoie {"verdict": "vraie" | "fake" | "incertain", "confiance", "raison", "conso"} ou {"erreur"}."""
    code = ", ".join(codes) if codes else "inconnu"
    contenu = [{"type": "text", "text": f"GENUINE size tags of the {modele} (style code {code}):"}]
    contenu += [_img(v, 1024) for v in vraies[:3]]
    contenu.append({"type": "text", "text": f"COUNTERFEIT size tags of the same shoe ({modele}), confirmed fakes:"})
    contenu += [_img(f, 1024) for f in fakes[:3]]
    contenu.append({"type": "text", "text": "Size tag from a second-hand listing:"})
    contenu.append(_img(photo, 1024))
    contenu.append({"type": "text", "text": (
        "Question: is the listing's size tag genuine or counterfeit? Compare only the printing: font shape, font weight "
        "(boldness), letter size, letter and line spacing, alignment of the columns, position of the text blocks, "
        "barcode and logo. Genuine tags can vary slightly between factories; decide by which group the listing tag "
        "matches best. If the tag is blurry, cut off or another kind of label, answer \"incertain\".\n"
        "Reply with JSON only: {\"verdict\": \"vraie\" | \"fake\" | \"incertain\", \"confiance\": 0-100, "
        "\"raison\": \"main difference seen, in French, max 12 words\"}")})
    try:
        r = requests.post(API, timeout=90, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.environ.get("IA_MODELE") or "claude-haiku-5-5", "max_tokens": 1500,
                  "messages": [{"role": "user", "content": contenu}]})
        if r.status_code != 200:
            return {"erreur": f"HTTP {r.status_code} {r.text[:200]}"}
        u = r.json().get("usage") or {}
        conso = {"entree": int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
                 + int(u.get("cache_creation_input_tokens") or 0), "sortie": int(u.get("output_tokens") or 0)}
        texte = "".join(b.get("text", "") for b in r.json().get("content", []))
        m = re.search(r"\{.*\}", texte, re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
        v = str(d.get("verdict", "")).lower().strip()
        return {"verdict": v if v in ("vraie", "fake", "incertain") else "incertain",
                "confiance": int(float(d.get("confiance") or 0)), "raison": str(d.get("raison") or "").strip()[:120],
                "conso": conso}
    except Exception as e:
        return {"erreur": str(e)[:200]}
