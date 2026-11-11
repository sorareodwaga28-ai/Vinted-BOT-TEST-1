"""Test de reconnaissance photo (CLIP) — n'envoie PAS d'alertes, sert à apprendre.

Lit les annonces Vinted « jordan » (sans filtre sur le titre), analyse la 1re photo
de chaque annonce et envoie sur Discord un classement avec les scores.

Deux façons de juger une photo :
1. Photos de référence : dossier references/<modele>/ (jpg, png, webp).
   Score = ressemblance avec la photo de référence la plus proche (0 à 1).
2. Description texte : CLIP choisit, parmi les coloris décrits plus bas,
   celui qui colle le mieux à la photo (pourcentage).
"""
import csv
import io
import json
import os
import re
from datetime import datetime
import sys
import time
from pathlib import Path

import open_clip
import requests
import torch
from PIL import Image

import bot

# Baskets Hommes (1242) ET Femmes (2632), toutes tailles : beaucoup de Jordan 4 (Frozen Moments...)
# sont rangées en Femmes ou en petites tailles (36-39).
BASE_URL = ("https://www.vinted.fr/catalog?catalog[]=1242&catalog[]=2632&currency=EUR"
            + "&status_ids[]=6&status_ids[]=1&status_ids[]=2&status_ids[]=3&status_ids[]=4&order=newest_first")

# Description de chaque coloris pour CLIP (en anglais : CLIP a appris en anglais).
COLORIS = {
    "Vivid Sulfur": "Air Jordan 4 Vivid Sulfur sneakers, white and grey with bright yellow details",
    "Fire Red": "Air Jordan 4 Fire Red sneakers, white leather with red details",
    "Infrared": "Air Jordan 4 Infrared sneakers, dark grey nubuck, black wings and netting, bright infrared red eyelets and collar lining, cement grey midsole panel, white and black midsole",
    "Black Canvas": "Air Jordan 4 Black Canvas sneakers, black canvas with red details",
    "Midnight Navy": "Air Jordan 4 Midnight Navy sneakers, white with navy blue details",
    "Seafoam": "Air Jordan 4 Seafoam sneakers, white leather, black wings and midsole stripe, sage oil green eyelets, collar, heel and midsole, white netting, light grey outsole",
    "Canyon Purple": "Air Jordan 4 Canyon Purple sneakers, all purple hairy suede, black wings and heel, sage green eyelets and speckled green midsole, safety orange Jumpman on the tongue and orange lining",
    "Craft Olive": "Air Jordan 4 SE Craft Medium Olive sneakers, olive green hairy suede, black wings, heel tab and midsole details, pale vanilla cream midsole, olive outsole",
    "Fear": "Air Jordan 4 Fear sneakers, black and dark grey nubuck with white speckled midsole and white wings",
    "Frozen Moments": "Air Jordan 4 Frozen Moments sneakers, light grey suede and patent leather with chrome silver wings and sail midsole",
    "Air Max 1 Limeade": "Nike Air Max 1 Limeade sneakers, pine green with white Air logo print, cream laces and white midsole",
    "Sacai Vaporwaffle Villain Red": "Nike x Sacai Vaporwaffle sneakers, grey mesh with burgundy red suede, green suede and green laces, black swoosh, double stacked white midsole",
    "Sacai Vaporwaffle Sesame Blue Void": "Nike x Sacai Vaporwaffle sneakers, tan sesame suede and nylon with navy blue swoosh, white and navy laces, double stacked white midsole with navy layer",
    "SB Pine Green": "Nike SB x Air Jordan 4 Pine Green sneakers, white and sail leather with pine green details, grey suede toe and gum rubber sole",
    "Lightning": "Air Jordan 4 Lightning sneakers, bright tour yellow nubuck with dark grey wings and heel, black mesh, white and dark grey midsole",
    "Craft Photon Dust": "Air Jordan 4 SE Craft Photon Dust sneakers, light grey hairy suede and leather with dark grey midsole details, pale yellow eyelets and sail midsole",
    "Jordan 1 High Hyper Royal": "Air Jordan 1 High OG Hyper Royal sneakers, washed light blue suede overlays, white leather, light smoke grey swoosh and collar, sail midsole",
    "Jordan 3 White Cement Reimagined": "Air Jordan 3 White Cement Reimagined sneakers, white tumbled leather, grey and black elephant print, black midsole stripe, aged sail midsole, fire red eyelets and Jumpman",
    "A Ma Maniere Phantom": "Air Jordan 4 A Ma Maniere sneakers, fossil stone light beige nubuck and hairy suede, cream sail midsole, black and burgundy midsole accents, purple quilted satin lining",
    "Taupe Haze": "Air Jordan 4 Taupe Haze sneakers, taupe brown-grey suede with shiny cracked overlay, black wings, heel and outsole, infrared orange lining and Jumpman, white midsole",
    "White Cement": "Air Jordan 4 White Cement sneakers, white leather, grey speckled cement wings, heel and midsole, black midsole and lace eyelets, fire red Jumpman on the tongue",
    "Red Thunder": "Air Jordan 4 Red Thunder sneakers, black nubuck, crimson red eyelets, wings, netting and midsole, black outsole",
    "Sacai LDWaffle Undercover Yellow": "Nike x Sacai x Undercover LDWaffle sneakers, black nylon, grey suede, sail leather, bright citron yellow swoosh, laces, collar and outsole, double stacked sail midsole",
    "Jordan 3 J Balvin Rio": "Air Jordan 3 J Balvin Rio sneakers, black tumbled leather, black elephant print, gold Jumpman, sunset gradient orange-yellow-red heel and midsole, translucent pink-orange outsole",
    "Oxidized Green": "Air Jordan 4 Oxidized Green sneakers, white tumbled leather, dark teal oxidized green eyelets, heel tab, midsole and Jumpman, white netting, sail midsole",
    "Jordan 3 Fire Red": "Air Jordan 3 Fire Red sneakers, white tumbled leather, grey and black elephant print, fire red midsole, eyelets and collar lining, cement grey sole, Nike Air on the heel",
    "Neon": "Air Jordan 4 SE Neon 95 sneakers, gradient grey suede from light to dark charcoal (Air Max 95 style), volt neon yellow eyelets, wings and Air bubble, reflective grey laces",
    "Jordan 3 Racer Blue": "Air Jordan 3 Racer Blue sneakers, white tumbled leather, black and grey elephant print, bright racer blue midsole, collar lining and eyelets, cement grey sole",
    "Jordan 3 Wizards": "Air Jordan 3 Wizards sneakers, white tumbled leather, grey elephant print, true blue midsole and collar, metallic copper eyelets and Jumpman",
    "Jordan 3 Dark Iris": "Air Jordan 3 Dark Iris sneakers, white tumbled leather, grey and black elephant print, dark iris purple midsole, collar lining and eyelets, black collar, cement grey sole",
    "Red Cement": "Air Jordan 4 Red Cement sneakers, white tumbled leather, fire red wings, heel and midsole with black speckles, black lower midsole and outsole, black collar lining",
    "Jordan 3 Nina Chanel Abney": "Air Jordan 3 Nina Chanel Abney Bicoastal sneakers, teal green canvas upper, malachite green hairy suede mudguard and heel, cream laces and sail midsole, Nike Air on the heel, orange tag and lining",
    "Sacai Vaporwaffle Tour Yellow": "Nike x Sacai Vaporwaffle sneakers, tour yellow mesh, gorge green suede overlays and double swoosh, light grey swoosh, sail double stacked midsole, black outsole",
    "Jordan 1 High Lost and Found": "Air Jordan 1 High OG Lost and Found sneakers, varsity red and sail cracked leather with black swoosh, grey collar and aged cream midsole",
    "Air Max 1 Patta Waves Noise Aqua": "Nike Air Max 1 Patta Waves sneakers, wavy noise aqua teal suede mudguard, metallic silver grey mesh, sail white panels, teal swoosh, small white swoosh near toe, cream midsole, brown and aqua outsole",
    "Air Max 1 Patta Waves Monarch": "Nike Air Max 1 Patta Waves sneakers, wavy monarch orange suede mudguard, metallic silver grey mesh, white panels, orange swoosh, small white swoosh near toe, orange eyelets, cream midsole, brown and orange outsole",
    "Air Max 1 Patta Waves Rush Maroon": "Nike Air Max 1 Patta Waves sneakers, burgundy rush maroon leather mudguard and panels, metallic silver grey mesh, chrome outlined maroon swoosh, orange collar lining and eyelets, orange Nike Air heel logo, cream midsole, dark outsole",
    "Air Max 1 Patta Waves Black": "Nike Air Max 1 Patta Waves sneakers, black suede and leather wavy mudguard and panels, metallic silver grey mesh, white swoosh, small white swoosh near toe, white eyelets, sail cream midsole, black outsole",
    "Air Max 1 Patta Waves White": "Nike Air Max 1 Patta Waves sneakers, white leather wavy mudguard and panels, metallic silver grey mesh, light grey swoosh, small grey swoosh near toe, grey eyelets, sail cream midsole, white outsole",
    "Air Max 1 Lemonade": "Nike Air Max 1 Lemonade sneakers, pale lemon yellow leather and mesh, yellow and white Air logo print, dark pine green laces and eyelets, white midsole, black outsole",
    "Air Max 1 Curry": "Nike Air Max 1 Curry sneakers, dark curry orange suede mudguard and heel, tan brown leather panel, sail white mesh and swoosh, royal blue eyelets, white midsole, black outsole",
    "Air Max 1 Watermelon": "Nike Air Max 1 Watermelon sneakers, summit white and light grey with pink swoosh and kinetic green suede mudguard",
    "Air Max 1 Safari Cobblestone": "Nike Air Max 1 Safari Cobblestone sneakers, grey safari spotted print with beige and olive panels, light blue swoosh, yellow laces and sail midsole",
    "Blank Canvas": "Air Jordan 4 Blank Canvas sneakers, all sail cream canvas with frayed edges, small blue and red accents on the outsole",
    "Wet Cement": "Air Jordan 4 Wet Cement sneakers, all smoke grey nubuck and suede with dark iron grey midsole and sole, tonal grey",
    "Air Max 1 Anniversary Aqua": "Nike Air Max 1 Anniversary Aqua sneakers, white mesh and light grey suede with turquoise aqua mudguard and swoosh, purple accents",
    "Thunder": "Air Jordan 4 Thunder sneakers, black nubuck with tour yellow, red or white eyelets, mesh and midsole accents",
    "Nocta Glide White": "Nike x Nocta Glide sneakers, all white mesh and synthetic with chrome silver oval pods in the sole",
    "Bred Reimagined": "Air Jordan 4 Bred Reimagined sneakers, black tumbled leather upper (not nubuck), cement grey wings, eyelets and midsole, fire red heel lining and outsole, Nike Air logo on heel",
    "Shimmer": "Air Jordan 4 Shimmer sneakers, pale pink-beige suede upper, cream wings and lace, darker orange-brown leather heel and midsole panel, white midsole, translucent orange-pink outsole",
    "Rare Air": "Air Jordan 4 Rare Air sneakers, black nubuck with dark grey suede toe, red eyelets, deep royal blue collar and heel tab with Nike Air logo, grey and red sole",
    "autre Jordan": "Jordan sneakers of another model or colorway",
    "pas une paire": "a photo that does not show sneakers",
}

