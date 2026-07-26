---
name: magnific
description: "Magnific AI via l'API REST api.magnific.com : génération d'images (Mystic, nano-banana-pro, Flux, Seedream), upscale (creative/precision), édition (relight, style transfer, remove background, expand, icônes), vidéo, audio et analytics de crédits. Utiliser pour produire ou améliorer des visuels clients (hero, affiches, photos événement, assets maquette) avant intégration web."
version: 3.0.0
author: System
license: MIT
platforms: [linux, macos, windows]
required_credential_files: []
tags: [design, images, upscale, generation, magnific, nano-banana, ai]
---

# Magnific AI — API REST

Génération et amélioration de visuels par IA. Trois usages ici : **générer** un
asset (Mystic ou Nano Banana Pro), **upscaler** un asset trop petit, **auditer**
la consommation de crédits.

**Chaque appel consomme des crédits.** Ne jamais lancer un appel « pour voir » :
vérifier les paramètres avant de POSTer, et auditer après (`scripts/credits.py`).

## Credentials

- `MAGNIFIC_API_KEY` — clé API, header `x-magnific-api-key`. **Vérifiée valide.**
- `MAGNIFIC_WEBHOOK_SECRET` — signing secret webhook. **Absent de l'environnement
  de l'agent** (défini côté Railway pour le récepteur HTTP externe). Sans
  récepteur exposé ici, ne pas passer `webhook_url` — poller le `task_id`.

Ne jamais afficher ces valeurs en clair ; référencer via `$MAGNIFIC_API_KEY`.

Vérifier l'auth sans rien dépenser :

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://api.magnific.com/v1/ai/image-upscaler/00000000-0000-0000-0000-000000000000" \
  -H "x-magnific-api-key: $MAGNIFIC_API_KEY"
# 404 = clé valide  |  401 = clé absente/invalide
```

## Quel modèle choisir — l'arbre de décision

**Y a-t-il du texte à composer dans l'image ?** (titre, affiche, capture d'UI,
infographie, packaging avec mentions)

- **OUI → `nano-banana-pro`.** Seul modèle qui rend du texte réellement lisible.
- **NON → Mystic.** Meilleur contrôle artistique (`structure_reference`,
  `style_reference`), et le seul à proposer `editorial_portraits`.

Test réalisé sur le même prompt (titre « TREMPLINS 2027 » + sous-titre) :

| | nano-banana-pro | Mystic `flexible` |
|---|---|---|
| Titre | exact, sur une ligne | **cassé en deux lignes**, mise en page ignorée |
| Sous-titre | exact, accents et trait d'union corrects | correct |
| Parasites | aucun | **fausse URL inventée en pied de page** |
| Sortie | PNG | JPEG |

Le pseudo-texte est une limite structurelle des modèles de diffusion (Mystic,
Flux, Seedream) — aucun réglage ne la contourne.

**Puis, si la résolution est insuffisante** → upscaler (Precision par défaut).

## Scripts (voie recommandée)

Tous gèrent POST + polling + téléchargement immédiat, et refusent les
combinaisons invalides **avant** de dépenser un crédit.

```bash
# Texte dans l'image (affiche, titre, UI)
python3 scripts/nano_banana.py "PROMPT" -o affiche.png --aspect 3:4 --resolution 1K

# Photo / illustration / portrait
python3 scripts/generate.py "PROMPT" -o hero --resolution 2k \
        --aspect widescreen_16_9 --model realism

# Upscale
python3 scripts/upscale.py hero.png -o hero@2x.png --scale 2x
python3 scripts/upscale.py logo.png --mode precision --scale 4x -o logo@4x.png

