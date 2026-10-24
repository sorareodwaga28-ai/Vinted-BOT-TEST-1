# Routine « Vérification batterie » (pour Claude)

Page : https://claude.ai/artifact/UEUNmHdoz3e1mj5RhJzyNf
Base de la page : collections `batteries` (doc = slug), `annonces` (doc = `<slug>__<id>`),
`verdicts` (doc = `<slug>__<id>`, champs slug, id, rep oui|non, batterie). Écrites avec l'outil ArtifactData.

## 1. Réponses de Bastien → repo (apprentissage)
1. `ArtifactData list` de `verdicts` avec `out_dir` (dossier temporaire), toutes les pages.
2. `python page_sync.py verdicts <dossier>` : écrit `verdicts/<slug>.json` si quelque chose a changé.
3. Si un fichier a changé : commit + push sur main. Le workflow « Apprentissage » applique les
   réponses (photos pas-ca / références, seuil par modèle dans `seuils.json`) et poste le bilan V1 à V5 sur Discord.

## 2. Nouvelles batteries → page
Pour chaque `batteries/<slug>/resultats.json` dont `batterie` diffère du doc `batteries/<slug>` de la page :
1. `python page_sync.py vignettes <slug>` → envoyer chaque liste avec `Artifact` (publish, url de la page,
   `asset: true`, `file_paths`). Noter « <id annonce> <id fichier> » (nom du fichier = id annonce) dans un fichier.
2. `python page_sync.py annonces <slug> <dossier> <fichier>` → `<dossier>/lots.json`.
3. `ArtifactData batch` pour chaque lot (un document qui existe déjà demande `if_version` : le lire d'abord).
4. Supprimer les docs `annonces` de ce slug qui ne sont plus dans la batterie (ancienne batterie).
Les réponses (`verdicts`) ne sont jamais supprimées.

## 3. Compte rendu
Une ou deux phrases : réponses envoyées (combien, quel modèle), batteries chargées.