# Prix max de chaque paire (ceux du bot).
PRIX = {"Vivid Sulfur": 35, "Fire Red": 40, "Infrared": 40, "Black Canvas": 40,
        "Midnight Navy": 50, "Seafoam": 40, "Canyon Purple": 30, "Craft Olive": 40, "Fear": 40, "Frozen Moments": 60,
        "Air Max 1 Limeade": 50, "Sacai Vaporwaffle Villain Red": 60, "Sacai Vaporwaffle Sesame Blue Void": 50, "SB Pine Green": 60, "Lightning": 60, "Craft Photon Dust": 40, "Jordan 1 High Lost and Found": 60, "Sacai Vaporwaffle Tour Yellow": 50, "Jordan 3 Nina Chanel Abney": 50, "Red Cement": 40, "Jordan 3 Dark Iris": 30, "Jordan 3 Wizards": 30, "Jordan 3 Racer Blue": 30, "Neon": 50, "Jordan 3 Fire Red": 30, "Oxidized Green": 30, "Jordan 3 J Balvin Rio": 60, "Sacai LDWaffle Undercover Yellow": 40, "Red Thunder": 40, "White Cement": 40, "Taupe Haze": 60, "A Ma Maniere Phantom": 50, "Jordan 3 White Cement Reimagined": 30, "Jordan 1 High Hyper Royal": 40, "Air Max 1 Watermelon": 50, "Air Max 1 Safari Cobblestone": 50, "Blank Canvas": 50, "Wet Cement": 60, "Air Max 1 Anniversary Aqua": 70, "Thunder": 60, "Nocta Glide White": 30, "Rare Air": 40, "Shimmer": 40, "Bred Reimagined": 40, "Air Max 1 Curry": 50, "Air Max 1 Patta Waves Noise Aqua": 50, "Air Max 1 Patta Waves Monarch": 50, "Air Max 1 Patta Waves Rush Maroon": 40, "Air Max 1 Patta Waves Black": 50, "Air Max 1 Patta Waves White": 50, "Air Max 1 Lemonade": 50}

# Code produit imprimé sur l'étiquette intérieure (doit correspondre exactement).
CODES = {"Vivid Sulfur": ["AQ9129-101"], "Fire Red": ["DC7770-160"], "Midnight Navy": ["DH6927-140"], "Black Canvas": ["DH7138-006"], "Fear": ["FQ8138-002"], "Frozen Moments": ["AQ9129-001"], "Air Max 1 Limeade": ["CJ0609-300"], "Sacai Vaporwaffle Villain Red": ["DD3035-200"], "Sacai Vaporwaffle Sesame Blue Void": ["DD1875-200"], "SB Pine Green": ["DR5415-103"], "Lightning": ["CT8527-700"], "Craft Photon Dust": ["DV3770-001", "DV2262-021"], "Jordan 1 High Lost and Found": ["DZ5485-612"], "Sacai Vaporwaffle Tour Yellow": ["CV1363-700"], "Jordan 3 Nina Chanel Abney": ["FZ7974-300"], "Red Cement": ["DH6927-161"], "Jordan 3 Dark Iris": ["CT8532-105"], "Jordan 3 Wizards": ["CT8532-148"], "Jordan 3 Racer Blue": ["CT8532-145"], "Neon": ["CT5342-007"], "Jordan 3 Fire Red": ["DN3707-160"], "Oxidized Green": ["FQ8138-103"], "Jordan 3 J Balvin Rio": ["FN0344-001"], "Sacai LDWaffle Undercover Yellow": ["DJ4877-001"], "Red Thunder": ["CT8527-016"], "White Cement": ["FV5029-100"], "Taupe Haze": ["DB0732-200"], "A Ma Maniere Phantom": ["FZ4810-200"], "Jordan 3 White Cement Reimagined": ["DN3707-100"], "Jordan 1 High Hyper Royal": ["555088-402"], "Air Max 1 Watermelon": ["AH8145-106"], "Air Max 1 Safari Cobblestone": ["DV3027-001"], "Blank Canvas": ["DQ4909-100"], "Wet Cement": ["FQ7928-001"], "Air Max 1 Anniversary Aqua": ["908375-105"], "Thunder": ["DH6927-017", "FQ8138-001"], "Nocta Glide White": ["DM0879-100"], "Rare Air": ["FV5029-003"], "Shimmer": ["DJ0675-200"], "Bred Reimagined": ["FV5029-006"], "Air Max 1 Curry": ["908366-700"], "Air Max 1 Patta Waves Noise Aqua": ["DH1348-004"], "Air Max 1 Patta Waves Monarch": ["DH1348-001"], "Air Max 1 Patta Waves Rush Maroon": ["DO9549-001"], "Air Max 1 Patta Waves Black": ["DQ0299-001"], "Air Max 1 Patta Waves White": ["DQ0299-100"], "Craft Olive": ["FB9927-200"], "Canyon Purple": ["AQ9129-500"], "Seafoam": ["AQ9129-103"], "Infrared": ["DH6927-061"], "Air Max 1 Lemonade": ["CJ0609-700"]}

# Nom complet donné à l'IA de vision, et recherche Vinted de la famille (Jordan 4 par défaut).
NOMS = {"Air Max 1 Limeade": "Nike Air Max 1 Premium \"Limeade\" (Powerwall, Pine Green)",
        "Sacai Vaporwaffle Villain Red": "Nike x Sacai Vaporwaffle \"Villain Red / Neptune Green\" (String/Black-Villain Red)",
        "Sacai Vaporwaffle Sesame Blue Void": "Nike x Sacai Vaporwaffle \"Sesame / Blue Void\" (Sesame/Blue Void-White)",
        "SB Pine Green": "Nike SB x Air Jordan 4 Retro SP \"Pine Green\" (Sail/White-Pine Green)",
        "Lightning": "Air Jordan 4 Retro \"Lightning\" 2021 (Tour Yellow/Dark Blue Grey)",
        "Craft Photon Dust": "Air Jordan 4 SE Craft \"Photon Dust\" (Photon Dust/Pale Vanilla, grey, NOT the olive Craft)",
        "Jordan 1 High Hyper Royal": "Air Jordan 1 Retro High OG \"Hyper Royal\" (Hyper Royal/Light Smoke Grey-White, washed blue suede — NOT the University Blue, NOT a Low or Mid)",
        "Jordan 3 White Cement Reimagined": "Air Jordan 3 Retro \"White Cement Reimagined\" 2023 (Summit White/Fire Red-Black-Cement Grey, sail midsole, Nike Air on the heel — NOT the Jordan 4 White Cement / Wet Cement, NOT the Black Cement)",
        "A Ma Maniere Phantom": "A Ma Maniere x Air Jordan 4 Retro OG SP \"While You Were Sleeping\" 2024, aka Phantom (Fossil Stone/Burgundy Crush, WMNS) — NOT the A Ma Maniere Violet Ore, NOT other A Ma Maniere Jordans (1, 3, 12)",
        "Taupe Haze": "Air Jordan 4 Retro \"Taupe Haze\" 2021 (Taupe Haze/Infrared 23-Oil Grey) — NOT the Infrared, NOT the Craft Olive or Photon Dust",
        "White Cement": "Air Jordan 4 Retro \"White Cement\" 2025 (Summit White/Fire Red-Black-Tech Grey) — NOT the Wet Cement (all grey), NOT the Jordan 3 White Cement, NOT the Military Black or Black Cat",
        "Red Thunder": "Air Jordan 4 Retro \"Red Thunder\" 2022 (Black/Multi-Color, black nubuck with crimson red) — NOT the yellow Thunder, NOT the White Thunder, NOT the Bred, Fire Red or Infrared",
        "Sacai LDWaffle Undercover Yellow": "Nike x Sacai x Undercover LDWaffle (Black/Bright Citron-Sail) — NOT the Undercover Night Maroon/Team Royal, NOT other LDWaffle (Fragment, Pine Green, Black Nylon…), NOT the Vaporwaffle",
        "Jordan 3 J Balvin Rio": "J Balvin x Air Jordan 3 Retro SP \"Rio\" 2024 (Black Rainbow / Black-Multi, all black with sunset gradient heel) — NOT the J Balvin Medellin Sunset (colorful), NOT the Black Cat or Black Cement",
        "Oxidized Green": "Air Jordan 4 Retro \"Oxidized Green\" 2024 (White/Oxidized Green-Neutral Grey) — NOT the SB Pine Green (sail suede), NOT the Seafoam, NOT the Military Blue",
        "Jordan 3 Fire Red": "Air Jordan 3 Retro \"Fire Red\" 2022 (White/Fire Red-Cement Grey-Black, Nike Air heel) — NOT the Jordan 4 Fire Red, NOT the Jordan 3 White Cement (Reimagined), NOT the Cardinal Red",
        "Neon": "Air Jordan 4 Retro SE \"Neon\" / \"95 Neon\" 2020 (Cool Grey/Volt-Wolf Grey, grey gradient suede) — NOT the Lightning (all yellow), NOT the Cool Grey, NOT the Wet Cement",
        "Jordan 3 Racer Blue": "Air Jordan 3 Retro \"Racer Blue\" 2021 (White/Black-Cement Grey-Racer Blue, blue eyelets) — NOT the Wizards (copper eyelets, darker true blue), NOT the True Blue",
        "Jordan 3 Wizards": "Air Jordan 3 Retro \"Wizards\" 2023 (White/Metallic Copper-True Blue, copper eyelets) — NOT the Racer Blue (bright blue eyelets), NOT the True Blue",
        "Jordan 3 Dark Iris": "Air Jordan 3 Retro \"Dark Iris\" 2022 (White/Black-Dark Iris-Cement Grey, purple) — NOT the Racer Blue, NOT the Wizards, NOT the Court Purple",
        "Red Cement": "Air Jordan 4 Retro \"Red Cement\" 2023 (White/Fire Red-Black-Neutral Grey, red speckled wings) — NOT the Fire Red 2020 (white with red wings, NO speckles, Nike Air heel), NOT the White Cement, NOT the Red Thunder",
        "Jordan 3 Nina Chanel Abney": "Nina Chanel Abney x Air Jordan 3 Retro OG SP \"Bicoastal\" (WMNS, Bicoastal/Black-Malachite, teal canvas with green hairy suede) — NOT the Jordan 3 Pine Green, NOT the Jordan 3 Green Glow",
        "Sacai Vaporwaffle Tour Yellow": "Nike x Sacai Vaporwaffle \"Tour Yellow / Stadium Green\" (Tour Yellow/Gorge Green-Sail) — NOT the LDWaffle Varsity Blue/Yellow, NOT the Undercover LDWaffle, NOT the Villain Red or Sesame",
        "Jordan 1 High Lost and Found": "Air Jordan 1 Retro High OG \"Lost and Found\" / Chicago Reimagined (Varsity Red/Black-Sail-Muslin)",
        "Air Max 1 Patta Waves Noise Aqua": "Nike Air Max 1 / Patta \"Waves\" Noise Aqua 2021 (Metallic Silver/Noise Aqua, wavy mudguard — NOT the Anniversary Aqua, NOT other Patta Waves colors like Monarch, Rush Maroon, Black, White)",
        "Air Max 1 Patta Waves Monarch": "Nike Air Max 1 / Patta \"Waves\" Monarch 2021 (Metallic Silver/Monarch, orange wavy mudguard — NOT the Air Max 1 Curry, NOT other Patta Waves colors like Noise Aqua, Rush Maroon, Dark Russet, Black, White)",
        "Air Max 1 Patta Waves Rush Maroon": "Nike Air Max 1 / Patta \"Waves\" Rush Maroon 2021 (Rush Maroon/Metallic Silver, burgundy leather, orange lining — NOT other Patta Waves colors like Monarch, Noise Aqua, Dark Russet, Black, White)",
        "Air Max 1 Patta Waves Black": "Nike Air Max 1 / Patta \"Waves\" Black 2021 (Black/Metallic Silver-White, black with grey mesh and white swoosh — NOT other Patta Waves colors like White, Monarch, Noise Aqua, Rush Maroon, Dark Russet)",
        "Air Max 1 Patta Waves White": "Nike Air Max 1 / Patta \"Waves\" White 2021 (White/Pure Platinum, white leather with grey mesh and grey swoosh — NOT other Patta Waves colors like Black, Monarch, Noise Aqua, Rush Maroon, Dark Russet)",
        "Craft Olive": "Air Jordan 4 Retro SE Craft \"Medium Olive\" 2023 (Medium Olive/Pale Vanilla, olive green hairy suede, black wings and heel, NOT the grey Photon Dust Craft)",
        "Canyon Purple": "Air Jordan 4 Retro WMNS \"Canyon Purple\" 2022 (Canyon Purple/Safety Orange-Alligator, purple suede, sage green accents)",
        "Seafoam": "Air Jordan 4 Retro WMNS \"Seafoam\" / Oil Green 2021 (White/Oil Green-Dark Ash, NOT the Craft Olive, NOT Military Black)",
        "Infrared": "Air Jordan 4 Retro \"Infrared\" 2022 (Dark Grey/Infrared 23-Black, NOT the Bred, NOT the Fire Red)",
        "Air Max 1 Lemonade": "Nike Air Max 1 Premium \"Lemonade\" 2020 (Lemon Wash/Lemon Wash-Pine Green, pale yellow with Air logo print and green laces — NOT the green Limeade, NOT the pink Strawberry Lemonade)",
        "Air Max 1 Curry": "Nike Air Max 1 Premium Retro \"Curry\" 2018 (Dark Curry/True White, NOT the atmos or Patta versions)",
        "Air Max 1 Watermelon": "Nike Air Max 1 \"Watermelon\" (Summit White/Sunset Pulse-Kinetic Green)",
        "Air Max 1 Safari Cobblestone": "Nike Air Max 1 \"Safari Cobblestone\" (women's, Cobblestone/Worn Blue)",
        "Blank Canvas": "Air Jordan 4 Retro \"Blank Canvas\" (women's, Sail/Sail-Cement Grey-Fire Red, all cream canvas — NOT the Black Canvas)",
        "Wet Cement": "Air Jordan 4 Retro SE \"Wet Cement\" (Smoke Grey/Iron Grey, all grey — NOT White, Black or Fire Red Cement)",
        "Air Max 1 Anniversary Aqua": "Nike Air Max 1 Anniversary \"Aqua\" 2017 (White/Aqua-Neutral Grey-Hyper Grape, turquoise — NOT the red or royal blue Anniversary)",
        "Thunder": "Air Jordan 4 Retro \"Thunder\" 2023 (black/tour yellow) or \"White Thunder\" (black/white, sail/orange accents). NOT the Red Thunder (black/crimson red, tracked separately), NOT the all-yellow Lightning, NOT the Bred",
        "Nocta Glide White": "Nike x Nocta Glide \"White Chrome\" (White/White-Chrome, all white — NOT the Black Chrome or Slime Green Glide)",
        "Rare Air": "Air Jordan 4 Retro \"Rare Air\" 2024 (Black/Fire Red-Deep Royal Blue)",
        "Shimmer": "Air Jordan 4 Retro WMNS \"Shimmer\" 2021 (Shimmer/Metallic Silver, pink-beige suede, NOT a custom or the beige Craft)",
        "Bred Reimagined": "Air Jordan 4 Retro \"Bred Reimagined\" 2024 (Black/Fire Red-Cement Grey, black LEATHER — NOT the 2019 Bred in nubuck, NOT Rare Air FV5029-003)"}
