---
name: magnific
description: "Magnific AI: upscale et enhance d'images via l'API Magnific (api.magnific.com). Utiliser pour agrandir/améliorer des visuels clients (hero, photos événement, assets maquette) avant intégration web."
version: 1.0.0
author: System
license: MIT
platforms: [linux, macos, windows]
required_credential_files: []
tags: [design, images, upscale, magnific, ai]
---

# Magnific AI — Upscaler API

Upscale/enhance d'images par IA. À utiliser quand un asset source est trop
petit ou trop dégradé pour la maquette (hero, photos événement, logos raster).

## Credentials (vars Railway, déjà injectées dans l'environnement)
- `MAGNIFIC_API_KEY` — clé API (header `x-magnific-api-key`)
- `MAGNIFIC_WEBHOOK_SECRET` — signing secret webhook. **Inutilisé pour l'instant** :
  aucun récepteur HTTP n'est exposé côté Railway, donc ne pas passer de
  `webhook_url` — récupérer les résultats en pollant le `task_id`.

Ne jamais afficher ces valeurs en clair ; référence-les via `$MAGNIFIC_API_KEY`.

## Usage — passer par le script

Utiliser `scripts/upscale.py`, qui gère tout le cycle (POST + polling +
téléchargement immédiat des URLs CDN signées, qui expirent) :

```bash
python3 scripts/upscale.py SOURCE -o out.png --scale 2x \
  [--mode creative|precision] [--engine automatic|magnific_illusio|magnific_sharpy|magnific_sparkle] \
  [--optimized-for STYLE] [--prompt TXT] [--creativity N] [--hdr N] \
  [--resemblance N] [--fractality N]
```

Points API à connaître (si appel manuel) :
- `image` n'accepte **que du base64** (pas d'URL — le script télécharge puis encode).
- Deux endpoints : `/v1/ai/image-upscaler` (creative) et
  `/v1/ai/image-upscaler-precision` (precision).
- Réponse enveloppée dans `data` : `{"data": {"task_id", "status", ...}}` ;
  statuts `CREATED` → `IN_PROGRESS` → `COMPLETED`/failed ; résultat dans
  `generated[]` (URLs signées à télécharger tout de suite).
- Limite de sortie **25,3 MP** : baisser `--scale` ou recadrer la source sinon.

Référence complète : https://docs.magnific.com

## Workflow recommandé
1. Optimiser/recadrer l'image source d'abord (cf. persona : sips/ImageMagick).
2. Upscaler via Magnific si la résolution reste insuffisante pour la maquette.
3. Re-compresser le résultat (cwebp) avant intégration — ne jamais servir le
   PNG brut upscalé.
