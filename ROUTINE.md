# Routine « Vérification batterie » (pour Claude)

Page : https://claude.ai/artifact/UEUNmHdoz3e1mj5RhJzyNf
Base de la page (outil ArtifactData) :
- `batteries/<slug>` : manche affichée (modele, etape, nom_etape, batterie, seuil, prix_max, terminees…)
- `annonces/<slug>__<V>__<id>` : une carte (anciennes cartes V1 : `<slug>__<id>`, sans champ etape = V1)
- `verdicts/<même clé que l'annonce>` : réponses de Bastien (slug, id, etape, rep oui|non)

Une batterie se joue en 5 manches (V1 Seuil, V2 Sans le bon titre, V3 Titres, V4 Prix max, V5 Étiquette).
Chaque manche apporte de nouvelles annonces choisies grâce aux réponses d'avant.

## 1. Réponses → repo
1. `ArtifactData list` de `verdicts` avec `out_dir` (dossier temporaire), toutes les pages (`limit` 1000).
2. `python page_sync.py verdicts <dossier>` → écrit `verdicts/<slug>.json` si quelque chose a changé.
3. Si changé : commit + push sur main. Le workflow « Apprentissage » ajoute pièges / références, recalcule le
   seuil, poste le bilan sur Discord et, si la manche en cours est entièrement jugée, lance la manche suivante
   (workflow « Batterie de tests », 15 à 25 min).

## 2. Attendre la manche suivante (seulement si une manche vient d'être terminée)
`gh run list -R sorareodwaga28-ai/Vinted-BOT-TEST-1 -w "Batterie de tests" -L 3`, puis `gh run watch <id>`
jusqu'à la fin (timeout 600000 par appel, relancer si besoin ; au plus ~40 min au total). Puis `git pull`.

## 3. Repo → page
`python page_sync.py etat` donne la manche en cours de chaque modèle. Pour chaque modèle dont le fichier
`batteries/<slug>/<V>.json` a une `batterie` différente du doc `batteries/<slug>` de la page (ou une autre manche) :
1. `python page_sync.py vignettes <slug> <V>` → envoyer chaque liste avec `Artifact` (publish, url de la page,
   `asset: true`, `file_paths`). Noter « <id annonce> <id fichier> » (nom du fichier = id annonce) dans un fichier.
2. `python page_sync.py annonces <slug> <V> <dossier> <fichier>` → `<dossier>/lots.json`.
3. `ArtifactData batch` pour chaque lot. Le doc `batteries/<slug>` existe déjà : le lire d'abord pour son `if_version`.
Ne jamais supprimer de réponses (`verdicts`).

## 4. Compte rendu
Une ou deux phrases, sans emoji : réponses envoyées, manche terminée ou non, manche chargée dans la page.