RECHERCHES = {"Sacai Vaporwaffle Tour Yellow": "&search_text=sacai", "Sacai LDWaffle Undercover Yellow": "&search_text=sacai", "Nocta Glide White": "&search_text=nocta", "Air Max 1 Limeade": "&search_text=air+max+1", "Air Max 1 Lemonade": "&search_text=air+max+1", "Air Max 1 Anniversary Aqua": "&search_text=air+max+1", "Air Max 1 Watermelon": "&search_text=air+max+1", "Air Max 1 Curry": "&search_text=air+max+1", "Air Max 1 Patta Waves Noise Aqua": "&search_text=patta", "Air Max 1 Patta Waves Monarch": "&search_text=patta", "Air Max 1 Patta Waves Rush Maroon": "&search_text=patta", "Air Max 1 Patta Waves Black": "&search_text=patta", "Air Max 1 Patta Waves White": "&search_text=patta", "Air Max 1 Safari Cobblestone": "&search_text=air+max+1", "Sacai Vaporwaffle Villain Red": "&search_text=sacai", "Sacai Vaporwaffle Sesame Blue Void": "&search_text=sacai"}


# Mots qui, dans le titre, empêchent de dire « le titre nomme la paire » (l'IA juge quand même sur photo)
EXCLUS = {"Air Max 1 Lemonade": ["strawberry", "fraise", "pink", "rose"]}

# 11/11 (Bastien) : nom affiché dans les alertes, le site et l'inventaire (la clé interne reste la même)
AFFICHE = {"Thunder": "Yellow Thunder"}


def aff(x):
    return AFFICHE.get(x, x)


def nom(x):
    return NOMS.get(x, f"Air Jordan 4 \"{x}\"")


NIKE = "&brand_ids[]=53"     # Jordan rangées sous la marque « Nike » par les particuliers (vérifié au démarrage)


def recherche(x):
    return RECHERCHES.get(x, "&brand_ids[]=2703" + NIKE)

# Lettres et chiffres que l'OCR confond souvent.
CONFUS = {"0": "0OQD", "1": "1IL", "2": "2Z", "5": "5S", "8": "8B", "6": "6G",
          "O": "O0", "I": "I1L", "S": "S5", "B": "B8", "Z": "Z2", "Q": "Q0O"}


def motif_code(code):
    parts = []
    for c in code.upper():
        if c == "-":
            parts.append(r"[-\s._]?")
        else:
            parts.append("[" + CONFUS.get(c, c) + "]")
    return re.compile("".join(parts))


_ocr = None


