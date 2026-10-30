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


def dispo():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _img(im, cote=768):
    im = im.convert("RGB")
    im.thumbnail((cote, cote))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(b.getvalue()).decode()}}


def verifier(modele, codes, refs, photos, etiquette=None, titre="", voisins=()):
    """refs / photos / etiquette : images PIL. Renvoie {"verdict", "confiance", "code_lu", "raison"} ou {"erreur"}."""
    code = ", ".join(codes) if codes else "inconnu"
    contenu = [{"type": "text", "text": f"REFERENCE photos of a genuine {modele} (style code {code}):"}]
    contenu += [_img(r, 512) for r in refs[:2]]
    contenu.append({"type": "text", "text": f"PHOTOS from a second-hand listing titled \"{titre}\":"})
    # Photos de l'annonce en 512 px (5 par défaut depuis le 31/10, la plus ressemblante en 1re ; avant : 1 seule)
    contenu += [_img(p, 512) for p in photos[:int(os.environ.get("IA_PHOTOS") or 5)]]
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
        "{\"verdict\": \"oui\" | \"non\" | \"incertain\", \"confiance\": 0-100, \"code_lu\": \"style code read on the tag, or empty\", "
        "\"raison\": \"at most 15 words, in French\"}")})
    try:
        r = requests.post(API, timeout=60, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": os.environ.get("IA_MODELE") or "claude-haiku-5-5", "max_tokens": int(os.environ.get("IA_MAX_TOKENS") or 1500),
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
                "raison": str(d.get("raison") or "").strip()[:200], "conso": conso}
    except Exception as e:
        return {"erreur": str(e)[:200]}
