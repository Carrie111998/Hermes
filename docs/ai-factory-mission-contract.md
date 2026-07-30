# AI Factory — Contrat de mission des codeurs autonomes

Process layer (inspiré de Finn-loop) posé sur le moteur mécanique Hermes
(admission fail-closed, owner exact, freshness gate, statut productif).
Les agents exécutent; l'humain spécifie, juge et fusionne.

## 1. Contrat d'issue Linear (obligatoire avant tout dispatch)

Une issue n'est éligible au dispatch que si elle porte le label `agent-ready`
ET contient les deux blocs suivants dans sa description :

```markdown
## Critères d'acceptation
- AC-1: <comportement observable, vérifiable par un test ou une commande>
- AC-2: ...

## Non-objectifs
- NG-1: <ce que la mission ne doit PAS toucher — module, config, infra>
- NG-2: ...
```

Règles :
- **1 issue = 1 worker = 1 worktree = 1 branche = 1 carte kanban** (INV-1).
- Une mission tient en une journée de worker; sinon, découper l'issue.
- Les dépendances (`bloqué par HER-N`) sont déclarées dans Linear **et**
  reflétées en lien parent kanban à la création de la carte — le freshness
  gate de `factory_lane` refuse un claim sans évidence de fraîcheur, mais la
  déclaration du lien reste une responsabilité du contrôleur `default`.
- Réparer l'infrastructure de la factory est TOUJOURS un non-objectif
  implicite : un worker qui rencontre un problème d'infra **bloque sa carte
  avec preuve** au lieu de dériver (pattern Code B, 2026-07-30 — le bon
  comportement).

## 2. Labels-sémaphores Linear

| Label | Sens | Posé par |
|---|---|---|
| `agent-ready` | Contrat AC/NG complet, dispatchable | Jean (ou `/finn-spec`-équivalent) |
| `agent-building` | Carte kanban active, owner claimé | contrôleur |
| `loop-approved` | Revue verte sur SHA exact | reviewer (délégation lecture-seule) |
| `loop-changes-requested` | Revue rouge, ≤ 2 itérations | reviewer |
| `loop-stuck` | 2 blocages/échecs — escalade humaine | contrôleur (failure_limit=2) |
| `needs-human-review` | Ambiguïté de contrat ou risque détecté | worker ou reviewer |

## 3. Cycle d'une mission

1. **Sélection** : `default` prend l'issue `agent-ready` la plus prioritaire
   dont les dépendances sont Done.
2. **Admission** : évidence de fraîcheur (`current|superseded|duplicate|
   needs-rebase`, ≤ 24 h, SHA canonique) → carte kanban → claim owner exact →
   spawn gated. Tout échec = pas de worker, carte routée, raison visible.
3. **Build** : le worker implémente STRICTEMENT les AC, respecte les NG,
   exécute les tests ciblés + voisins avant chaque commit
   (`scripts/run_tests.sh`), committe dans SON worktree uniquement.
4. **Revue** : délégation lecture-seule sur le SHA exact; verdict trois états.
   Après 2 `loop-changes-requested` → `loop-stuck`, STOP (budget incident :
   1 implémenteur + 1 revue).
5. **Clôture** : handoff capturé, owner libéré, writeback Linear (HER-99),
   **fusion par Jean uniquement** — jamais par un agent.

## 4. Suivi (où en sont-ils ?)

- `hermes kanban list` / `show <carte>` — état canonique.
- Dashboard `GET /workers/active` — chaque worker est qualifié
  `spawned`/`alive`/`heartbeat_fresh`/`productive`. **Un PID seul n'est
  jamais « en train de travailler ».**
- Notifications Telegram : événements terminaux uniquement (completed,
  blocked, crashed…) — le silence pendant un run est normal, le board fait foi.
- La conversation Telegram d'un profil Code n'est PAS son worker : toute
  question « où en es-tu » se pose au board, pas au chat.

## 5. Anti-régression (verrous mécaniques + process)

- Hook d'admission fail-closed : mutation hors worktree owné impossible.
- Base de travail : lignée runtime à jour obligatoire (freshness gate).
- Tests avant commit (worker) + revue exact-SHA (reviewer) + fusion humaine.
- Déploiement runtime = `git` uniquement (fast-forward + restart);
  **jamais de copie manuelle de scripts** — le drift copie/source est la
  cause racine de la panne du 2026-07-30.
- Le canari hermétique `tests/hermes_cli/test_kanban_code_ab_canary.py`
  fait partie de la suite : toute régression d'admission casse un test
  avant de casser un run live.