def lire_texte(img):
    """Texte lu sur l'image (majuscules, sans espaces superflus)."""
    global _ocr
    if _ocr is None:
        import easyocr
        _ocr = easyocr.Reader(["en"], gpu=False, verbose=False)
    import numpy as np
    from PIL import ImageEnhance, ImageFilter, ImageOps
    w, h = img.size
    if max(w, h) < 1200:   # agrandir les petites photos aide la lecture
        k = 1200 / max(w, h)
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    # 2 lectures : photo telle quelle, puis en gris + contraste + netteté
    net = ImageEnhance.Contrast(ImageOps.autocontrast(ImageOps.grayscale(img))).enhance(1.5)
    net = net.filter(ImageFilter.UnsharpMask(4, 200, 2))
    textes = []
    for version in (img, net):
        lignes = _ocr.readtext(np.array(version), detail=0, paragraph=False,
                               allowlist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
        textes.append(" ".join(lignes).upper())
    return " | ".join(textes)


def verifier_code(texte, codes):
    """('ok', code) si un code attendu est lu, ('autre', code lu) si un autre code produit, sinon (None, '')."""
    compact = texte.replace(" ", "")
    for c in codes:
        if motif_code(c).search(texte) or motif_code(c).search(compact):
            return "ok", c
    m = re.search(r"[A-Z0-9]{2}\d{4}[-\s]?\d{3}", texte)
    return ("autre", m.group(0)) if m else (None, "")


# Mots obligatoires actuels du bot, pour comparer titre et photo.
MOTS = {
    "Vivid Sulfur": "jordan + sulfur, jordan + sulphur, jordan 4 + sulfur, jordan 4 + sulphur, jordan4 + sulfur, jordan4 + sulphur, j4 + sulfur, j4 + sulphur, aj4 + sulfur, aj4 + sulphur, jordan iv + sulfur, jordan iv + sulphur",
    "Fire Red": "jordan + fire + red, jordan + firered, jordan 4 + fire + red, jordan 4 + firered, jordan4 + fire + red, jordan4 + firered, j4 + fire + red, j4 + firered, aj4 + fire + red, aj4 + firered, jordan iv + fire + red, jordan iv + firered",
    "Infrared": "jordan + infrared, jordan 4 + infrared, jordan4 + infrared, j4 + infrared, aj4 + infrared, jordan iv + infrared, jordan + infra + red, jordan 4 + infra + red, jordan4 + infra + red, j4 + infra + red, aj4 + infra + red, jordan iv + infra + red, jordan + infrarouge, jordan 4 + infrarouge, jordan4 + infrarouge, j4 + infrarouge, aj4 + infrarouge, jordan iv + infrarouge, jordan + infra + rouge, jordan 4 + infra + rouge, jordan4 + infra + rouge, j4 + infra + rouge, aj4 + infra + rouge, jordan iv + infra + rouge, jordan + infrared + 23, jordan 4 + infrared + 23, jordan4 + infrared + 23, j4 + infrared + 23, aj4 + infrared + 23, jordan iv + infrared + 23",
    "Black Canvas": "jordan + black + canvas, jordan 4 + black + canvas, jordan4 + black + canvas, j4 + black + canvas, aj4 + black + canvas, jordan iv + black + canvas",
    "Midnight Navy": "jordan + midnight + navy, jordan + midnightnavy, jordan 4 + midnight + navy, jordan4 + midnight + navy, j4 + midnight + navy, aj4 + midnight + navy, jordan iv + midnight + navy",
    "Seafoam": "jordan + seafoam, jordan 4 + seafoam, jordan4 + seafoam, j4 + seafoam, aj4 + seafoam, jordan iv + seafoam, jordan + sea + foam, jordan 4 + sea + foam, jordan4 + sea + foam, j4 + sea + foam, aj4 + sea + foam, jordan iv + sea + foam, jordan + seafom, jordan 4 + seafom, jordan4 + seafom, j4 + seafom, aj4 + seafom, jordan iv + seafom, jordan + oil + green, jordan 4 + oil + green, jordan4 + oil + green, j4 + oil + green, aj4 + oil + green, jordan iv + oil + green",
    "Canyon Purple": "jordan + canyon, jordan 4 + canyon, jordan4 + canyon, j4 + canyon, aj4 + canyon, jordan iv + canyon, jordan + canyon + purple, jordan 4 + canyon + purple, jordan4 + canyon + purple, j4 + canyon + purple, aj4 + canyon + purple, jordan iv + canyon + purple, jordan + canyon + violet, jordan 4 + canyon + violet, jordan4 + canyon + violet, j4 + canyon + violet, aj4 + canyon + violet, jordan iv + canyon + violet, jordan + canion, jordan 4 + canion, jordan4 + canion, j4 + canion, aj4 + canion, jordan iv + canion",
    "Craft Olive": "jordan + craft + olive, jordan 4 + craft + olive, jordan4 + craft + olive, j4 + craft + olive, aj4 + craft + olive, jordan iv + craft + olive, jordan + medium + olive, jordan 4 + medium + olive, jordan4 + medium + olive, j4 + medium + olive, aj4 + medium + olive, jordan iv + medium + olive, jordan + craft + kaki, jordan 4 + craft + kaki, jordan4 + craft + kaki, j4 + craft + kaki, aj4 + craft + kaki, jordan iv + craft + kaki, jordan + craft + khaki, jordan 4 + craft + khaki, jordan4 + craft + khaki, j4 + craft + khaki, aj4 + craft + khaki, jordan iv + craft + khaki, jordan + craft + vert, jordan 4 + craft + vert, jordan4 + craft + vert, j4 + craft + vert, aj4 + craft + vert, jordan iv + craft + vert, jordan + craft + verte, jordan 4 + craft + verte, jordan4 + craft + verte, j4 + craft + verte, aj4 + craft + verte, jordan iv + craft + verte, jordan + craft + green, jordan 4 + craft + green, jordan4 + craft + green, j4 + craft + green, aj4 + craft + green, jordan iv + craft + green",
    "Fear": "jordan + fear, jordan 4 + fear, jordan4 + fear, j4 + fear, aj4 + fear, jordan iv + fear",
    "Frozen Moments": "jordan + frozen, jordan 4 + frozen, jordan4 + frozen, j4 + frozen, aj4 + frozen, jordan iv + frozen",
    "Sacai Vaporwaffle Sesame Blue Void": "sacai + sesame, sacai + blue + void, sacai + bleu + void, sakai + sesame, sakai + blue + void, sakai + bleu + void, vaporwaffle + sesame, vaporwaffle + blue + void, vaporwaffle + bleu + void, vapor waffle + sesame, vapor waffle + blue + void, vapor waffle + bleu + void, sacai + beige + bleu, sacai + beige + blue, sacai + beige + navy, sacai + marron + bleu, sacai + camel + bleu, sakai + beige + bleu, sakai + beige + blue, sakai + beige + navy, sakai + marron + bleu, sakai + camel + bleu, vaporwaffle + beige + bleu, vaporwaffle + beige + blue, vaporwaffle + beige + navy, vaporwaffle + marron + bleu, vaporwaffle + camel + bleu",
    "Sacai Vaporwaffle Villain Red": "sacai + villain, sacai + neptune, sakai + villain, sakai + neptune, vaporwaffle + villain, vaporwaffle + neptune, vapor waffle + villain, vapor waffle + neptune, sacai + rouge + vert, sacai + bordeaux + vert, sakai + rouge + vert, sakai + bordeaux + vert, vaporwaffle + rouge + vert, vaporwaffle + bordeaux + vert, sacai + red + green, sacai + burgundy + green, vaporwaffle + red + green, vaporwaffle + burgundy + green",
    "SB Pine Green": "jordan + pine, jordan 4 + pine, jordan4 + pine, j4 + pine, aj4 + pine, jordan iv + pine, jordan + pinegreen, jordan 4 + pinegreen, jordan4 + pinegreen, j4 + pinegreen, aj4 + pinegreen, jordan iv + pinegreen, jordan + sb + green, jordan + sb + vert, jordan + sb + verte, jordan 4 + sb + green, jordan 4 + sb + vert, jordan 4 + sb + verte, jordan4 + sb + green, jordan4 + sb + vert, jordan4 + sb + verte, j4 + sb + green, j4 + sb + vert, j4 + sb + verte, aj4 + sb + green, aj4 + sb + vert, aj4 + sb + verte, jordan iv + sb + green, jordan iv + sb + vert, jordan iv + sb + verte, jordan + vert + sapin, jordan 4 + vert + sapin, jordan4 + vert + sapin, j4 + vert + sapin, aj4 + vert + sapin, jordan iv + vert + sapin",
    "Lightning": "jordan + lightning, jordan 4 + lightning, jordan4 + lightning, j4 + lightning, aj4 + lightning, jordan iv + lightning, jordan + lighting, jordan 4 + lighting, jordan4 + lighting, j4 + lighting, aj4 + lighting, jordan iv + lighting, jordan + lightening, jordan 4 + lightening, jordan4 + lightening, j4 + lightening, aj4 + lightening, jordan iv + lightening, jordan + lightnin, jordan 4 + lightnin, jordan4 + lightnin, j4 + lightnin, aj4 + lightnin, jordan iv + lightnin, jordan + tour + yellow, jordan 4 + tour + yellow, jordan4 + tour + yellow, j4 + tour + yellow, aj4 + tour + yellow, jordan iv + tour + yellow",
    "Craft Photon Dust": "jordan + craft + photon, jordan 4 + craft + photon, jordan4 + craft + photon, j4 + craft + photon, aj4 + craft + photon, jordan iv + craft + photon, jordan + photon + dust, jordan 4 + photon + dust, jordan4 + photon + dust, j4 + photon + dust, aj4 + photon + dust, jordan iv + photon + dust, jordan + photon, jordan 4 + photon, jordan4 + photon, j4 + photon, aj4 + photon, jordan iv + photon, jordan + craft + grey, jordan 4 + craft + grey, jordan4 + craft + grey, j4 + craft + grey, aj4 + craft + grey, jordan iv + craft + grey, jordan + craft + gris, jordan 4 + craft + gris, jordan4 + craft + gris, j4 + craft + gris, aj4 + craft + gris, jordan iv + craft + gris, jordan + craft + grise, jordan 4 + craft + grise, jordan4 + craft + grise, j4 + craft + grise, aj4 + craft + grise, jordan iv + craft + grise, jordan + craft + vanilla, jordan 4 + craft + vanilla, jordan4 + craft + vanilla, j4 + craft + vanilla, aj4 + craft + vanilla, jordan iv + craft + vanilla",
    "Jordan 1 High Hyper Royal": "jordan 1 + hyper + royal, jordan1 + hyper + royal, j1 + hyper + royal, aj1 + hyper + royal, jordan one + hyper + royal, jordan + hyper + royal, jordan 1 + hyperroyal, jordan1 + hyperroyal, j1 + hyperroyal, aj1 + hyperroyal, jordan one + hyperroyal, jordan + hyperroyal",
    "Jordan 3 White Cement Reimagined": "jordan 3 + cement + reimagined, jordan 3 + ciment + reimagined, jordan 3 + white + reimagined, jordan 3 + blanc + reimagined, jordan 3 + white + cement + reimagine, jordan 3 + white + ciment + reimagine, jordan3 + cement + reimagined, jordan3 + ciment + reimagined, jordan3 + white + reimagined, jordan3 + blanc + reimagined, jordan3 + white + cement + reimagine, jordan3 + white + ciment + reimagine, j3 + cement + reimagined, j3 + ciment + reimagined, j3 + white + reimagined, j3 + blanc + reimagined, j3 + white + cement + reimagine, j3 + white + ciment + reimagine, aj3 + cement + reimagined, aj3 + ciment + reimagined, aj3 + white + reimagined, aj3 + blanc + reimagined, aj3 + white + cement + reimagine, aj3 + white + ciment + reimagine, jordan iii + cement + reimagined, jordan iii + ciment + reimagined, jordan iii + white + reimagined, jordan iii + blanc + reimagined, jordan iii + white + cement + reimagine, jordan iii + white + ciment + reimagine",
    "A Ma Maniere Phantom": "jordan 4 + a ma maniere, jordan 4 + amamaniere, jordan 4 + maniere, jordan 4 + amm, jordan 4 + phantom, jordan 4 + while + you + were + sleeping, jordan4 + a ma maniere, jordan4 + amamaniere, jordan4 + maniere, jordan4 + amm, jordan4 + phantom, jordan4 + while + you + were + sleeping, j4 + a ma maniere, j4 + amamaniere, j4 + maniere, j4 + amm, j4 + phantom, j4 + while + you + were + sleeping, aj4 + a ma maniere, aj4 + amamaniere, aj4 + maniere, aj4 + amm, aj4 + phantom, aj4 + while + you + were + sleeping, jordan iv + a ma maniere, jordan iv + amamaniere, jordan iv + maniere, jordan iv + amm, jordan iv + phantom, jordan iv + while + you + were + sleeping",
    "Taupe Haze": "jordan 4 + taupe, jordan 4 + haze, jordan 4 + brume + taupe, jordan4 + taupe, jordan4 + haze, jordan4 + brume + taupe, j4 + taupe, j4 + haze, j4 + brume + taupe, aj4 + taupe, aj4 + haze, aj4 + brume + taupe, jordan iv + taupe, jordan iv + haze, jordan iv + brume + taupe",
    "White Cement": "jordan 4 + white + cement, jordan 4 + white + ciment, jordan 4 + blanc + ciment, jordan 4 + blanc + cement, jordan 4 + whitecement, jordan4 + white + cement, jordan4 + white + ciment, jordan4 + blanc + ciment, jordan4 + blanc + cement, jordan4 + whitecement, j4 + white + cement, j4 + white + ciment, j4 + blanc + ciment, j4 + blanc + cement, j4 + whitecement, aj4 + white + cement, aj4 + white + ciment, aj4 + blanc + ciment, aj4 + blanc + cement, aj4 + whitecement, jordan iv + white + cement, jordan iv + white + ciment, jordan iv + blanc + ciment, jordan iv + blanc + cement, jordan iv + whitecement",
    "Red Thunder": "jordan 4 + red + thunder, jordan 4 + rouge + thunder, jordan 4 + thunder + red, jordan 4 + thunder + rouge, jordan 4 + redthunder, jordan 4 + crimson + thunder, jordan4 + red + thunder, jordan4 + rouge + thunder, jordan4 + thunder + red, jordan4 + thunder + rouge, jordan4 + redthunder, jordan4 + crimson + thunder, j4 + red + thunder, j4 + rouge + thunder, j4 + thunder + red, j4 + thunder + rouge, j4 + redthunder, j4 + crimson + thunder, aj4 + red + thunder, aj4 + rouge + thunder, aj4 + thunder + red, aj4 + thunder + rouge, aj4 + redthunder, aj4 + crimson + thunder, jordan iv + red + thunder, jordan iv + rouge + thunder, jordan iv + thunder + red, jordan iv + thunder + rouge, jordan iv + redthunder, jordan iv + crimson + thunder",
    "Sacai LDWaffle Undercover Yellow": "sacai + undercover + yellow, sacai + undercover + jaune, sacai + undercover + citron, sacai + undercover + black, sacai + undercover + noir, sacai + undercover + noire, sakai + undercover + yellow, sakai + undercover + jaune, sakai + undercover + citron, sakai + undercover + black, sakai + undercover + noir, sakai + undercover + noire, ldwaffle + undercover + yellow, ldwaffle + undercover + jaune, ldwaffle + undercover + citron, ldwaffle + undercover + black, ldwaffle + undercover + noir, ldwaffle + undercover + noire, ld waffle + undercover + yellow, ld waffle + undercover + jaune, ld waffle + undercover + citron, ld waffle + undercover + black, ld waffle + undercover + noir, ld waffle + undercover + noire",
    "Jordan 3 J Balvin Rio": "jordan 3 + balvin + rio, jordan 3 + balvin + black, jordan 3 + balvin + noir, jordan 3 + balvin + noire, jordan 3 + black + rainbow, jordan3 + balvin + rio, jordan3 + balvin + black, jordan3 + balvin + noir, jordan3 + balvin + noire, jordan3 + black + rainbow, j3 + balvin + rio, j3 + balvin + black, j3 + balvin + noir, j3 + balvin + noire, j3 + black + rainbow, aj3 + balvin + rio, aj3 + balvin + black, aj3 + balvin + noir, aj3 + balvin + noire, aj3 + black + rainbow, jordan iii + balvin + rio, jordan iii + balvin + black, jordan iii + balvin + noir, jordan iii + balvin + noire, jordan iii + black + rainbow, jordan + balvin + rio, jordan + balvin + black, jordan + balvin + noir, jordan + balvin + noire, jordan + black + rainbow",
    "Oxidized Green": "jordan 4 + oxidized, jordan 4 + oxydized, jordan 4 + oxidised, jordan 4 + oxydised, jordan 4 + oxide, jordan 4 + oxyde, jordan 4 + oxydee, jordan 4 + oxydize, jordan 4 + oxidize, jordan4 + oxidized, jordan4 + oxydized, jordan4 + oxidised, jordan4 + oxydised, jordan4 + oxide, jordan4 + oxyde, jordan4 + oxydee, jordan4 + oxydize, jordan4 + oxidize, j4 + oxidized, j4 + oxydized, j4 + oxidised, j4 + oxydised, j4 + oxide, j4 + oxyde, j4 + oxydee, j4 + oxydize, j4 + oxidize, aj4 + oxidized, aj4 + oxydized, aj4 + oxidised, aj4 + oxydised, aj4 + oxide, aj4 + oxyde, aj4 + oxydee, aj4 + oxydize, aj4 + oxidize, jordan iv + oxidized, jordan iv + oxydized, jordan iv + oxidised, jordan iv + oxydised, jordan iv + oxide, jordan iv + oxyde, jordan iv + oxydee, jordan iv + oxydize, jordan iv + oxidize",
    "Jordan 3 Fire Red": "jordan 3 + fire + red, jordan 3 + firered, jordan 3 + rouge + feu, jordan 3 + fire, jordan3 + fire + red, jordan3 + firered, jordan3 + rouge + feu, jordan3 + fire, j3 + fire + red, j3 + firered, j3 + rouge + feu, j3 + fire, aj3 + fire + red, aj3 + firered, aj3 + rouge + feu, aj3 + fire, jordan iii + fire + red, jordan iii + firered, jordan iii + rouge + feu, jordan iii + fire",
    "Neon": "jordan 4 + neon, jordan 4 + néon, jordan 4 + 95 + neon, jordan 4 + volt + grey, jordan 4 + volt + gris, jordan 4 + gris + fluo, jordan4 + neon, jordan4 + néon, jordan4 + 95 + neon, jordan4 + volt + grey, jordan4 + volt + gris, jordan4 + gris + fluo, j4 + neon, j4 + néon, j4 + 95 + neon, j4 + volt + grey, j4 + volt + gris, j4 + gris + fluo, aj4 + neon, aj4 + néon, aj4 + 95 + neon, aj4 + volt + grey, aj4 + volt + gris, aj4 + gris + fluo, jordan iv + neon, jordan iv + néon, jordan iv + 95 + neon, jordan iv + volt + grey, jordan iv + volt + gris, jordan iv + gris + fluo",
    "Jordan 3 Racer Blue": "jordan 3 + racer + blue, jordan 3 + racer + bleu, jordan 3 + racer, jordan3 + racer + blue, jordan3 + racer + bleu, jordan3 + racer, j3 + racer + blue, j3 + racer + bleu, j3 + racer, aj3 + racer + blue, aj3 + racer + bleu, aj3 + racer, jordan iii + racer + blue, jordan iii + racer + bleu, jordan iii + racer",
    "Jordan 3 Wizards": "jordan 3 + wizards, jordan 3 + wizard, jordan 3 + copper, jordan 3 + cuivre, jordan3 + wizards, jordan3 + wizard, jordan3 + copper, jordan3 + cuivre, j3 + wizards, j3 + wizard, j3 + copper, j3 + cuivre, aj3 + wizards, aj3 + wizard, aj3 + copper, aj3 + cuivre, jordan iii + wizards, jordan iii + wizard, jordan iii + copper, jordan iii + cuivre",
    "Jordan 3 Dark Iris": "jordan 3 + dark + iris, jordan 3 + iris, jordan 3 + violet, jordan 3 + purple, jordan3 + dark + iris, jordan3 + iris, jordan3 + violet, jordan3 + purple, j3 + dark + iris, j3 + iris, j3 + violet, j3 + purple, aj3 + dark + iris, aj3 + iris, aj3 + violet, aj3 + purple, jordan iii + dark + iris, jordan iii + iris, jordan iii + violet, jordan iii + purple",
    "Red Cement": "jordan 4 + red + cement, jordan 4 + rouge + ciment, jordan 4 + rouge + cement, jordan 4 + red + ciment, jordan 4 + fire + red + cement, jordan 4 + redcement, jordan4 + red + cement, jordan4 + rouge + ciment, jordan4 + rouge + cement, jordan4 + red + ciment, jordan4 + fire + red + cement, jordan4 + redcement, j4 + red + cement, j4 + rouge + ciment, j4 + rouge + cement, j4 + red + ciment, j4 + fire + red + cement, j4 + redcement, aj4 + red + cement, aj4 + rouge + ciment, aj4 + rouge + cement, aj4 + red + ciment, aj4 + fire + red + cement, aj4 + redcement, jordan iv + red + cement, jordan iv + rouge + ciment, jordan iv + rouge + cement, jordan iv + red + ciment, jordan iv + fire + red + cement, jordan iv + redcement",
    "Jordan 3 Nina Chanel Abney": "jordan 3 + nina, jordan 3 + abney, jordan 3 + bicoastal, jordan 3 + chanel + abney, jordan3 + nina, jordan3 + abney, jordan3 + bicoastal, jordan3 + chanel + abney, j3 + nina, j3 + abney, j3 + bicoastal, j3 + chanel + abney, aj3 + nina, aj3 + abney, aj3 + bicoastal, aj3 + chanel + abney, jordan iii + nina, jordan iii + abney, jordan iii + bicoastal, jordan iii + chanel + abney, jordan + nina, jordan + abney, jordan + bicoastal, jordan + chanel + abney",
    "Sacai Vaporwaffle Tour Yellow": "sacai + tour + yellow, sacai + jaune + vert, sacai + yellow + green, sacai + stadium + green, sacai + gorge + green, sakai + tour + yellow, sakai + jaune + vert, sakai + yellow + green, sakai + stadium + green, sakai + gorge + green, vaporwaffle + tour + yellow, vaporwaffle + jaune + vert, vaporwaffle + yellow + green, vaporwaffle + stadium + green, vaporwaffle + gorge + green, vapor waffle + tour + yellow, vapor waffle + jaune + vert, vapor waffle + yellow + green, vapor waffle + stadium + green, vapor waffle + gorge + green",
    "Jordan 1 High Lost and Found": "jordan + lost + found, jordan 1 + lost + found, jordan1 + lost + found, j1 + lost + found, aj1 + lost + found, jordan one + lost + found, jordan + lost, jordan 1 + lost, jordan1 + lost, j1 + lost, aj1 + lost, jordan one + lost, jordan + chicago + reimagined, jordan 1 + chicago + reimagined, jordan1 + chicago + reimagined, j1 + chicago + reimagined, aj1 + chicago + reimagined, jordan one + chicago + reimagined, jordan + chicago + lost, jordan 1 + chicago + lost, jordan1 + chicago + lost, j1 + chicago + lost, aj1 + chicago + lost, jordan one + chicago + lost",
    "Air Max 1 Patta Waves Noise Aqua": "air max 1 + patta + aqua, am1 + patta + aqua, air max one + patta + aqua, air max + patta + aqua, patta + waves + aqua, patta + wave + aqua, nike + patta + aqua, air max 1 + patta + noise, am1 + patta + noise, air max one + patta + noise, air max + patta + noise, patta + waves + noise, patta + wave + noise, nike + patta + noise, air max 1 + patta + turquoise, am1 + patta + turquoise, air max one + patta + turquoise, air max + patta + turquoise, patta + waves + turquoise, patta + wave + turquoise, nike + patta + turquoise, air max 1 + patta + teal, am1 + patta + teal, air max one + patta + teal, air max + patta + teal, patta + waves + teal, patta + wave + teal, nike + patta + teal, air max 1 + patta + noise + aqua, am1 + patta + noise + aqua, air max one + patta + noise + aqua, air max + patta + noise + aqua, patta + waves + noise + aqua, patta + wave + noise + aqua, nike + patta + noise + aqua",
    "Air Max 1 Patta Waves Monarch": "air max 1 + patta + monarch, am1 + patta + monarch, air max one + patta + monarch, air max + patta + monarch, patta + waves + monarch, patta + wave + monarch, nike + patta + monarch, air max 1 + patta + monarc, am1 + patta + monarc, air max one + patta + monarc, air max + patta + monarc, patta + waves + monarc, patta + wave + monarc, nike + patta + monarc, air max 1 + patta + monach, am1 + patta + monach, air max one + patta + monach, air max + patta + monach, patta + waves + monach, patta + wave + monach, nike + patta + monach, air max 1 + patta + monarque, am1 + patta + monarque, air max one + patta + monarque, air max + patta + monarque, patta + waves + monarque, patta + wave + monarque, nike + patta + monarque, air max 1 + patta + orange, am1 + patta + orange, air max one + patta + orange, air max + patta + orange, patta + waves + orange, patta + wave + orange, nike + patta + orange",
    "Air Max 1 Patta Waves Rush Maroon": "air max 1 + patta + rush, am1 + patta + rush, air max one + patta + rush, air max + patta + rush, patta + waves + rush, patta + wave + rush, nike + patta + rush, air max 1 + patta + maroon, am1 + patta + maroon, air max one + patta + maroon, air max + patta + maroon, patta + waves + maroon, patta + wave + maroon, nike + patta + maroon, air max 1 + patta + maron, am1 + patta + maron, air max one + patta + maron, air max + patta + maron, patta + waves + maron, patta + wave + maron, nike + patta + maron, air max 1 + patta + bordeaux, am1 + patta + bordeaux, air max one + patta + bordeaux, air max + patta + bordeaux, patta + waves + bordeaux, patta + wave + bordeaux, nike + patta + bordeaux, air max 1 + patta + burgundy, am1 + patta + burgundy, air max one + patta + burgundy, air max + patta + burgundy, patta + waves + burgundy, patta + wave + burgundy, nike + patta + burgundy, air max 1 + patta + rouge, am1 + patta + rouge, air max one + patta + rouge, air max + patta + rouge, patta + waves + rouge, patta + wave + rouge, nike + patta + rouge, air max 1 + patta + red, am1 + patta + red, air max one + patta + red, air max + patta + red, patta + waves + red, patta + wave + red, nike + patta + red",
    "Air Max 1 Patta Waves Black": "air max 1 + patta + black, am1 + patta + black, air max one + patta + black, air max + patta + black, patta + waves + black, patta + wave + black, nike + patta + black, air max 1 + patta + noir, am1 + patta + noir, air max one + patta + noir, air max + patta + noir, patta + waves + noir, patta + wave + noir, nike + patta + noir, air max 1 + patta + noire, am1 + patta + noire, air max one + patta + noire, air max + patta + noire, patta + waves + noire, patta + wave + noire, nike + patta + noire, air max 1 + patta + noires, am1 + patta + noires, air max one + patta + noires, air max + patta + noires, patta + waves + noires, patta + wave + noires, nike + patta + noires, air max 1 + patta + noirs, am1 + patta + noirs, air max one + patta + noirs, air max + patta + noirs, patta + waves + noirs, patta + wave + noirs, nike + patta + noirs",
    "Air Max 1 Patta Waves White": "air max 1 + patta + white, am1 + patta + white, air max one + patta + white, air max + patta + white, patta + waves + white, patta + wave + white, nike + patta + white, air max 1 + patta + blanc, am1 + patta + blanc, air max one + patta + blanc, air max + patta + blanc, patta + waves + blanc, patta + wave + blanc, nike + patta + blanc, air max 1 + patta + blanche, am1 + patta + blanche, air max one + patta + blanche, air max + patta + blanche, patta + waves + blanche, patta + wave + blanche, nike + patta + blanche, air max 1 + patta + blanches, am1 + patta + blanches, air max one + patta + blanches, air max + patta + blanches, patta + waves + blanches, patta + wave + blanches, nike + patta + blanches, air max 1 + patta + blancs, am1 + patta + blancs, air max one + patta + blancs, air max + patta + blancs, patta + waves + blancs, patta + wave + blancs, nike + patta + blancs",
    "Air Max 1 Lemonade": "air max 1 + lemonade, am1 + lemonade, air max one + lemonade, air max + lemonade, air max 1 + limonade, am1 + limonade, air max one + limonade, air max + limonade, air max 1 + lemonad, am1 + lemonad, air max one + lemonad, air max + lemonad, air max 1 + leamade, am1 + leamade, air max one + leamade, air max + leamade, air max 1 + lemon + wash, am1 + lemon + wash, air max one + lemon + wash, air max + lemon + wash, air max 1 + lemon + powerwall, am1 + lemon + powerwall, air max one + lemon + powerwall, air max + lemon + powerwall",
    "Air Max 1 Curry": "air max 1 + curry, am1 + curry, air max one + curry, air max + curry, air max 1 + curri, am1 + curri, air max one + curri, air max + curri, air max 1 + currys, am1 + currys, air max one + currys, air max + currys, air max 1 + dark + curry, am1 + dark + curry, air max one + dark + curry, air max + dark + curry",
    "Air Max 1 Watermelon": "air max 1 + watermelon, am1 + watermelon, air max one + watermelon, air max + watermelon, air max 1 + water + melon, am1 + water + melon, air max one + water + melon, air max + water + melon, air max 1 + pasteque, am1 + pasteque, air max one + pasteque, air max + pasteque, air max 1 + watermelone, am1 + watermelone, air max one + watermelone, air max + watermelone, air max 1 + sunset + pulse, am1 + sunset + pulse, air max one + sunset + pulse, air max + sunset + pulse",
    "Air Max 1 Safari Cobblestone": "air max 1 + cobblestone, am1 + cobblestone, air max one + cobblestone, air max + cobblestone, air max 1 + cobble + stone, am1 + cobble + stone, air max one + cobble + stone, air max + cobble + stone, air max 1 + safari + bleu, am1 + safari + bleu, air max one + safari + bleu, air max + safari + bleu, air max 1 + safari + blue, am1 + safari + blue, air max one + safari + blue, air max + safari + blue, air max 1 + safari + beige, am1 + safari + beige, air max one + safari + beige, air max + safari + beige, air max 1 + safari + worn, am1 + safari + worn, air max one + safari + worn, air max + safari + worn",
    "Blank Canvas": "jordan + blank, jordan 4 + blank, jordan4 + blank, j4 + blank, aj4 + blank, jordan iv + blank, jordan + blanc + canvas, jordan 4 + blanc + canvas, jordan4 + blanc + canvas, j4 + blanc + canvas, aj4 + blanc + canvas, jordan iv + blanc + canvas, jordan + canvas + sail, jordan 4 + canvas + sail, jordan4 + canvas + sail, j4 + canvas + sail, aj4 + canvas + sail, jordan iv + canvas + sail, jordan + canvas + beige, jordan 4 + canvas + beige, jordan4 + canvas + beige, j4 + canvas + beige, aj4 + canvas + beige, jordan iv + canvas + beige, jordan + canvas + creme, jordan 4 + canvas + creme, jordan4 + canvas + creme, j4 + canvas + creme, aj4 + canvas + creme, jordan iv + canvas + creme, jordan + canvas + blanche, jordan 4 + canvas + blanche, jordan4 + canvas + blanche, j4 + canvas + blanche, aj4 + canvas + blanche, jordan iv + canvas + blanche, jordan + canvas + blanc, jordan 4 + canvas + blanc, jordan4 + canvas + blanc, j4 + canvas + blanc, aj4 + canvas + blanc, jordan iv + canvas + blanc",
    "Wet Cement": "jordan + wet + cement, jordan 4 + wet + cement, jordan4 + wet + cement, j4 + wet + cement, aj4 + wet + cement, jordan iv + wet + cement, jordan + wetcement, jordan 4 + wetcement, jordan4 + wetcement, j4 + wetcement, aj4 + wetcement, jordan iv + wetcement, jordan + wet + ciment, jordan 4 + wet + ciment, jordan4 + wet + ciment, j4 + wet + ciment, aj4 + wet + ciment, jordan iv + wet + ciment, jordan + smoke + grey, jordan 4 + smoke + grey, jordan4 + smoke + grey, j4 + smoke + grey, aj4 + smoke + grey, jordan iv + smoke + grey, jordan + smoke + gray, jordan 4 + smoke + gray, jordan4 + smoke + gray, j4 + smoke + gray, aj4 + smoke + gray, jordan iv + smoke + gray, jordan + gris + fume, jordan 4 + gris + fume, jordan4 + gris + fume, j4 + gris + fume, aj4 + gris + fume, jordan iv + gris + fume",
    "Air Max 1 Anniversary Aqua": "air max 1 + anniversary + aqua, am1 + anniversary + aqua, air max one + anniversary + aqua, air max + anniversary + aqua, air max 1 + anniversary + turquoise, am1 + anniversary + turquoise, air max one + anniversary + turquoise, air max + anniversary + turquoise, air max 1 + anniversary + bleu + ciel, am1 + anniversary + bleu + ciel, air max one + anniversary + bleu + ciel, air max + anniversary + bleu + ciel, air max 1 + anniversary + teal, am1 + anniversary + teal, air max one + anniversary + teal, air max + anniversary + teal, air max 1 + anniversaire + aqua, am1 + anniversaire + aqua, air max one + anniversaire + aqua, air max + anniversaire + aqua, air max 1 + anniversaire + turquoise, am1 + anniversaire + turquoise, air max one + anniversaire + turquoise, air max + anniversaire + turquoise",
    "Thunder": "jordan + thunder, jordan 4 + thunder, jordan4 + thunder, j4 + thunder, aj4 + thunder, jordan iv + thunder, jordan + thunders, jordan 4 + thunders, jordan4 + thunders, j4 + thunders, aj4 + thunders, jordan iv + thunders, jordan + noir + jaune, jordan 4 + noir + jaune, jordan4 + noir + jaune, j4 + noir + jaune, aj4 + noir + jaune, jordan iv + noir + jaune, jordan + noire + jaune, jordan 4 + noire + jaune, jordan4 + noire + jaune, j4 + noire + jaune, aj4 + noire + jaune, jordan iv + noire + jaune, jordan + black + yellow, jordan 4 + black + yellow, jordan4 + black + yellow, j4 + black + yellow, aj4 + black + yellow, jordan iv + black + yellow",
    "Nocta Glide White": "nocta + glide + white, nocta + glide + blanc, nocta + glide + blanche, nocta + glide + blanches, nocta + glide + triple + white, nocta + glid + white, nocta + glid + blanc, nocta + glid + blanche, nocta + glid + blanches, nocta + glid + triple + white, nocta + glides + white, nocta + glides + blanc, nocta + glides + blanche, nocta + glides + blanches, nocta + glides + triple + white",
    "Rare Air": "jordan + rare + air, jordan 4 + rare + air, jordan4 + rare + air, j4 + rare + air, aj4 + rare + air, jordan iv + rare + air, jordan + rareair, jordan 4 + rareair, jordan4 + rareair, j4 + rareair, aj4 + rareair, jordan iv + rareair, jordan + rare, jordan 4 + rare, jordan4 + rare, j4 + rare, aj4 + rare, jordan iv + rare",
    "Bred Reimagined": "jordan + reimagined, jordan 4 + reimagined, jordan4 + reimagined, j4 + reimagined, aj4 + reimagined, jordan iv + reimagined, jordan + reimagine, jordan 4 + reimagine, jordan4 + reimagine, j4 + reimagine, aj4 + reimagine, jordan iv + reimagine, jordan + remagined, jordan 4 + remagined, jordan4 + remagined, j4 + remagined, aj4 + remagined, jordan iv + remagined, jordan + reimaged, jordan 4 + reimaged, jordan4 + reimaged, j4 + reimaged, aj4 + reimaged, jordan iv + reimaged, jordan + re + imagined, jordan 4 + re + imagined, jordan4 + re + imagined, j4 + re + imagined, aj4 + re + imagined, jordan iv + re + imagined, jordan + reimagened, jordan 4 + reimagened, jordan4 + reimagened, j4 + reimagened, aj4 + reimagened, jordan iv + reimagened",
    "Shimmer": "jordan + shimmer, jordan 4 + shimmer, jordan4 + shimmer, j4 + shimmer, aj4 + shimmer, jordan iv + shimmer, jordan + shimer, jordan 4 + shimer, jordan4 + shimer, j4 + shimer, aj4 + shimer, jordan iv + shimer, jordan + shimmers, jordan 4 + shimmers, jordan4 + shimmers, j4 + shimmers, aj4 + shimmers, jordan iv + shimmers, jordan + schimmer, jordan 4 + schimmer, jordan4 + schimmer, j4 + schimmer, aj4 + schimmer, jordan iv + schimmer",
    "Air Max 1 Limeade": "air max 1 + limeade, am1 + limeade, air max one + limeade, air max + limeade, air max 1 + limead, air max 1 + powerwall + green, air max 1 + powerwall + vert, air max 1 + pine + green, am1 + pine + green, air max 1 + vert + sapin",
}


# Couleurs et mots courants des titres : jamais pris pour une faute de frappe (pink ≠ pine, rose ≠ ...)
COURANTS = ("pink rose black noir noire white blanc blanche blanche red rouge blue bleu bleue green vert verte "
            "grey gray gris grise yellow jaune orange purple violet violette brown marron beige cream creme "
            "gold silver dark light clair fonce fire high mid low retro nike air baskets basket sneakers "
            "shoes chaussures taille homme femme enfant neuf neuve bon etat")
bot.VOCAB.update(COURANTS.split())

# Mots connus de toutes les paires : un mot du titre qui est lui-même le mot d'une autre paire
# (black/blank…) n'est jamais pris pour une faute de frappe (comme dans le bot titre).
for _g in MOTS.values():
    for _t in _g.split(","):
        for _m in _t.split("+"):
            bot.VOCAB.add("".join(bot.tokens(_m)))
            bot.VOCAB.update(bot.tokens(_m))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", bot.norm(s)).strip("-")


def post(webhook, payload):
    r = requests.post(webhook, json=payload, timeout=20)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)))
        requests.post(webhook, json=payload, timeout=20)
    time.sleep(1)


