---
name: firebase-deploy
description: "Déployer un site web sur Firebase Hosting et générer une URL de preview (preview channel) pour faire relire le travail, via le compte de service partagé — pas de login navigateur ni de token CI. Déclencher pour : déployer sur Firebase, publier une preview, 'voir le travail', hosting preview, firebase deploy."
version: 1.0.0
author: WMH Project
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [firebase, hosting, deploy, preview, service-account, web-dev]
---

# firebase-deploy

Déploie des sites statiques / builds front sur **Firebase Hosting** et produit des
**URLs de preview** pour « voir le travail » avant mise en prod.

## Auth (déjà configurée)
- CLI `firebase` installé dans l'image (sur le PATH).
- Auth **non-interactive** via compte de service : `GOOGLE_APPLICATION_CREDENTIALS`
  est déjà exporté vers `$HERMES_HOME/firebase-service-account.json` (projet
  `pippabot-reports-e0e67`) par l'entrypoint. Aucun `firebase login` requis.
- Vérifier : `firebase projects:list`.

## ⚠️ Garde-fou — ne pas écraser les rapports CRON
Le **channel live** du projet `pippabot-reports-e0e67` héberge les rapports CRON du
profil principal (via `/opt/data/firebase-public`, motif merge→deploy→sync-back).
Un `firebase deploy` complet vers ce live channel **effacerait** ces fichiers.
Pour le travail web-dev, **utiliser les preview channels** (isolés, sans toucher au
live) — ou un **projet Firebase dédié** au web-dev.

## Preview channel — le cas « voir le travail » (recommandé)
Crée une URL temporaire isolée, sans impacter la prod :
```bash
cd <dossier-du-build>   # doit contenir firebase.json (+ .firebaserc ou --project)
firebase hosting:channel:deploy preview-<nom> \
  --project pippabot-reports-e0e67 \
  --expires 7d
```
→ renvoie une URL `https://pippabot-reports-e0e67--preview-<nom>-xxxx.web.app` à
partager. Lister / supprimer : `firebase hosting:channel:list` /
`firebase hosting:channel:delete preview-<nom>`.

## Config minimale d'un projet
`firebase.json` :
```json
{ "hosting": { "public": "dist", "ignore": ["firebase.json", "**/.*", "**/node_modules/**"] } }
```
(`public` = dossier du build : `dist` pour Vite, `out`/export pour Next static, `.`
pour un dossier HTML simple.) Le projet est passé via `--project` ou un `.firebaserc`.

## Déploiement live (à réserver à un projet dédié)
```bash
firebase deploy --only hosting --project <projet-dédié>
```
Ne cible **jamais** le live de `pippabot-reports-e0e67` depuis web-dev.

## Rappels
- Sortie attendue quand on te demande de « voir le travail » : un **preview channel**
  + l'URL renvoyée à l'utilisateur.
- Pour un nouveau projet client, préfère créer/《utiliser》un **projet Firebase dédié**
  (préviens l'utilisateur) plutôt que de mutualiser avec les rapports CRON.