# Audit de conso (aucun crédit)
python3 scripts/credits.py
python3 scripts/credits.py --by-model
python3 scripts/credits.py --keys
```

`--help` sur chacun liste tous les paramètres.

## Nano Banana Pro (Google)

`POST /v1/ai/text-to-image/nano-banana-pro` — **route absente de `llms.txt`**
mais bien active (page de doc : `/api-reference/text-to-image/post-nano-banana-pro`).
Sortie **PNG**.

| Champ | Défaut | Valeurs |
|---|---|---|
| `prompt` **(requis)** | — | 2 à 3000 caractères |
| `aspect_ratio` | `1:1` | `1:1` `2:3` `3:2` `4:3` `3:4` `5:4` `4:5` `16:9` `9:16` `21:9` |
| `resolution` | `2K` | `1K` `2K` `4K` (aussi `low`/`medium`/`high`) |
| `reference_images` | — | max **14**, objets `{image, mime_type, text}` |
| `webhook_url` | — | ne pas utiliser ici |

**Attention aux deux contrats opposés :**
- `aspect_ratio` en notation `16:9` — Mystic utilise `widescreen_16_9`.
- `reference_images` exige des **URL publiques** (ou GCS) — Mystic exige du
  **base64**. Inverser les deux est l'erreur la plus facile à commettre.

### Modèles Google accessibles en REST

Seul `nano-banana-pro` répond. Testés et **404 en REST** : `nano-banana`,
`nano-banana-2`, `nano-banana-2-lite`, `gemini-3-pro-image`, `imagen-3`,
`imagen-4`, `gpt-2`, `qwen`, `ideogram-3`. Ces modèles existent au catalogue
(table de crédits de l'app web) mais ne sont exposés que dans l'app et via MCP.

## Génération d'images — Mystic

`POST /v1/ai/mystic`. Sortie **JPEG**.

| Champ | Défaut | Valeurs |
|---|---|---|
| `prompt` | — | texte libre ; supporte `@personnage` et `@personnage::force` |
| `resolution` | `2k` | `1k` `2k` `4k` |
| `aspect_ratio` | `square_1_1` | `square_1_1` `classic_4_3` `traditional_3_4` `widescreen_16_9` `social_story_9_16` `smartphone_horizontal_20_9` `smartphone_vertical_9_20` `standard_3_2` `portrait_2_3` `horizontal_2_1` `vertical_1_2` `social_5_4` `social_post_4_5` |
| `model` | `realism` | `realism` `fluid` `zen` `flexible` `super_real` `editorial_portraits` |
| `engine` | `automatic` | `automatic` `illusio` `sharpy` `sparkle` |
| `creative_detailing` | `33` | `[0, 100]` — plus haut = plus de détail mais look « HDR/IA » |
| `adherence` / `hdr` | — | `[0, 100]` |
| `structure_reference` | — | **base64** : impose la **forme** (colorier un croquis, texturer un 3D) |
| `structure_strength` | `50` | `[0, 100]` — n'agit qu'avec `structure_reference` |
| `style_reference` | — | **base64** : impose le **style** |
| `filter_nsfw` | `false` | booléen |

### Choisir le modèle Mystic

- `realism` — palette réaliste, « moins d'effet IA ». Défaut pour photo produit
  et lifestyle. Mauvais sur le fantastique ou les personnages connus.
- `super_real` — priorité au réalisme, excellent en plan moyen.
- `editorial_portraits` — portraits serrés, état de l'art. Artefacts anatomiques
  en plan large. Aime les prompts très longs.
- `zen` — rendu doux, épuré, peu d'objets. Bon pour des fonds discrets.
- `flexible` — bonne adhérence au prompt, rendu saturé/HDR. Idéal illustration.
- `fluid` — meilleure adhérence globale (tourne sur **Google Imagen 3**), mais
  **sur-modéré** : des mots anodins comme « war » peuvent être refusés. N'accepte
  que 5 ratios : `square_1_1` `social_story_9_16` `widescreen_16_9`
  `traditional_3_4` `classic_4_3`.

### Autres modèles de génération en REST

Convention `/v1/ai/text-to-image/<modèle>` : `flux-2-pro`, `flux-2-turbo`,
`flux-2-klein`, `flux-kontext-pro`, `flux-pro-v1-1`, `flux-dev`, `hyperflux`,
`seedream-4`, `seedream-v4-5`, `z-image-turbo`, `runway`. Utiles pour de
l'image-to-image ou de la génération sub-seconde.

## Upscale

| Mode | Endpoint | Pour quoi |
|---|---|---|
| Creative | `POST /v1/ai/image-upscaler` | concept art, créas marketing ; **peut inventer du détail** |
| Precision | `POST /v1/ai/image-upscaler-precision` | logos, UI, packshots, texte, scans ; zéro hallucination |

| Champ | Défaut | Valeurs |
|---|---|---|
| `image` **(requis)** | — | **base64 nu** du fichier |
| `scale_factor` | `2x` | `2x` `4x` `8x` `16x` |
| `optimized_for` | `standard` | `standard` `soft_portraits` `hard_portraits` `art_n_illustration` `videogame_assets` `nature_n_landscapes` `films_n_photography` `3d_renders` `science_fiction_n_horror` |
| `engine` | `automatic` | `automatic` `magnific_illusio` `magnific_sharpy` `magnific_sparkle` |
| `prompt` | — | guide l'upscale (mode creative) |
| `creativity` / `hdr` / `resemblance` / `fractality` | `0` | entiers `[-10, 10]` |

**Precision par défaut** dès que la fidélité compte. Creative jamais sur un
asset de marque.

## Contrat commun

Base `https://api.magnific.com/v1`, asynchrone, réponse enveloppée dans `data` :