def modele_depuis_commit():
    """Lancement auto : retrouve le modèle d'après le dossier où des photos ont été ajoutées."""
    try:
        c = json.loads(os.environ.get("HEAD_COMMIT") or "null") or {}
    except ValueError:
        c = {}
    par_slug = {slug(k): k for k in COLORIS}
    fichiers = [l.strip() for l in (os.environ.get("CHANGES") or "").splitlines() if l.strip()]
    for f in fichiers + (c.get("added") or []) + (c.get("modified") or []):
        parts = f.split("/")
        if len(parts) >= 3 and parts[0] == "references" and parts[1] in par_slug:
            return par_slug[parts[1]]
    return None


def jeton(url):
    m = re.search(r"/t/([^/]+)/", url or "")
    return m.group(1) if m else url


PAGES = {}     # dernières pages d'annonce lues (id -> texte)


def vendeur(item_id):
    """Pseudo, id, lien du profil, nombre d'avis et note (sur 5) du vendeur, lus dans la page de
    l'annonce déjà ouverte. On cherche d'abord le bloc « user » de l'annonce (id + login proches),
    pour ne pas prendre le pseudo d'un autre membre affiché sur la page."""
    full = PAGES.get(str(item_id)) or ""
    q = r'\\?"'
    def cherche(cle, motif, texte):
        m = re.search(q + cle + q + r'\s*:\s*\\?"?(' + motif + ')', texte)
        return m.group(1) if m else None
    bloc, uid = full, None
    m = re.search(q + "user" + q + r"\s*:\s*\{\s*" + q + "id" + q + r"\s*:\s*(\d+)", full)
    if m:
        uid, bloc = m.group(1), full[m.start():m.start() + 4000]
    else:
        uid = cherche("user_id", r"\d+", full)
    rep = cherche("feedback_reputation", r"[0-9.]+", bloc) or cherche("feedback_reputation", r"[0-9.]+", full)
    nb = cherche("feedback_count", r"\d+", bloc) or cherche("feedback_count", r"\d+", full)
    login = cherche("login", r'[^"\\]+', bloc) or cherche("login", r'[^"\\]+', full)
    # 1/11 : le pseudo et l'id ne sont plus sous « login » / « user » : autres noms possibles
    for cle in ("userLogin", "user_login", "username", "userName", "sellerLogin", "seller_login"):
        if login:
            break
        login = cherche(cle, r'[^"\\]+', full)
    for cle in ("userId", "seller_id", "sellerId", "owner_id", "ownerId"):
        if uid:
            break
        uid = cherche(cle, r"\d+", full)
    if not uid:      # lien du profil écrit dans la page : /member/123456-pseudo
        m = re.search(r"/member/(\d+)(?:-([A-Za-z0-9_.\-]+))?", full)
        if m:
            uid = m.group(1)
            login = login or m.group(2)
    return {"pseudo": login, "id": uid,
            "profil": f"https://www.vinted.fr/member/{uid}" if uid else None,
            "avis": int(nb) if nb else None,
            "etoiles": round(float(rep) * 5, 1) if rep else None}


