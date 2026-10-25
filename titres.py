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
           "jordan 6", "jordan 11", "jordan 12", "jordan 13", "aj1", "aj3", "aj11", "max aura", "tn", "shox")


def autre_marque(titre):
    """Le titre nomme une autre marque ou un autre modèle de basket (donc pas une Jordan 4)."""
    t = norm_titre(titre)
    return any(f" {m} " in t for m in MARQUES)