```json
{"data": {"task_id": "046b6c7f-...", "status": "CREATED", "error": null, "generated": []}}
```

Statuts : `CREATED` → `IN_PROGRESS` → `COMPLETED` | `FAILED`.
Résultat dans `data.generated[]` (URLs CDN signées).

**Sonder une route sans dépenser** : `POST` avec `{}` → `400 Validation error`
si la route existe, `404 Not found` sinon. Un `GET` sur la collection n'est pas
fiable (404 sur les routes POST-only comme Flux).

## Pièges (tous constatés en test réel)

1. **`image` refuse les URL** (upscale/Mystic) : `400 "Unable to resolve image
   from <url>"` même sur un CDN public. C'est du **base64 nu**, sans préfixe
   `data:`. Mais `reference_images` de Nano Banana Pro exige l'**inverse** : des
   URL publiques. Ne pas confondre.
2. **Les URLs CDN de sortie expirent** (`?token=exp=...~hmac=...`). Télécharger
   immédiatement ; ne jamais coller dans une maquette ou un livrable.
3. **Formats de sortie différents** : Mystic → JPEG, Nano Banana Pro → PNG,
   upscale Precision → PNG. Ne pas nommer le fichier en aveugle.
4. **Limite dure de 25,3 MP en sortie d'upscale** (`l × facteur × h × facteur`).
   Un 1920×1080 en 8x = 132 MP → rejeté. `upscale.py` bloque avant l'appel.
5. **Combinaisons invalides silencieusement ignorées.** La doc l'écrit :
   « The API will not return errors for incompatible combinations ». Un
   `aspect_ratio` non supporté par `fluid`, ou des LoRAs avec `style_reference`,
   partent quand même — crédit consommé pour un résultat inattendu.
6. **Le coût suit l'aire de sortie, pas le facteur.** Un `2x` sur une grande
   source coûte autant qu'un `8x` sur une petite.
7. **`llms.txt` est incomplet.** Des routes actives en sont absentes
   (`nano-banana-pro`, `text-to-icon`). Ne jamais conclure « pas disponible »
   depuis l'index seul : sonder la route.
8. **Durées observées** : Mystic 1k ≈ 5–10 s ; Nano Banana Pro 1K ≈ 15 s ;
   upscale 2x ≈ 5 s ; upscale 16x (19 MP) ≈ 60–90 s.

## Coût et audit

Facturation **à crédits**, déduits à chaque appel. Les allocations
« Unlimited » d'un abonnement ne couvrent **que l'app web** — l'API tourne
toujours au crédit.

