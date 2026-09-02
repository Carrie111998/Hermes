# Relatório de implementação — P0 de segurança e encerramento de subprocessos

## 1. Escopo e decisão de prioridade

Este documento registra exclusivamente a implementação do P0 identificado em `RELATORIO_UNIFICADO.md`. O trabalho foi deliberadamente limitado aos dois riscos P0:

- **P0-A — timeout de `execFileNoThrow`:** garantir que o timeout seja resolvido mesmo quando um processo filho/grandchild mantém os descritores de saída abertos.
- **P0-B — whitelist de ferramentas em workers:** preservar a política de autorização da whitelist quando a execução de ferramentas atravessa threads do pool de workers.

Os itens P1 não foram implementados, testados como correções ou incluídos no issue/PR. Eles permanecem fora do escopo desta entrega para evitar misturar mitigação urgente com mudanças de prioridade inferior.

## 2. Resumo executivo

O P0-B foi corrigido no código de produção: a política de whitelist, antes armazenada em `threading.local()`, agora usa `contextvars.ContextVar`. Isso permite que o contexto de autorização seja capturado e propagado para os workers pela infraestrutura já existente em `tools.thread_context`, sem alterar a regra de negação nem abrir uma exceção para ferramentas fora da whitelist.

O P0-A já estava corrigido no código de produção no `origin/main`, pelos commits `27fb1179f8` e `30b2e44dc`. A investigação confirmou que a correção foi entregue no PR #93355. Portanto, esta implementação não duplica nem reabre a alteração de produção: adiciona um teste de regressão multiplataforma que executa um processo nativo mantendo stdio aberto e comprova que o timeout retorna com código `124`.

## 3. Evidência e causa raiz do P0-A

### Sintoma

Uma chamada de `execFileNoThrow` podia permanecer pendurada após o prazo configurado quando um daemon ou grandchild herdava os descritores de stdio. Esperar o evento de `close` do processo não é suficiente nesse cenário: o processo direto pode terminar, mas o pipe continua aberto por um descendente.

### Causa raiz

A resolução dependia de um evento de encerramento que não precisava ocorrer no momento do timeout. Assim, a promessa podia não ser liquidada apesar de o prazo de segurança já ter expirado.

### Estado encontrado

A implementação de produção em `origin/main` já contém a correção upstream. O histórico confirma:

- `27fb1179f8 fix(tui): settle execFileNoThrow on timeout even when a daemon holds stdio`;
- `30b2e44dc fix(tui): settle execFileNoThrow timeouts unconditionally`;
- PR `#93355`, já merged: `fix(tui): clipboard subprocess timeouts settle even when a daemon grandchild holds stdio (#93134)`.

### Alteração desta entrega

Foi adicionado em `ui-tui/packages/hermes-ink/src/utils/execFileNoThrow.test.ts` um teste que inicia o próprio `process.execPath` com um timer de 30 segundos, configura timeout de 200 ms e verifica o retorno `code === 124`. O teste não depende de `/bin/sh`, `sleep`, sinais POSIX ou de um sistema operacional simulado; ele usa um processo nativo disponível no runtime Node.

Esse teste protege o contrato contra regressão sem alterar novamente o código de produção já corrigido upstream.

## 4. Evidência e causa raiz do P0-B

### Sintoma

A whitelist de ferramentas é consultada durante os hooks de autorização. Quando a execução foi deslocada para o pool de workers, o worker não encontrava a whitelist configurada no thread que iniciou a operação. O resultado era uma política de segurança silenciosamente inerte no caminho de execução em worker: ferramentas que deveriam ser negadas podiam não receber a decisão de negação.

### Causa raiz

`threading.local()` associa valores ao thread atual. O valor configurado no thread de origem não é automaticamente visível em outro thread. O executor já possuía o mecanismo correto para propagar contexto (`propagate_context_to_thread`), mas a whitelist não era um `ContextVar`; portanto, não participava dessa captura e propagação.

### Correção

Em `hermes_cli/plugins.py`:

1. o armazenamento thread-local foi substituído por dois `ContextVar`;
2. a whitelist permitida continua sendo configurada por `set_thread_tool_whitelist`;
3. `clear_thread_tool_whitelist` restaura o estado sem whitelist e o formatador padrão;
4. `get_pre_tool_call_block_message` mantém a mesma decisão e a mesma mensagem padrão;
5. o formatador customizado continua sendo respeitado;
6. nenhum fallback para valores de outro perfil, thread ou contexto foi introduzido.

O código de execução em `agent/tool_executor.py` já propaga o contexto para os caminhos sequencial e concorrente; a alteração torna a política de whitelist compatível com esse contrato existente.

## 5. Invariantes de segurança preservados

A implementação deve continuar obedecendo aos seguintes invariantes:

- uma ferramenta fora da whitelist recebe uma decisão de bloqueio;
- uma ferramenta presente na whitelist não é bloqueada por esse hook;
- o formato padrão da mensagem de negação permanece estável;
- um formatador customizado continua funcionando;
- o `clear` remove a política do contexto corrente e evita vazamento para operações futuras;
- a política não é copiada por estado global mutável nem lida de `os.environ`;
- a autorização não depende de uma suposição sobre qual thread executará a ferramenta;
- o timeout do subprocesso não depende de todos os descendentes fecharem seus pipes;
- não há alteração de toolset, system prompt ou histórico de conversa.

## 6. Arquivos alterados

- `hermes_cli/plugins.py`
  - armazenamento da whitelist e do formatador convertido para `ContextVar`.
- `tests/hermes_cli/test_plugins.py`
  - regressão que configura a whitelist no thread de origem e verifica o bloqueio dentro de um worker com contexto propagado.