def boite(item_id, titre=""):
    """1/11 : boîte d'après le titre et la description de l'annonce (gratuit, sans IA).
    Renvoie « Og Box », « Boîte de remplacement », « No box » ou « Non précisé »."""
    full = PAGES.get(str(item_id)) or ""
    desc = ""
    m = re.search(r'\\?"description\\?"\s*:\s*\\?"(.{0,1500}?)\\?"\s*,', full, re.S)
    if m:
        desc = m.group(1).replace("\\n", " ")
    t = " " + re.sub(r"[^a-z0-9]+", " ", bot.norm(f"{titre} {desc}")) + " "
    # 3/11 : « envoi soigné dans une boîte / un carton » parle de l'emballage, pas de la boîte d'origine
    t = re.sub(r" (envoi|envoye|envoyee|envoyees|expedie|expediee|expediees|expedition|emballe|emballee|emballees"
               r"|livre|livree|livrees|colis|protege|protegee|protegees)(?: (?!boite|box|carton|avec)[a-z]+){0,3}? (dans|en) (une |un |sa |la |leur )?"
               r"(boite|box|carton) ", " ", t)
    if re.search(r" (sans|pas de|pas d|no|without|aucune|plus de|pas la|pas sa|plus la|plus sa|sans sa|sans la|sans leur"
                 r"|pas avec la|pas avec sa|no og) (la |de |d |sa |og )?(boite|box|boxe) ", t) \
            or re.search(r" (boite|box) (absente|manquante|perdue|jetee|non fournie|non incluse|non inclus|pas incluse"
                         r"|pas inclus|pas fournie|not included|non livree|non disponible) ", t) or " nobox " in t:
        return "No box"
    if re.search(r" (boite|box) (de )?(remplacement|generique|autre|pas d origine|non d origine) ", t):
        return "Boîte de remplacement"
    if re.search(r" (boite|box|boxe|og box|double box) ", t):
        return "Og Box"
    return "Non précisé"


