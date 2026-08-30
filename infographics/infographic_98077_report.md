# Infográfico forense — Issue #98077

## Escopo

**Sintoma:** `state.db` apresentou corrupção física entre B-trees sob WAL; depois da detecção de erro malformado, escritas canônicas continuaram.

**Objeto analisado:** fronteira de persistência `SessionDB` e recuperação FTS em `hermes_state.py`, além do caminho de sessão em `gateway/session.py`.

**Limitação importante:** a evidência confirma corrupção física e a continuação de escritas após a detecção. Ela não prova que SQLite 3.50.4, isoladamente, iniciou a corrupção. A causa de baixo nível que produziu a primeira página inválida permanece indeterminada.

---

## Mapa arquitetural Graphify

```text
conversation_loop.py
        |
        v
conversation_compression.py
        |
        v
SessionDB / SessionStore
        |
        +--> _execute_write()
        |       |
        |       +--> BEGIN IMMEDIATE
        |       +--> canonical write callback
        |       +--> COMMIT / ROLLBACK
        |       +--> DatabaseError
        |               |
        |               +--> _try_runtime_fts_rebuild()
        |               |       |
        |               |       +--> rebuild_fts()
        |               |       +--> retry
        |               |
        |               +--> _enter_fts_fail_open()
        |                       |
        |                       +--> canonical write admitted again
        |
        +--> gateway/session.py
                |
                +--> _is_fts_corruption_error()
                +--> session recovery / retry
```

### Nós críticos

| Nó | Arquivo:linha | Responsabilidade | Risco observado |
|---|---|---|---|
| `SessionDB._execute_write` | `hermes_state.py:5252` | Abre transação, executa callback canônico, faz commit/rollback e classifica `DatabaseError` | O caminho de erro pode retornar para o loop de escrita; não existe latch estrutural global nesta fronteira |
| `_is_fts_write_corruption_error` | `hermes_state.py:5410` | Decide se a corrupção é específica de FTS | A classificação original aceitava `is_malformed_db_error(exc)` como evidência suficiente de FTS |
| `_try_runtime_fts_rebuild` | `hermes_state.py:5594-5656` | Tenta reconstrução runtime de índices FTS | Reconstrução é recuperação de FTS; não pode autorizar escrita canônica quando o escopo é estrutural/desconhecido |
| `_enter_fts_fail_open` | `hermes_state.py:5658` | Entra no modo de tolerância para FTS | Fail-open permite progressão; incorreto para corrupção estrutural ou evidência ambígua |
| `_FTS_TABLES` | `hermes_state.py:14898` | Lista tabelas FTS conhecidas | Evidência deve nomear uma tabela Hermes FTS5; mensagem genérica não basta |
| `SessionStore._is_fts_corruption_error` | `gateway/session.py:3932` | Classificação/recovery no gateway | Segunda porta de admissão podia repetir a confusão de escopo |
| `conversation_loop.py` | Graphify | Coordena o loop de conversa | Consumidor indireto; não é a fronteira que deve decidir integridade do banco |
| `conversation_compression.py` | Graphify | Compactação e persistência de contexto | Consumidor indireto; pode disparar escrita depois de recuperação permissiva |

---

## Evidência de reprodução

Reprodução sintética determinística observada antes do patch:

```text
canonical_calls= 2
error= database disk image is malformed
events= ['canonical', 'fail-open', 'canonical', 'fail-open']
```

Interpretação: o primeiro callback canônico encontrou `database disk image is malformed`; a classificação genérica permitiu o caminho de fail-open/retry; uma segunda chamada canônica foi executada. Esse comportamento demonstra a falha de admissão, não a origem física da corrupção.

### Testes locais registrados

- `scripts/run_tests.sh tests/gateway/test_session.py -q` — **65 passed**.
- `scripts/run_tests.sh tests/state/test_fts_runtime_rebuild.py -q` — falhou somente nos dois testes que dependem de semântica Linux (`/proc` e symlink) em host Windows:
  - `test_foreign_holder_detection_proc_readlink_deleted_wal` — `WinError 1314` (privilégio de symlink).
  - `test_foreign_holder_uninspectable_process_cmdline_fallback` — cenário Linux simulado não é equivalente no host Windows.
- Ambiente local: Python 3.11.15; SQLite 3.53.1. Portanto, não é uma validação bit-a-bit do SQLite 3.50.4 citado no relatório de campo.

---

## Causa raiz comportamental

