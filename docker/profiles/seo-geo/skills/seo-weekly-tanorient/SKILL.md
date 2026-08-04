---
name: seo-weekly-tanorient
description: "Routine hebdomadaire de suivi SEO de tanorient.com : Search Console (performances 7j, requêtes non-brand, indexation), Matomo via API (segment Trafic fiable, 4 objectifs), contrôle de la sync YachtWorld du lundi, rapport Discord comparé à la semaine précédente. Déclencher pour : routine hebdo SEO, point SEO tanorient, rapport Search Console, chiffres Matomo, suivi indexation, 'fais le point SEO de la semaine'."
version: 1.0.0
author: WMH Project
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [seo, gsc, tanorient, weekly, reporting, seo-geo]
---

# seo-weekly-tanorient

Routine hebdo de suivi SEO de **tanorient.com**. Avant de commencer :
`git -C /opt/data/wiki pull --rebase`, puis lis `tan-seo/manuel.md` (§ règles)
et la dernière entrée hebdo de `tan-seo/journal.md` (point de comparaison).

## 1. Search Console — Performances 7 jours (MCP `gsc`)

Propriété : `https://tanorient.com/` (préfixe). Fenêtre : 7 derniers jours
complets, comparés aux 7 précédents.

- Totaux : clics, impressions, CTR, position moyenne.
- Top requêtes : sépare **brand** (tan services, tanorient…) et **non-brand**
  (sailing yacht brokerage uae, lagoon dealer dubai…). **L'indicateur qui
  compte : l'apparition/progression de requêtes non-brand.** Tout le reste est
  du trafic de marque.
- Top pages : note tout mouvement sur `/brokerage-service/`,
  `/sailing-yachts-for-sale-uae/`, `/yacht-refit-and-repair-uae/` et les
  fiches `boat`.

## 2. Search Console — Indexation

- Compteur « explorée, actuellement non indexée » : baisse-t-il ?
  ⚠️ Une partie de ces pages sont les archives `/brand/*` que **nous** avons
  volontairement passées en `noindex` : leur sortie du compteur n'est **pas**
  un succès. Ne compter que les fiches `boat`.
- Vérifie par inspection d'URL l'état des 2 fiches pilotes (Lagoon 46 : post
  936, Dragonfly 36 : post 420) si le pilote éditorial est publié.
- Cas connu **volontairement non corrigé** : « Il faut indiquer offers, review
  ou aggregateRating » sur les extraits produits → ne pas s'alarmer, ne pas
  corriger (remède prévu : `Product` → `ProductModel` avec le pilote).

## 3. Matomo — API de reporting (segment « Trafic fiable » OBLIGATOIRE)

Site **78**, token `$MATOMO_TRACKING_TOKEN` (VAR Railway). 🔴 Tout chiffre
rapporté passe par le **segment 51** — sans lui, ~24 % de bots Linux faussent
tout. La définition stockée contient un double-encodage : la passer **via
`--data-urlencode`, telle quelle** (vérifié 04/08 ; toute variante renvoie `[]`) :

```bash
SEG='segment=operatingSystemName!=GNU%252FLinux,actions>1'
M() { curl -s -X POST 'https://wmhproject.matomo.cloud/index.php' \
  -d "module=API&method=$1&idSite=78&period=week&date=last2&format=json" \
  --data-urlencode "$SEG" -d "token_auth=$MATOMO_TRACKING_TOKEN"; }
M VisitsSummary.get          # visites, actions, durée, rebond (S-1 + S en cours)
M Goals.get                  # conversions globales ; par objectif : ajouter &idGoal=1..4
M Referrers.getReferrerType  # répartition sources (organique, direct, sites, social)
```

- Objectifs : #1 consultation brokerage · #2 clic annonce YachtWorld · #3
  formulaire envoyé · #4 clic tel/WhatsApp/email.
- ⚠️ Pré-archivage différé sur Matomo Cloud : la **semaine en cours** peut être
  vide ou partielle — le rapport compare la **dernière semaine complète** à la
  précédente (`date=last2`, ignorer la tranche courante si < 7 jours).
- ⚠️ Le module API en session UI reste bloqué ; seul ce chemin token
  fonctionne. Si l'API renvoie une page d'erreur HTML, le signaler (token
  révoqué ?) et marquer la section « à consulter manuellement ».

## 4. Contrôle de la sync YachtWorld

- Lis le dernier output du cron `yw-sync` (`~/cron/output/<job_id>/`, le plus
  récent) : a-t-elle tourné lundi ? statut ?
- Contre-vérifie en prod : `curl -s "https://tanorient.com/brokerage-service/?nocache=$(date +%s)"`
  → la ligne « Listings updated … » et le nombre de cartes doivent correspondre
  au rapport de sync. Teste aussi la variante sans slash final.

## 5. Rapport & journal

- **Rapport Discord compact** (c'est la sortie du cron) : 8-12 lignes max —
  clics/impressions/position vs S-1, requêtes non-brand nouvelles, compteur
  indexation, visites fiables + conversions Matomo vs S-1, statut sync YW,
  et la ligne fixe : « GBP : à consulter manuellement (hors périmètre serveur). »
- Toute anomalie (chute brutale, page désindexée, sync en échec) : détaille-la
  et propose l'action — mais **n'applique aucun correctif de contenu** sans
  validation.
- Consigne dans `tan-seo/journal.md` (`## Fait (JJ/MM/AAAA) — hebdo SEO` +
  tableau des chiffres), puis `git add/commit/push`.
