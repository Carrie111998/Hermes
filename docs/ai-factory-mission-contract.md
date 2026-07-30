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

Créés sur la team HER par `scripts/linear_loop.py ensure-labels --apply`.

| Label | Sens | Posé par |
|---|---|---|
| `agent-ready` | Jean autorise un codeur à traiter l'issue | **Jean, à la main** |
| `agent-building` | Mission en cours | la boucle |
| `agent-blocked` | Le codeur attend une décision de Jean | la boucle |
| `agent-review` | Branche locale prête, en attente du GO de merge | la boucle |

`agent-ready` est le seul geste humain requis, et le seul filtre qui sépare une
issue codable d'une tâche business (signature de deal, relecture juridique,
achat média). Une issue sans ce label est invisible pour la boucle.

## 3. Cycle d'une mission

1. **Sélection** : la boucle (`scripts/linear_loop.py`, tick cron toutes les
   10 min) prend l'issue `agent-ready` la plus prioritaire — priorité Linear
   d'abord, puis ancienneté — pour chaque codeur libre. La sélection est
   **déterministe** : aucun modèle n'intervient dans le choix de l'issue, du
   codeur ou du moment. Le seul LLM du système est celui qui code.
2. **Admission** : évidence de fraîcheur (`current|superseded|duplicate|
   needs-rebase`, ≤ 24 h, SHA canonique) → carte kanban → claim owner exact →
   spawn gated. Tout échec = pas de worker, carte routée, raison visible.
3. **Build** : le worker implémente STRICTEMENT les AC, respecte les NG,
   exécute les tests ciblés + voisins avant chaque commit
   (`scripts/run_tests.sh`), committe dans SON worktree uniquement.
4. **Revue** : délégation lecture-seule sur le SHA exact; verdict trois états.
   Après 2 `loop-changes-requested` → `loop-stuck`, STOP (budget incident :
   1 implémenteur + 1 revue).
5. **Clôture** : au tick suivant, la boucle commente l'issue (branche, HEAD,
   nombre de commits, résumé du worker), la passe en `In Review` avec le label
   `agent-review`, archive la carte et envoie à Jean une demande de GO sur
   Telegram. **La fusion appartient à Jean** — le hook d'admission n'admet
   aucun `push`, `merge` ni PR, donc aucun agent ne peut fusionner, même par
   accident.

## 4. Suivi (où en sont-ils ?)

- **Linear est la seule interface nécessaire** : l'état d'une issue (`In
  Progress` + `agent-building`, puis `In Review` + `agent-review`) suffit à
  savoir où en est le travail. Le kanban est de la plomberie ; il n'y a aucune
  raison de l'ouvrir au quotidien.
- `hermes kanban list` / `show <carte>` — état canonique quand on veut le détail.
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

## 6. La boucle (câblage)

| Élément | Où |
|---|---|
| Code de la boucle | `scripts/linear_loop.py` (versionné) |
| Lanceur cron | `~/.hermes/scripts/linear_loop_tick.py` — inerte, n'évolue jamais |
| Job cron | « Boucle Linear → codeurs (HER) », `no_agent`, toutes les 10 min |
| Tests | `tests/test_linear_loop.py` — Linear simulé, aucune sortie réseau |

Commandes utiles :

```bash
python scripts/linear_loop.py status              # qui travaille, quoi est prêt
python scripts/linear_loop.py tick                # simulation : n'écrit rien
python scripts/linear_loop.py tick --apply        # un tick réel
python scripts/linear_loop.py ensure-labels --apply
```

Garde-fous de la boucle :

- **Sans `--apply`, rien n'est muté** — ni Linear, ni le kanban.
- **Un codeur libre = une mission.** Une carte `blocked` n'occupe pas un
  codeur (sinon un blocage le gèlerait indéfiniment), mais elle retient son
  issue : celle-ci n'est jamais redistribuée tant que la carte vit.
- **Gate disque** : sous 3 Gio libres, aucune mission ne démarre et Jean est
  prévenu. Chaque mission crée un worktree ; un disque plein casse le runtime
  bien plus sûrement qu'une issue traitée en retard.
- **Silence par défaut** : un tick sans rien à signaler n'écrit rien sur la
  sortie, donc le cron n'envoie aucun message. Jean n'est sollicité que pour un
  GO de merge, un blocage ou une panne.
- **Les cartes humaines sont intouchables** : la boucle ne rapporte et
  n'archive que les cartes qu'elle a créées (`created_by = linear-loop`).