def publiee_le(item_id):
    """V4 : heure de publication de l'annonce (secondes depuis 1970), lue dans sa page déjà ouverte.
    On prend la date la plus récente entre la création et une éventuelle remise en avant. None si introuvable."""
    full = PAGES.get(str(item_id)) or ""
    if not full:
        return None
    q = r'\\?"'
    # Zone de l'annonce elle-même (la page contient aussi d'autres annonces « similaires »)
    zones = []
    for m in re.finditer(q + r"id" + q + r"\s*:\s*" + str(item_id) + r"\b", full):
        zones.append(full[max(0, m.start() - 3000):m.start() + 6000])
    zones.append(full)
    cles = ("created_at_ts", "createdAt", "created_at", "last_push_up_at", "pushed_up_at")
    for zone in zones:
        dates = []
        for cle in cles:
            for m in re.finditer(q + cle + q + r"\s*:\s*\\?\"?([0-9T:\-+.Z ]{10,32})", zone):
                v = m.group(1).strip()
                try:
                    if v.isdigit():
                        t = int(v) / (1000 if len(v) > 11 else 1)
                    else:
                        t = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                    if 1.6e9 < t < 2.2e9:
                        dates.append(t)
                except ValueError:
                    pass
                break       # 1re valeur de chaque clé dans la zone
        if dates:
            return max(dates)
    i = full.find("created")
    print(f"[délai] {item_id} : date introuvable (extrait : {full[max(0, i - 60):i + 120]!r})" if i >= 0
          else f"[délai] {item_id} : aucun champ « created » dans la page")
    diag_publication(item_id, full)
    return None


DIAG = Path(__file__).with_name("diag_publication.txt")


def diag_publication(item_id, full):
    """Une fois : relève dans la page tous les champs qui ressemblent à une date ou une heure (noms + valeurs courtes),
    enregistré dans diag_publication.txt (envoyé sur GitHub avec la sauvegarde) pour trouver où Vinted met l'heure."""
    if DIAG.exists():
        return
    motif = (r'\\?"([A-Za-z_]*(?:time|Time|date|Date|created|Created|updated|Updated|push|Push|_at|_ts|At|ago|Ago|login|Login|user|User|seller|Seller|member|Member|owner|Owner)[A-Za-z_]*)'
             r'\\?"\s*:\s*\\?"?([^",}\\]{0,40})')
    vus, lignes = set(), []
    for m in re.finditer(motif, full):
        k = (m.group(1), m.group(2)[:40])
        if k not in vus:
            vus.add(k)
            pos = m.start()
            lignes.append(f"{m.group(1)} = {m.group(2)[:40]}   (position {pos})")
        if len(lignes) >= 400:
            break
    lignes += [f"lien profil : {u}" for u in list(dict.fromkeys(re.findall(r"/member/[0-9]+[A-Za-z0-9_.-]*", full)))[:10]]
    pos_id = [m.start() for m in re.finditer(str(item_id), full)][:20]
    DIAG.write_text(f"annonce {item_id} · page de {len(full)} caractères · id trouvé aux positions {pos_id}\n"
                    + "\n".join(lignes), encoding="utf-8")


def champ_vendeur(vend):
    """Texte du champ « Vendeur » des alertes Discord : pseudo cliquable (profil) + avis."""
    vend = vend or {}
    nom = vend.get("pseudo") or "profil"
    lien = f"[{nom}]({vend['profil']})" if vend.get("profil") else (vend.get("pseudo") or "?")
    if vend.get("avis") is not None:
        lien += f"\n{vend['avis']} avis" + (f" · {vend['etoiles']}/5" if vend.get("etoiles") is not None else "")
    return lien


def photos_annonce(session, item):
    """Toutes les photos d'une annonce (on ouvre sa page). Liste vide si échec."""
    r = session.get(item["url"], timeout=30)
    if r.status_code != 200:
        print(f"[diag] annonce {item['id']} : HTTP {r.status_code}")
        return []
    full = r.text
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", r.text, re.S):
        try:
            arr = json.loads(m.group(1))
            if len(arr) > 1 and isinstance(arr[1], str):
                full += arr[1]
        except ValueError:
            pass
    PAGES[str(item["id"])] = full          # gardée pour lire le vendeur (onglet COP sneakers)
    while len(PAGES) > 50:      # plusieurs annonces lues en même temps : on ignore une clé déjà retirée
        try:
            PAGES.pop(next(iter(PAGES)), None)
        except RuntimeError:
            break
    cible = jeton(item["photo"])
    dec = json.JSONDecoder()
    for m in re.finditer(r'"photos":\[', full):
        try:
            arr, _ = dec.raw_decode(full, m.end() - 1)
        except ValueError:
            continue
        urls = [p.get("full_size_url") or p.get("url") for p in arr if isinstance(p, dict)]
        urls = [u for u in urls if u]
        # On garde la liste qui contient la photo de l'annonce (et pas celles des annonces suggérées).
        if urls and any(jeton(u) == cible for u in urls):
            return urls
    # Plan B : adresses d'images dans la page, en partant de la photo de l'annonce.
    brut = full.replace("\\u0026", "&").replace("\\/", "/")
    urls, vus = [], set()
    for u in re.findall(r"https://images\d*\.vinted\.net/t/[^\"\\\s<>]+", brut):
        j = jeton(u)
        if j not in vus:
            vus.add(j)
            urls.append(u)
    if cible in vus:
        i = [jeton(u) for u in urls].index(cible)
        return urls[i:i + 20]
    return []


