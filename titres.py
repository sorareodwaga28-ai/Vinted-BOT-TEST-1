"""Outils sur les titres d'annonces (partagés par le calibrage et la surveillance)."""
import re

import bot


def norm_titre(t):
    return " " + re.sub(r"[^a-z0-9 ]+", " ", bot.norm(t or "")) + " "


MARQUES = ("adidas", "reebok", "new balance", "puma", "asics", "vans", "converse", "salomon", "lacoste", "fila",
           "skechers", "timberland", "dr martens", "balenciaga", "gucci", "louis vuitton", "dior", "prada", "veja",
           "le coq", "kappa", "hoka", "on running", "mizuno", "saucony", "diadora", "ellesse", "umbro", "dc shoes",
           "dunk", "air max", "airmax", "air force", "af1", "blazer", "cortez", "yeezy", "campus", "samba", "gazelle",
           "superstar", "stan smith", "forum", "spezial", "550", "9060", "2002r", "jordan 1", "jordan 3", "jordan 5",
           "jordan 6", "jordan 11", "jordan 12", "jordan 13", "aj1", "aj3", "aj11", "max aura", "tn", "shox",
           "legacy", "312", "spizike", "stadium 90", "nu retro", "one take", "zion", "luka", "tatum", "jumpman two",
           "jordan 2", "jordan 7", "jordan 9", "jordan 10", "jordan 14", "jordan 23", "aj2", "aj7", "aj9", "aj10",
           "j1", "j3", "j5", "j6", "j11", "j12", "j13", "air 9", "delta", "mvp", "flight club", "true flight")


AUTRES_AIR_MAX = ("air max 90", "air max 95", "air max 97", "air max plus", "air max 270", "air max 180", "air max 2090",
                  "air max 87", "golf", "airmax 90", "airmax 95", "airmax 97", "jordan", "j4", "aj4")


AUTRES_SACAI = ("ldwaffle", "ld waffle", "ldv", "magmascape", "pegasus", "jordan", "regasus", "kaws")


AUTRES_NOCTA = ("hot step", "hotstep", "terra", "running", "opal", "zoom", "drive", "jordan", "black", "noir", "noire",
                "slime", "green", "vert", "verte")


AUTRES_JORDAN_1 = ("jordan 3", "jordan 4", "jordan 5", "jordan 6", "jordan 11", "jordan 12", "jordan 13", "j4", "aj4",
                   "aj3", "aj11", "low", "mid")


def autre_marque(titre, modele=""):
    """Le titre nomme une autre marque ou un autre modèle de basket que celui de la paire suivie
    (par défaut : une Jordan 4)."""
    t = norm_titre(titre)
    if " nocta " in norm_titre(modele):      # Nocta Glide : refuse les autres modèles Nocta et la Glide noire/verte
        return (any(f" {m} " in t for m in MARQUES)
                or any(f" {m} " in t for m in AUTRES_NOCTA))
    if " jordan 1 " in norm_titre(modele):   # Jordan 1 High : refuse les autres Jordan, les Low et les Mid
        return (any(f" {m} " in t for m in MARQUES if m not in ("jordan 1", "aj1", "j1"))
                or any(f" {m} " in t for m in AUTRES_JORDAN_1))
    if " sacai " in norm_titre(modele):      # Sacai : refuse les autres modèles Sacai (LDWaffle, Blazer, Cortez…)
        return (any(f" {m} " in t for m in MARQUES)
                or any(f" {m} " in t for m in AUTRES_SACAI))
    if " air max " in norm_titre(modele):
        return (any(f" {m} " in t for m in MARQUES if m not in ("air max", "airmax"))
                or any(f" {m} " in t for m in AUTRES_AIR_MAX))
    return any(f" {m} " in t for m in MARQUES)