Repères (table officielle de l'app) : Mystic 2.5 **50**, avec style reference
**100** ; Mystic 2.5 Flexible/Fluid **80** ; Nano Banana Pro **75 / 75 / 150**
(1K/2K/4K) ; Flux.1 Fast **5** ; Z-Image **5** ; Seedream 4 **50** ;
GPT 2 High 4K **2100**.

Upscale (à l'aire de sortie) : 2x sur 640×480 = 0,10 € ; 4x = 0,20 € ;
8x = 0,50 € ; 2x sur 1920×1080 = 0,20 €.

**Auditer la conso réelle** — `scripts/credits.py`, via l'Analytics API
(`POST /v1/analytics/team-credit-usage`, `GET /v1/analytics/team-api-keys`).
Donne le détail par jour, outil, utilisateur et projet. Aucun crédit consommé.
À lancer après toute série de tests.

## Workflow recommandé

1. **Choisir le modèle** via l'arbre de décision (texte → Nano Banana Pro).
2. Recadrer/nettoyer la source avant upscale (ImageMagick), **sans la
   redimensionner** — downscaler avant upscale dégrade fortement le résultat.
3. **Upscaler** en Precision si la fidélité compte. Facteur modéré (2x–4x),
   itérer plutôt que viser 8x/16x.
4. Re-compresser avant intégration (`cwebp -q 82`) — ne jamais servir le fichier
   brut (de quelques centaines de Ko à ~20 Mo en 16x).
5. Comparer à 100 % de zoom (netteté, couleurs, artefacts anatomiques).
6. `python3 scripts/credits.py` pour vérifier ce que la série a coûté.

## Autres endpoints REST (vérifiés actifs)

- **Édition** : `/ai/image-relight`, `/ai/image-style-transfer`,
  `/ai/beta/remove-background` (accepte `image_url` **ou** `image_file`),
  `/ai/image-expand`, Seedream 4.5 Edit.
- **Icônes** : `/ai/text-to-icon` (absent de `llms.txt`).
- **Audio** : `/ai/music-generation`, `/ai/sound-effects`, `/ai/audio-isolation`.
- **Vidéo** : Kling 2.1/2.5/2.6, MiniMax Hailuo, WAN 2.5/2.6, RunWay Gen4,
  LTX 2.0 Pro, Seedance Pro, PixVerse V5, OmniHuman 1.5, VFX.
- **Analytics** : `/analytics/team-credit-usage`, `team-api-keys`,
  `team-members`, `team-groups`, `team-projects`.
- **Stock** : images, icônes, vidéos. Plafonné à 100 téléchargements/jour hors
  Business/Enterprise.

## REST vs MCP — pourquoi on reste en REST

Magnific expose aussi un serveur MCP (`https://mcp.magnific.com`, streamable
HTTP). **Ne pas l'utiliser pour remplacer le REST ici** : il s'authentifie en
**OAuth 2.0 avec ouverture de navigateur**, sans client_credentials ni service
account. Incompatible avec un déploiement headless type Railway (pas de
navigateur, pas de clé statique, session liée à un compte personnel).

Le MCP apporte en plus : Nano Banana 2, Imagen 4, GPT 2, SVG (`images_generate_svg`,
`images_to_svg`), 3D (`models3d_generate`), entraînement de personnages
« Soul » (`custom_references_create`), historique et dossiers
(`creations_search`, `folders_*`, `spaces_*`).

Le REST apporte en plus : l'**Analytics API** (absente du MCP, qui n'a que
`account_balance`), les webhooks signés, et une clé statique déployable.

Les deux tapent dans le **même solde de crédits**. Bon partage : REST pour la
production automatisée, MCP en poste de travail pour l'exploration créative.
`images_models_list` côté MCP sert de catalogue dynamique.

## Webhooks

Si un récepteur est exposé, la vérification de signature (HMAC-SHA256 base64 sur
`{webhook-id}.{webhook-timestamp}.{raw_body}`) est documentée dans le skill
`third-party-api-integration` (`references/magnific-freepik-api.md` +
`scripts/verify_webhook_signature.py`). Le payload webhook est identique à la
réponse du GET **sans** le champ `data`.

Référence : https://docs.magnific.com — index (incomplet) :
https://docs.magnific.com/llms.txt — crédits par modèle :
https://www.magnific.com/ai/docs/ai-image-generator-credits