def main():
    auto = not os.environ.get("MODELE")
    modele = os.environ.get("MODELE") or modele_depuis_commit() or "Vivid Sulfur"
    prix_max = (os.environ.get("PRIX_MAX") or "").strip().lower()
    recherche = (os.environ.get("RECHERCHE") or "").strip()
    pages = int(os.environ.get("PAGES") or 3)
    m = re.search(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/[\w/-]+",
                  os.environ.get("DISCORD_WEBHOOK", ""))
    webhook = m.group(0) if m else None
    if modele not in COLORIS:
        sys.exit(f"Modèle inconnu : {modele}")

    # 1. Lire les annonces
    if prix_max in ("", "defaut", "défaut"):
        prix_max = str(PRIX.get(modele, ""))
    elif prix_max in ("aucun", "0"):
        prix_max = ""
    url = (BASE_URL + (f"&price_to={prix_max}" if prix_max else "")
           + (f"&search_text={requests.utils.quote(recherche)}" if recherche
              else RECHERCHES.get(modele, "")))      # Air Max : texte « air max 1 » par défaut
    base, purl = bot.page_url(url)
    session = bot.get_session()
    items, ids = [], set()
    for pg in range(1, pages + 1):
        try:
            for it in bot.fetch_items(session, base, purl + (f"&page={pg}" if pg > 1 else "")):
                if it["id"] not in ids and it.get("photo"):
                    ids.add(it["id"])
                    items.append(it)
        except Exception as e:
            print(f"Page {pg} illisible : {e}")
        time.sleep(3)
    # Sécurité : rien au-dessus du prix max, même si Vinted en renvoie.
    if prix_max:
        avant = len(items)
        items = [it for it in items if (bot.price_value(it) or 0) <= float(prix_max)]
        if avant != len(items):
            print(f"{avant - len(items)} annonce(s) au-dessus de {prix_max} € retirées")
    groupes = [g.strip() for g in MOTS[modele].split(",")]
    # Témoins (option, désactivée par défaut) : vraies annonces du modèle, SANS limite
    # de prix, pour calibrer le seuil. Jamais comptées comme résultats.
    if (os.environ.get("TEMOINS") or "non").strip().lower() == "oui":
        turl = ("https://www.vinted.fr/catalog?catalog[]=1242&order=relevance&search_text="
                + requests.utils.quote(f"jordan 4 {modele}"))
        try:
            for it in bot.fetch_items(session, "https://www.vinted.fr", turl):
                if it["id"] not in ids and it.get("photo") and bot.matches_required(it, groupes):
                    ids.add(it["id"])
                    it["temoin"] = True
                    items.append(it)
        except Exception as e:
            print(f"Témoins illisibles : {e}")
    nb_temoins = sum(1 for x in items if x.get("temoin"))
    print(f"{len(items)} annonces avec photo, dont {nb_temoins} témoins")
    if not items:
        sys.exit("Aucune annonce lue.")

    # 2. Charger le modèle CLIP
    model, _, prep = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
    tok = open_clip.get_tokenizer("ViT-B-32")
    model.eval()

    def embed_images(imgs):
        with torch.no_grad():
            f = model.encode_image(torch.stack([prep(i) for i in imgs]))
        return f / f.norm(dim=-1, keepdim=True)

    labels = list(COLORIS)
    with torch.no_grad():
        t = model.encode_text(tok([f"a photo of {COLORIS[k]}" for k in labels]))
    t = t / t.norm(dim=-1, keepdim=True)
    cible = labels.index(modele)

    # 3. Photos de référence
    ref_dir = Path("references") / slug(modele)
    refs = []
    for p in sorted(ref_dir.glob("*")) if ref_dir.exists() else []:
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                refs.append(Image.open(p).convert("RGB"))
            except Exception as e:
                print(f"Référence illisible {p.name} : {e}")
    ref_emb = embed_images(refs) if refs else None
    # Pièges : photos de modèles qui ressemblent mais qui ne sont PAS le bon.
    pieges, noms_pieges = [], []
    piege_dir = ref_dir / "pas-ca"
    for p in sorted(piege_dir.glob("*")) if piege_dir.exists() else []:
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                pieges.append(Image.open(p).convert("RGB"))
                noms_pieges.append(p.stem)
            except Exception as e:
                print(f"Piège illisible {p.name} : {e}")
    piege_emb = embed_images(pieges) if pieges else None
    mode = f"{len(refs)} photo(s) de référence" if refs else "description texte seule (aucune photo de référence)"
    if pieges:
        mode += f" + {len(pieges)} photo(s) piège"
    print(f"Mode : {mode}")

    # 4. Analyser chaque annonce
    res = []
    for i, it in enumerate(items, 1):
        try:
            r = session.get(it["photo"], timeout=20)
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception as e:
            print(f"Photo illisible {it['id']} : {e}")
            continue
        e = embed_images([img])
        probs = (100 * e @ t.T).softmax(dim=-1)[0]
        top = int(probs.argmax())
        if piege_emb is not None:
            sp = (e @ piege_emb.T)[0]
            it["piege"] = float(sp.max())
            it["piege_nom"] = noms_pieges[int(sp.argmax())]
        it.update({
            "ref": float((e @ ref_emb.T).max()) if ref_emb is not None else None,
            "prob": float(probs[cible]),
            "devine": labels[top],
            "devine_prob": float(probs[top]),
            "titre_ok": bot.matches_required(it, groupes),
        })
        res.append(it)
        if i % 25 == 0:
            print(f"{i}/{len(items)} photos analysées")

    for x in res:
        if x["ref"] is not None and "piege" in x:
            x["marge"] = x["ref"] - x["piege"]
    if ref_emb is not None and piege_emb is not None:
        key = "marge"
    elif ref_emb is not None:
        key = "ref"
    else:
        key = "prob"
    res.sort(key=lambda x: x[key], reverse=True)

    # Étape 1 : est-ce le bon modèle ? Seuil choisi à la main, sinon calé sur les témoins
    # (80 % des vraies paires témoins sont au-dessus du seuil automatique).
    seuil_txt = (os.environ.get("SEUIL") or "").strip().replace(",", ".")
    sc_t = sorted((x[key] for x in res if x.get("temoin")), reverse=True)
    if seuil_txt:
        seuil, origine_seuil = float(seuil_txt), "choisi à la main"
    elif len(sc_t) >= 3:
        seuil, origine_seuil = sc_t[int(0.8 * (len(sc_t) - 1))], "automatique : 80 % des témoins au-dessus"
    else:
        seuil = {"marge": 0.0, "ref": 0.84, "prob": 0.5}[key]
        origine_seuil = "par défaut"
    for x in res:
        x["oui"] = x[key] >= seuil

    # 5. Résultats complets (fichier téléchargeable)
    with open("resultats.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["rang", "temoin", "score_reference", "score_piege", "piege_le_plus_proche", "marge",
                    f"proba_{modele}", "clip_pense", "titre_mentionne_modele", "prix", "titre", "lien"])
        for n, x in enumerate(res, 1):
            w.writerow([n, "oui" if x.get("temoin") else "", "" if x["ref"] is None else f"{x['ref']:.3f}",
                        f"{x['piege']:.3f}" if "piege" in x else "", x.get("piege_nom", ""),
                        f"{x['marge']:+.3f}" if "marge" in x else "", f"{x['prob']:.2f}",
                        x["devine"], "oui" if x["titre_ok"] else "non", x["price"], x["title"], x["url"]])
    for n, x in enumerate(res[:30], 1):
        print(f"{n:>3} | ref {x['ref'] if x['ref'] is None else round(x['ref'], 3)} | {x['devine']} {x['devine_prob']:.0%} | titre {'OK' if x['titre_ok'] else '--'} | {x['title'][:60]}")

    if not webhook:
        print("Pas de webhook Discord : résultats seulement dans les logs.")
        return

    # 6. Discord
    rangs_t = [n for n, x in enumerate(res, 1) if x.get("temoin")]
    devine_cible = [x for x in res if x["devine"] == modele]
    caches = [x for x in devine_cible if not x["titre_ok"]]
    titre_ok = sum(x["titre_ok"] for x in res)
    lignes = [
        f"**Test photo — {modele}**" + (" (lancé automatiquement après ajout de photos)" if auto else ""),
        f"Annonces analysées : {len(res)} (recherche : " + (f"« {recherche} »" if recherche else "aucun texte, toutes les baskets")
        + f", {pages} page(s), "
        + (f"prix max {prix_max} €)" if prix_max else "tous les prix)"),
        f"Mode : {mode}",
        f"Titre qui mentionne le modèle : {titre_ok}",
        f"CLIP pense que c'est une {modele} : {len(devine_cible)}, dont {len(caches)} avec un titre qui ne le dit pas",
    ]
    if rangs_t:
        sc = sorted((x[key] for x in res if x.get("temoin")), reverse=True)
        lignes.append(f"Témoins (vraies {modele} ajoutées exprès) : {len(rangs_t)}, classés aux rangs "
                      f"{', '.join(map(str, rangs_t[:12]))}{'…' if len(rangs_t) > 12 else ''}")
        lignes.append(f"Score des témoins ({'marge' if key == 'marge' else 'ressemblance' if key == 'ref' else 'proba'}) : "
                      f"du plus haut {sc[0]:.3f} au plus bas {sc[-1]:.3f}")
        autres = [x[key] for x in res if not x.get("temoin")]
        if autres:
            lignes.append(f"Meilleur score parmi les autres annonces : {max(autres):.3f}")
    else:
        lignes.append("Aucun témoin trouvé sur Vinted pour ce modèle.")
    if ref_emb is not None:
        for s in (0.90, 0.85, 0.80):
            lignes.append(f"Ressemblance ≥ {s:.2f} : {sum(x['ref'] >= s for x in res)}")
    if key == "marge":
        lignes.append(f"Marge positive (plus proche de tes références que des pièges) : {sum(x['marge'] > 0 for x in res)}")
    oui_autres = sum(bool(x["oui"] and not x.get("temoin")) for x in res)
    oui_t = sum(bool(x["oui"] and x.get("temoin")) for x in res)
    lignes.append(f"**Étape 1 — est-ce une {modele} ?** Seuil {seuil:.3f} ({origine_seuil}).")
    lignes.append(f"OUI : {oui_autres} annonce(s) hors témoins"
                  + (f" · témoins reconnus : {oui_t}/{len(rangs_t)}" if rangs_t else ""))
    lignes.append("Ci-dessous : les 10 annonces les plus ressemblantes, même si elles sont sous le seuil "
                  "(vert = OUI, gris = NON), puis les rangs 11 à 15. [T] = témoin.")
    post(webhook, {"content": "\n".join(lignes)})

    def embed(x, n, couleur):
        champs = [
            {"name": "Verdict étape 1", "value": "OUI" if x["oui"] else "NON", "inline": True},
            {"name": "Prix", "value": x["price"], "inline": True},
            {"name": "CLIP pense", "value": f"{x['devine']} ({x['devine_prob']:.0%})", "inline": True},
            {"name": "Titre", "value": "mentionne le modèle" if x["titre_ok"] else "ne le dit pas", "inline": True},
        ]
        if "marge" in x:
            champs.insert(0, {"name": "Piège le plus proche", "value": f"{x['piege_nom']} ({x['piege']:.3f})", "inline": True})
            champs.insert(0, {"name": "Marge", "value": f"{x['marge']:+.3f}", "inline": True})
        if x["ref"] is not None:
            champs.insert(0, {"name": "Ressemblance", "value": f"{x['ref']:.3f}", "inline": True})
        tag = "[T] " if x.get("temoin") else ""
        return {"title": f"#{n} · {tag}{x['title']}"[:250], "url": x["url"], "color": couleur,
                "fields": champs, "thumbnail": {"url": x["photo"]}}

    coul = lambda x: 0x2ECC71 if x["oui"] else 0x95A5A6
    post(webhook, {"embeds": [embed(x, n, coul(x)) for n, x in enumerate(res[:10], 1)]})
    if len(res) > 10:
        post(webhook, {"content": "**Zone de doute (rangs 11 à 15)**",
                       "embeds": [embed(x, n, coul(x)) for n, x in enumerate(res[10:15], 11)]})

    # 7. Contrôle de l'étiquette intérieure sur les meilleures candidates
    nb = int(os.environ.get("VERIF") or 20)
    if nb <= 0:
        return
    eti_dir = ref_dir / "etiquette"
    etis = []
    for p in sorted(eti_dir.glob("*")) if eti_dir.exists() else []:
        if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            try:
                etis.append(Image.open(p).convert("RGB"))
            except Exception as e:
                print(f"Étiquette de référence illisible {p.name} : {e}")
    eti_emb = embed_images(etis) if etis else None
    codes = CODES.get(modele, [])
    controle_ref = ""
    if codes and etis:
        try:
            etat = verifier_code(lire_texte(etis[0]), codes)
            controle_ref = ("Lecture de ton étiquette de référence : code " + etat[1] + " lu correctement."
                            if etat[0] == "ok" else
                            "Lecture de ton étiquette de référence : code NON lu (photo trop floue pour l'OCR ?).")
        except Exception as e:
            controle_ref = f"OCR indisponible : {e}"
        print(controle_ref)
    textes_eti = ["a close-up photo of the size label tag inside a sneaker, with barcode and small printed text",
                  "a photo of sneakers", "a photo of the sole of a sneaker", "a photo of a shoe box"]
    with torch.no_grad():
        te = model.encode_text(tok(textes_eti))
    te = te / te.norm(dim=-1, keepdim=True)

    # Étape 2 : seulement les annonces jugées OUI à l'étape 1
    candidates = [x for x in res if x["oui"] and not x.get("temoin")][:nb]
    candidates += [x for x in res if x["oui"] and x.get("temoin")][:2]   # 2 témoins pour comparer
    if not candidates:
        post(webhook, {"content": f"**Étape 2 — étiquette** : aucune annonce n'a passé l'étape 1, rien à ouvrir."})
        return
    bilan = []
    for x in candidates:
        time.sleep(2)
        try:
            urls = photos_annonce(session, x)
        except Exception as e:
            print(f"Annonce illisible {x['id']} : {e}")
            urls = []
        x["nb_photos"] = len(urls)
        meilleur, best_ref, notes = None, x["ref"], []
        for u in urls:
            try:
                img = Image.open(io.BytesIO(session.get(u, timeout=20).content)).convert("RGB")
            except Exception:
                continue
            e = embed_images([img])
            p_eti = float((100 * e @ te.T).softmax(dim=-1)[0][0])
            s_eti = float((e @ eti_emb.T).max()) if eti_emb is not None else None
            if ref_emb is not None:
                best_ref = max(best_ref or 0, float((e @ ref_emb.T).max()))
            note = s_eti if s_eti is not None else p_eti
            notes.append((note, u, img))
            if meilleur is None or note > meilleur[0]:
                meilleur = (note, u, p_eti, s_eti)
        x["ref_toutes"] = best_ref
        x["eti"] = meilleur
        x["eti_ok"] = bool(meilleur and (meilleur[2] >= 0.5 or (meilleur[3] or 0) >= 0.80))
        # Lecture du code sur les 3 photos qui ressemblent le plus à une étiquette
        x["code"] = (None, "")
        if codes:
            for note, u, img in sorted(notes, key=lambda t: t[0], reverse=True)[:3]:
                try:
                    etat = verifier_code(lire_texte(img), codes)
                except Exception as e:
                    print(f"OCR impossible : {e}")
                    break
                if etat[0] == "ok":
                    x["code"] = etat
                    x["eti"] = (note, u) + tuple(x["eti"][2:]) if x["eti"] else (note, u, 0, None)
                    break
                if etat[0] == "autre" and x["code"][0] is None:
                    x["code"] = etat
        bilan.append(x)
        print(f"[étiquette] {x['id']} : {len(urls)} photos, étiquette {'oui' if x['eti_ok'] else 'non'}"
              + (f" (proba {meilleur[2]:.2f}, ressemblance étiquette {meilleur[3]})" if meilleur else ""))

    ordre = {"ok": 0, None: 1, "autre": 2}
    bilan.sort(key=lambda x: (ordre[x["code"][0]], not x["eti_ok"], -(x["ref_toutes"] or 0)))
    avec = sum(x["eti_ok"] for x in bilan)
    bons = sum(x["code"][0] == "ok" for x in bilan)
    lues = sum(x["nb_photos"] > 0 for x in bilan)
    post(webhook, {"content": (
        f"**Étape 2 — étiquette** : {len(bilan)} annonce(s) jugées OUI ouvertes "
        f"({lues} pages lues), {avec} avec une photo d'étiquette repérée.\n"
        + (f"Code attendu : {', '.join(codes)} — trouvé dans {bons} annonce(s).\n" if codes else "")
        + (controle_ref + "\n" if controle_ref else "") +
        "Grande image = la photo qui ressemble le plus à une étiquette. Petite image = 1re photo. "
        "« Ressemblance (toutes photos) » = meilleure ressemblance sur toutes les photos de l'annonce.")})
    embeds = []
    for x in bilan:
        champs = [
            {"name": "Prix", "value": x["price"], "inline": True},
            {"name": "Photos", "value": str(x["nb_photos"]), "inline": True},
        ]
        et, lu = x["code"]
        statut = (f"trouvée, lisible : {lu} = BON CODE" if et == "ok" else
                  f"trouvée, lisible : {lu} = AUTRE CODE" if et == "autre" else
                  "trouvée, mais code illisible : à vérifier à l'oeil" if x["eti_ok"] else
                  "pas de photo d'étiquette : à demander au vendeur")
        champs.append({"name": "Étiquette", "value": statut, "inline": False})
        if x["ref_toutes"] is not None:
            champs.insert(1, {"name": "Ressemblance (toutes photos)", "value": f"{x['ref_toutes']:.3f}", "inline": True})
        if x["eti"]:
            v = f"{x['eti'][2]:.0%}" + (f" · proche de ton étiquette {x['eti'][3]:.3f}" if x["eti"][3] is not None else "")
            champs.append({"name": "Score étiquette", "value": v, "inline": False})
        e = {"title": (("[T] " if x.get("temoin") else "") + x["title"])[:250], "url": x["url"],
             "color": 0x2ECC71 if x["code"][0] == "ok" else 0xE74C3C if x["code"][0] == "autre"
                      else 0x3498DB if x["eti_ok"] else 0x95A5A6, "fields": champs,
             "thumbnail": {"url": x["photo"]}}
        if x["eti"]:
            e["image"] = {"url": x["eti"][1]}
        embeds.append(e)
    for i in range(0, len(embeds), 4):   # 4 par message pour garder des grandes images lisibles
        post(webhook, {"embeds": embeds[i:i + 4]})


if __name__ == "__main__":
    main()