- `ui-tui/packages/hermes-ink/src/utils/execFileNoThrow.test.ts`
  - regressão multiplataforma para timeout com processo nativo mantendo stdio aberto.
- `RELATORIO_P0_IMPLEMENTACAO.md`
  - documentação desta entrega.
- `assets/infografico-p0-tool-safety.png`
  - infográfico visual da causa raiz, correção, limites e evidências do P0.

## 7. Validação local reproduzível

Os comandos abaixo foram executados no worktree limpo baseado em `origin/main`, após a implementação do P0:

### Whitelist e plugin hooks

```text
HERMES_PYTHON='C:/Users/Nitro/hermes-agent/.venv/Scripts/python.exe' scripts/run_tests.sh tests/hermes_cli/test_plugins.py -q
```

Resultado real:

```text
75 tests passed, 0 failed.
```

O runner foi utilizado conforme o contrato do projeto, com ambiente hermético, timezone UTC e locale C.UTF-8.

### Timeout do TUI

```text
cd ui-tui
npm test -- --run packages/hermes-ink/src/utils/execFileNoThrow.test.ts
```

Resultado real:

```text
1 passed | 5 skipped (6)
```

O teste novo usa um processo nativo multiplataforma que mantém o stdio aberto; os cinco testes POSIX-only existentes foram pulados pelo ambiente Windows. O Vitest reportou apenas os avisos de configuração npm já existentes (`min-release-age` e `min-release-age-exclude`); não houve falha de teste.

### Higiene do patch

```text
git diff --check
```

Resultado real: nenhuma inconsistência de whitespace foi reportada.

A validação focada, o `git diff --check` e a verificação do PNG foram repetidos no worktree limpo usado para o commit e o PR. O resultado final do CI remoto é registrado somente após a leitura dos checks reais; não é tratado como verde por inferência.

## 8. Revisão de arquitetura, segurança e concorrência

O mapa arquitetural foi consultado em `graphify-out/GRAPH_REPORT.md` antes da alteração. Os contratos relevantes são:

```text
tools/thread_context.py -> agent/tool_executor.py -> hooks de plugins
hermes_cli/plugins.py -> autorização pre_tool_call
ui-tui/.../execFileNoThrow.ts -> testes de subprocesso do Hermes Ink
```

A análise SAC concentrou-se em dois modos de falha distintos:

1. **perda de autorização por fronteira de thread:** resolvida tornando a política explicitamente propagável por contexto;
2. **não liquidação por herança de stdio:** coberta por teste que força o cenário de grandchild/stdio aberto contra o timeout.

Não foram adicionados suppressions de erro, `except` silencioso, aumento de timeout, bypass de whitelist ou dependência nova.

## 9. Limites da entrega

- O P0-A não recebeu uma segunda mudança de produção porque a correção já existe em `origin/main` e foi identificada no PR #93355; esta entrega adiciona a proteção de regressão local.
- O P0-B foi implementado e coberto no caminho de contexto propagado para worker.
- Nenhum item P1 foi implementado.
- O infográfico é documentação visual e não substitui os testes automatizados.
- A aprovação final depende dos checks reais do PR e da revisão dos artefatos do branch limpo.

## 10. Rastreabilidade externa

Os artefatos GitHub foram criados e lidos de volta pela API:

- Issue: `https://github.com/NousResearch/hermes-agent/issues/98479`
- Pull request: `https://github.com/NousResearch/hermes-agent/pull/98485`
- Branch de entrega: `fix/p0-tool-safety`
- Commit de implementação P0: `e8dc3a9a5bb852a6c957d1eb183491ef24792a05` (código e testes).
- Commit de documentação e infográfico: `ea439f8a01222008d4cac81d5860bafbf763eac2` (artefatos finais até esta etapa).

A issue e o PR devem manter explícita a relação com o P0, referenciar a evidência upstream do P0-A e declarar que o P1 está fora do escopo.

### Infográfico

- Arquivo versionado no branch: `assets/infografico-p0-tool-safety.png`.
- Cópia de conveniência: `C:\Users\Nitro\Downloads\hermes-p0-pr-98485.png`.
- Validação real: PNG RGB, 1024x1536, 3.165.150 bytes; a cópia e o arquivo versionado foram comparados por tamanho.
- O gerador de temas executou 10.000 temas únicos com seed `98077`; o tema selecionado foi `tema-08486` (`steampunk-gotico`), com paleta `prata oxidada, azul petróleo e roxo escuro`.
- A inspeção visual confirmou texto legível, sem clipping ou sobreposição aparente, e representação dos dois caminhos P0, do bloqueio de bypass silencioso e da exclusão de P1.
- O infográfico é documentação visual e não substitui os testes automatizados nem a revisão do diff.

## 11. Checklist de encerramento

- [x] Escopo limitado ao P0.
- [x] Causa raiz do P0-A confirmada no histórico upstream.
- [x] Regressão P0-A adicionada sem duplicar a correção de produção.
- [x] Whitelist convertida de estado thread-local para contexto propagável.
- [x] Regressão P0-B adicionada para worker com contexto propagado.
- [x] Testes focados executados com resultado real.
- [x] `git diff --check` executado.
- [x] Worktree limpo baseado em `origin/main` preparado e verificado.
- [x] Commit P0 criado.
- [x] Issue criada e lida de volta pela API.
- [x] PR criado e lido de volta pela API.
- [ ] CI remoto verificado.
- [ ] Três passes de exhaustion review concluídos.
- [x] URLs e SHA finais incorporados neste documento.
- [x] Infográfico PNG validado no branch de entrega.
