---
name: claude-code
description: "Déléguer une tâche de code lourde (refactoring multi-fichiers, feature complète, migration, debug profond d'un repo) au CLI Claude Code en mode headless — auth déjà configurée via ANTHROPIC_API_KEY. Déclencher pour : grosse tâche de code, refactoring d'un repo, implémenter une feature complète, 'utilise claude code', corriger une suite de tests, migration de code."
version: 1.0.0
author: WMH Project
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [claude-code, coding, delegate, headless, web-dev]
---

# claude-code

Le CLI **Claude Code** est installé dans l'image (sur le PATH : `claude`). Il sert de
**délégué de code** : pour une tâche substantielle sur un repo (refactoring
multi-fichiers, feature complète, migration, debug d'une suite de tests), le lancer
en mode headless est souvent plus efficace que d'éditer fichier par fichier soi-même.

## Auth (déjà configurée)
- Non-interactive via `ANTHROPIC_API_KEY` (variable Railway, visible dans le
  container). Aucun `claude login` requis — et impossible en headless de toute façon.
- Config/état persistés sur le volume : `CLAUDE_CONFIG_DIR=/opt/data/claude-code`
  (exporté par l'entrypoint).
- Vérifier : `claude --version` puis `claude -p "réponds ok"`.
- ⚠️ Facturation à l'usage sur la clé API : réserver aux tâches qui le justifient,
  pas aux petites éditions (fais-les toi-même).

## Usage headless — le motif standard
Toujours `-p` (print mode, non-interactif) depuis la racine du repo concerné :

```bash
cd <repo>
claude -p "Décris la tâche précisément : contexte, fichiers concernés, critères de réussite (tests qui doivent passer, build qui doit compiler)." \
  --dangerously-skip-permissions \
  --output-format text
```

- `--dangerously-skip-permissions` : indispensable en headless (aucun humain pour
  approuver les prompts de permission). Acceptable ici car le container est déjà
  le bac à sable. **Ne jamais** l'utiliser dans un dossier contenant des données
  de prod partagées (ex. `/opt/data/firebase-public`) — travailler dans un clone
  ou un dossier de travail dédié.
- Alternative plus fine si la tâche est en lecture/édition simple :
  `--permission-mode acceptEdits` (les commandes bash restent gate-ées).

## Bien prompter le délégué
- Donner **le contexte et le critère de réussite vérifiable** ("les tests
  `npm test` doivent passer", "le build `npm run build` doit sortir sans erreur").
- Une tâche = un appel. Pour un gros chantier, découper en étapes et enchaîner
  les appels `claude -p`, en vérifiant entre chaque.
- Après l'appel : **vérifier le résultat toi-même** (relire le diff `git diff`,
  lancer les tests/build) avant de commiter ou déployer. Tu restes responsable
  du livrable.

## Rappels
- Repo git : travailler sur une branche, commiter après vérification — pas de
  push sans que ce soit le flux demandé.
- Sessions longues : `claude -p` peut prendre plusieurs minutes sur une grosse
  tâche ; c'est normal, ne pas l'interrompre prématurément.
- Modèle par défaut du CLI = le plus récent disponible sur la clé ; pas besoin
  de le forcer sauf demande explicite.