1. `hermes_state.py:5410` misturava duas categorias distintas: erro malformado genérico e corrupção comprovadamente localizada em FTS.
2. `hermes_state.py:5594-5656` tratava a recuperação runtime FTS como caminho de continuação/retry sem prova positiva de que a corrupção estava restrita às tabelas FTS.
3. `hermes_state.py:5658` expunha fail-open para uma decisão que deveria ser limitada a FTS explicitamente identificado.
4. `gateway/session.py:3932` mantinha uma segunda classificação, criando risco de divergência entre a fronteira `SessionDB` e a fronteira do gateway.
5. Não havia uma decisão sticky de `structurally_degraded` que bloqueasse toda escrita canônica depois de corrupção estrutural, escopo desconhecido ou resultado de integridade inconclusivo.

**Resultado:** detecção de `SQLITE_CORRUPT`/malformed não era monotônica. O sistema podia detectar o dano, executar recuperação inadequada e voltar a aceitar escrita canônica.

---

## Contrato de classificação correto

```text
Erro de banco
  |
  +--> evidência positiva: mensagem nomeia tabela FTS Hermes conhecida
  |       ou integrity report nomeia somente tabelas FTS Hermes
  |       -> recuperação/rebuild FTS permitido
  |
  +--> mensagem genérica, escopo misto, holder desconhecido,
  |    falha de inspeção, integrity report saudável/inconclusivo
          -> corrupção estrutural/escopo não provado
          -> latch sticky de escrita canônica
          -> nenhum fail-open FTS
```

A classificação deve ser **afirmativa**, não inferida por ausência de evidência. `database disk image is malformed` sozinho nunca prova FTS.

---

## PR concorrente / anti-duplicidade

- **PR #98090** — aberto — `fix(state): stop structurally corrupt databases from accepting writes` — `https://github.com/NousResearch/hermes-agent/pull/98090`.
- PR #98090 é o trabalho de implementação direta para #98077 e declara `Closes #98077`.
- Este relatório é deliberadamente **documentation-only**. Não copia nem compete com a implementação de #98090. Relação: `Related to #98077`; complementa #98090 com cadeia Graphify, evidência, limitações e protocolo de revisão.
- Issues relacionadas registradas durante a investigação: #88587, #97940, #69784, #90806, #90837, #93064.

---

## Proposta de diff cirúrgico documentada

Não aplicar uma segunda implementação enquanto #98090 estiver aberto. O delta de código esperado, já descrito para revisão do PR concorrente, é:

```diff
- malformed genérico => FTS recovery / fail-open
+ FTS recovery somente com prova positiva de FTS Hermes
+ erro genérico/misto/inconclusivo => structural-degraded sticky
+ qualquer escrita canônica subsequente => rejeitada
+ gateway e SessionDB => mesma classificação e mesmo contrato
```

O ponto de segurança é a admissão de escrita, não a mensagem exibida pelo parser nem o chamador de compressão.

---

## Luna PR Exhaustion — três passes

### Pass 1 — Corrida e reentrada

- Verificar duas chamadas concorrentes atravessando `_execute_write` enquanto a primeira marca corrupção.
- O latch precisa ser publicado antes de qualquer retry e consultado dentro da mesma fronteira de lock/transação.
- Rebuild FTS não pode limpar ou substituir o estado estrutural degradado.

### Pass 2 — Rollback, commit e cascata de tipos

- Falha no callback deve tentar rollback sem esconder a exceção original.
- Falha no rollback não pode reabrir admissão.
- `sqlite3.DatabaseError`, `SQLITE_CORRUPT`, `SQLITE_NOTADB` e mensagens malformed devem convergir para a política de escopo; texto genérico não vira FTS.
- Integrity report vazio, misto, saudável ou indisponível deve permanecer não-prova de FTS.

### Pass 3 — Limites de recuperação e persistência

- Rebuild FTS bem-sucedido só autoriza retry quando a evidência está restrita a FTS.
- Corrupção cross-B-tree, canonical table, página compartilhada ou escopo desconhecido exige bloqueio sticky.
- Reinício/reabertura deve preservar a política de segurança ou revalidar antes de permitir escrita; não pode depender de um flag transitório de uma única chamada.

---

## Simplify review

- **SAFE:** remover inferência genérica de FTS e alinhar documentação/testes ao contrato afirmativo.
- **CAREFUL:** centralizar a classificação para impedir divergência entre `SessionDB` e `SessionStore`; manter nomes de tabelas FTS em uma fonte única.
- **RISKY:** alterar concorrência, latch e contrato de retry somente no PR de implementação concorrente, com testes reais de WAL, rollback e escrita após corrupção. Não duplicar essa mudança neste PR.

---

## Conclusão operacional

A falha reproduzida é uma falha de **política de admissão**: após erro de integridade, o caminho de recuperação podia retornar ao fluxo canônico. A correção segura é monotônica: só FTS explicitamente provado pode ser reconstruído; todo escopo estrutural, misto ou desconhecido bloqueia novas escritas canônicas. Este commit registra o diagnóstico e não mascara a ausência de prova sobre a origem física inicial.
