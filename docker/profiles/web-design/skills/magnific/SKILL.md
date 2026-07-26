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

## Usage

L'endpoint est **asynchrone** : le POST retourne un `task_id`, puis on polle le
statut jusqu'à `completed`.

```bash
curl -s -X POST "https://api.magnific.com/v1/ai/image-upscaler" \
  -H "x-magnific-api-key: $MAGNIFIC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "<URL ou base64 de l image source>"
  }'
```

Réponse : `{"task_id": "..."}` — poller le statut avec le même header
(toutes les 5–10 s, l'upscale prend généralement < 1 min) :

```bash
curl -s "https://api.magnific.com/v1/ai/image-upscaler/<task_id>" \
  -H "x-magnific-api-key: $MAGNIFIC_API_KEY"
```

Référence complète (paramètres scale/creativity/engine, schémas de réponse) :
https://docs.magnific.com

## Workflow recommandé
1. Optimiser/recadrer l'image source d'abord (cf. persona : sips/ImageMagick).
2. Upscaler via Magnific si la résolution reste insuffisante pour la maquette.
3. Re-compresser le résultat (cwebp) avant intégration — ne jamais servir le
   PNG brut upscalé.
