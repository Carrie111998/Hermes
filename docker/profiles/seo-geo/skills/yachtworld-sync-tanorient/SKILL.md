---
name: yachtworld-sync-tanorient
description: "Sync hebdomadaire du miroir des annonces brokerage YachtWorld vers tanorient.com (post 1418, bloc « Yachts for sale » uniquement) : inventaire via parse.bot REST, diff, mise à jour cartes HTML + ItemList JSON-LD, upload des photos sur WordPress. Déclencher pour : sync YachtWorld, synchroniser les annonces, mettre à jour la page brokerage, miroir des listings."
version: 1.0.0
author: WMH Project
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [yachtworld, sync, wordpress, tanorient, weekly, seo-geo]
---

# yachtworld-sync-tanorient

Synchronise le miroir des annonces YachtWorld de Tan Services sur la page
**Brokerage Service** (post **1418**). Portage serveur de l'ancienne tâche
Claude Desktop — sans navigateur : parse.bot passe en **REST**, et il n'y a
**ni fallback scraping ni purge WP Rocket** (voir §5-6).

## 1. Inventaire — parse.bot en REST

```bash
curl -s -X GET "https://api.parse.bot/scraper/242b2dc6-c896-4c4c-bef5-3db255d870a9/get_broker_listings?owner_id=11743&limit=20" \
  -H "X-API-Key: $PARSEBOT_API_KEY"
```

- Ignore le tableau `sponsored` (annonces d'autres brokers).
- Par record : `make`, `model`, `year`, `class`
  (sail-trimaran→Trimaran, sail-catamaran→Catamaran, sail-cruiser→Monohull,
  sail-antique→Classic), `location.address.city`+`country`,
  `price.type.amount.USD` (arrondi au millier), `attributes` (IN_STOCK),
  `portalLink`, première entrée `media` avec `mediaType:"image"`.
- 🔴 **Vérifie `count`** : troncature récurrente observée (3 renvoyés sur 7).
  Si le nombre de records ≠ `count`, retente avec pagination/offset. Si
  l'inventaire reste incomplet : **NE MODIFIE RIEN** — rapporte l'échec
  (« inventaire parse.bot incomplet : X/Y ») et termine. Pas de fallback
  navigateur ici ; mieux vaut une page inchangée qu'un miroir amputé.

## 2. État actuel — post 1418

`wp_get_post` (MCP `wp-tanservices`), `content_format: full`.

🔴 Le post contient **trois** blocs `wp:html` : intro pilier / **listings
YachtWorld** / long-form + FAQPage. **Seul le bloc listings est à toucher** —
identifie-le par son `h2` « Yachts for sale » et son schema `ItemList`.
Les deux autres blocs doivent ressortir **octet pour octet identiques**.

## 3. Diff & mise à jour

Différence = annonce ajoutée/retirée, ou prix modifié de ±1 % ou plus.

- **Aucun changement** → ne modifie rien, rapporte « inventaire inchangé (N annonces) ».
- Sinon, mets à jour **dans le même `wp_update_post`** :
  - les cartes HTML, au format existant exact : div grid, img aspect-ratio
    16/10 **hébergée sur tanorient.com**, span `catégorie · lieu · In stock`,
    h3 `Modèle (année)`, `≈ US$ prix`, liens View details (portalLink) +
    Enquire ;
  - le JSON-LD `ItemList` (schema `Product` : image, offers USD,
    `seller.@id: https://tanorient.com/#boatdealer`) — cohérent avec les cartes ;
  - la ligne « Listings updated [date du jour] ».
- **Nouvelle annonce** : téléverse d'abord la photo via `wp_upload_media`
  (alt : `"Modèle (année) for sale — Tan Services brokerage"`) et utilise
  l'URL tanorient.com résultante — **jamais** d'URL boatsgroup.com.
- ⚠️ N'utilise pas `wp_alter_post` en regex sur ce post (backreference `$1`
  cassée, blocs ACF fragiles) : reconstruis le bloc listings complet et passe
  par `wp_update_post`.

## 4. Rappels de règles (manuel tan-seo)

Aucune mention « Tan Orient », NEEL ou Fountaine Pajot ; aucun
`aggregateRating`/`review` ajouté au schema.

## 5. Vérification (sans purge possible)

Pas de navigateur → pas de purge WP Rocket. Vérifie le rendu réel :

```bash
curl -s "https://tanorient.com/brokerage-service/?nocache=$(date +%s)"
```

- Compte les cartes et vérifie la ligne « Listings updated ». Teste aussi
  `https://tanorient.com/brokerage-service?nocache=…` (variante sans slash —
  cache distinct, piège connu).
- Si la version **cachée** (URL nue, sans paramètre) est périmée, c'est
  attendu (TTL WP Rocket) : **signale-le** dans le rapport (« cache public à
  purger manuellement ») au lieu de retenter en boucle.

## 6. Rapport

Fin de tâche (sortie du cron, livrée sur Discord) : annonces ajoutées /
retirées / prix modifiés, nombre total, statut de la vérification en prod, et
tout blocage (parse.bot incomplet, cache périmé). Consigne l'intervention dans
`/opt/data/wiki/tan-seo/journal.md` **seulement** s'il y a eu modification du
post (pull-rebase avant, commit/push après).
