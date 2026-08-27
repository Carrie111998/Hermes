---
title: Пулы учетных данных
description: Объединение нескольких ключей API или токенов OAuth для каждого поставщика
  для автоматической ротации и восстановления ограничения скорости.
sidebar_label: Credential Pools
sidebar_position: 9
---

# Пулы учетных данных

Пулы учетных данных позволяют зарегистрировать несколько ключей API или токенов OAuth для одного и того же поставщика. Когда один ключ достигает предела скорости или квоты выставления счетов, Hermes автоматически переключается на следующий работоспособный ключ, поддерживая работоспособность вашего сеанса без переключения провайдера.

Это отличается от [резервных поставщиков](./fallback-providers.md), которые полностью переключаются на *другого* поставщика. Пулы учетных данных представляют собой ротацию одного и того же поставщика; Резервные поставщики — это аварийное переключение между поставщиками. Сначала проверяются пулы — если все ключи пула исчерпаны, *затем* активируется резервный поставщик.

:::warning Вращение ключа сбрасывает кэш подсказок.
Кэши подсказок на стороне поставщика (Anthropic, OpenAI, OpenRouter) ограничены ключом учетной записи/API, который сделал запрос. Когда пул переключается на другой ключ в середине сеанса, новый ключ не имеет кэшированного префикса для вашего разговора — следующий запрос пересчитывает всю историю по входной цене без дисконта, а последующий возврат — это еще одно полное перечитывание, если только TTL кэша предыдущего ключа еще жив. Ротация поддерживает работу вашего сеанса, и в этом суть, но при длительных разговорах каждая ротация стоит одного полного прохода контекста.
:::

:::совет
Пулы учетных данных в основном предназначены для поставщиков API-ключей (OpenRouter, Anthropic). Один OAuth [Nous Portal](/integrations/nous-portal) охватывает более 300 моделей, поэтому большинству пользователей не нужен пул при работе на портале.
:::

## Как это работает

```
Your request
  → Pick key from pool (round_robin / least_used / fill_first / random)
  → Send to provider
  → 429 rate limit?
      → Plan/usage limit reached (e.g. ChatGPT/Codex "usage limit reached")?
          → Rotate to next pool key immediately (no retry — the cap won't clear on retry)
      → Generic / transient 429?
          → Retry same key once (transient blip)
          → Second 429 → rotate to next pool key
      → All keys exhausted → fallback_model (different provider)
  → 402 billing error?
      → Immediately rotate to next pool key (1h cooldown)
  → 401 auth expired?
      → Try refreshing the token (OAuth)
      → Refresh failed → rotate to next pool key
  → Success → continue normally
```

## Быстрый старт

Если у вас уже есть ключ API, установленный в `.env`, Hermes автоматически обнаружит его как пул с одним ключом. Чтобы получить выгоду от объединения, добавьте больше ключей:

```bash
# Add a second OpenRouter key
hermes auth add openrouter --api-key sk-or-v1-your-second-key

# Add a second Anthropic key
hermes auth add anthropic --type api-key --api-key sk-ant-api03-your-second-key

# Add an Anthropic OAuth credential (requires Claude Max plan + extra usage credits)
hermes auth add anthropic --type oauth
# Opens browser for OAuth login
```

Проверьте свои бассейны:

```bash
hermes auth list
```

Выход:
```
openrouter (2 credentials):
  #1  OPENROUTER_API_KEY   api_key env:OPENROUTER_API_KEY ←
  #2  backup-key           api_key manual

anthropic (3 credentials):
  #1  hermes_pkce          oauth   hermes_pkce ←
  #2  claude_code          oauth   claude_code
  #3  ANTHROPIC_API_KEY    api_key env:ANTHROPIC_API_KEY
```

`←` отмечает выбранные в данный момент учетные данные.

## Интерактивное управление

Запустите `hermes auth` без подкоманды интерактивного мастера:

```bash
hermes auth
```

Это показывает ваш полный статус пула и предлагает меню:

```
What would you like to do?
  1. Add a credential
  2. Remove a credential
  3. Reset cooldowns for a provider
  4. Set rotation strategy for a provider
  5. Exit
```

Для провайдеров, которые поддерживают как ключи API, так и OAuth (Anthropic, Nous, Codex), в процессе добавления запрашивается тип:

```
anthropic supports both API keys and OAuth login.
  1. API key (paste a key from the provider dashboard)
  2. OAuth login (authenticate via browser)
Type [1/2]:
```

## Команды CLI

| Команда | Описание |
|---------|-------------|
| `hermes auth` | Мастер интерактивного управления пулом |
| `hermes auth list` | Показать все пулы и учетные данные |
| `hermes auth list <provider>` | Показать пул конкретного провайдера |
| `hermes auth add <provider>` | Добавьте учетные данные (запрашивается тип и ключ) |
| `hermes auth add <provider> --type api-key --api-key <key>` | Добавить ключ API в неинтерактивном режиме |
| `hermes auth add <provider> --type oauth` | Добавьте учетные данные OAuth через вход в браузер |
| `hermes auth remove <provider> <index>` | Удалить учетные данные по индексу на основе 1 |
| `hermes auth reset <provider>` | Очистить все кулдауны/статусы истощения |

## Стратегии ротации

Настройте через `hermes auth` → «Настроить стратегию ротации» или в `config.yaml`:

```yaml
credential_pool_strategies:
  openrouter: round_robin
  anthropic: least_used
```

| Стратегия | Поведение |
|----------|----------|
| `fill_first` (по умолчанию) | Используйте первый исправный ключ, пока он не закончится, затем переходите к следующему |
| `round_robin` | Переключайте клавиши равномерно, вращая их после каждого выбора |
| `least_used` | Всегда выбирайте ключ с наименьшим количеством запросов |
| `random` | Случайный выбор среди исправных ключей |

## Восстановление ошибок

Пул по-разному обрабатывает разные ошибки:

| Ошибка | Поведение | Время восстановления |
|-------|----------|----------|
| **Ограничение скорости 429** | Повторите тот же ключ один раз (временно). Второй подряд 429 переходит к следующему ключу | 1 час |
| **402 Биллинг/Квота** | Немедленно перейти к следующей клавише | 1 час |
| **Срок действия аутентификации 401 истек** | Попробуйте сначала обновить токен OAuth. Поворот только в случае сбоя обновления | 5 минут |
| **Все ключи исчерпаны** | Перейти к `fallback_model`, если настроено | — |

Временные метки `reset_at`, предоставленные поставщиком, переопределяют эти значения времени восстановления по умолчанию.

Флаг `has_retried_429` сбрасывается при каждом успешном вызове API, поэтому одиночный переходный код 429 не запускает ротацию.

## Пользовательские пулы конечных точек

Пользовательские конечные точки, совместимые с OpenAI (Together.ai, RunPod, локальные серверы), получают свои собственные пулы, ключом которых является имя конечной точки из словаря `providers:` в config.yaml (или устаревшего списка `custom_providers`, который переносится автоматически).

Когда вы настраиваете пользовательскую конечную точку через `hermes model`, она автоматически генерирует имя, например «Together.ai» или «Local (localhost:8080)». Это имя становится ключом пула.

```bash
# After setting up a custom endpoint via hermes model:
hermes auth list
# Shows:
#   Together.ai (1 credential):
#     #1  config key    api_key config:Together.ai ←

# Add a second key for the same endpoint:
hermes auth add Together.ai --api-key sk-together-second-key
```

Пользовательские пулы конечных точек хранятся в `auth.json` под `credential_pool` с префиксом `custom:`:

```json
{
  "credential_pool": {
    "openrouter": [...],
    "custom:together.ai": [...]
  }
}
```

## Автообнаружение

Hermes автоматически обнаруживает учетные данные из нескольких источников и заполняет пул при запуске:

| Источник | Пример | Автопосев? |
|--------|---------|-------------|
| Переменные среды | `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` | Да |
| Токены OAuth (auth.json) | Код устройства Codex, Код устройства Nous | Да |
| Учетные данные Клода Кода | `~/.claude/.credentials.json` | Да (антропный) |
| Гермес PKCE OAuth | `~/.hermes/auth.json` | Да (антропный) |
| Пользовательская конфигурация конечной точки | `model.api_key` в config.yaml | Да (пользовательские конечные точки) |
| Ручной ввод | Добавлено через `hermes auth add` | Сохраняется в auth.json |

Записи с автоматическим заполнением обновляются при каждой загрузке пула — если вы удалите переменную env, ее запись в пуле будет автоматически удалена. Записи, введенные вручную (добавленные через `hermes auth add`), никогда не удаляются автоматически.

Заимствованные секреты времени выполнения (например, переменные среды, ссылки Bitwarden/Vault/keyring/systemd и пользовательские значения конфигурации) доступны только для ссылок на границе `auth.json`. Hermes может использовать разрешенное значение в памяти для текущего запуска, но сохраняет только метаданные, такие как ссылка на источник, метка, статус, счетчики запросов и необратимый отпечаток. Записанные вручную записи и состояние OAuth/кода устройства, принадлежащие Hermes, сохраняют надежные токены, которые им необходимо обновить.

## Делегирование и совместное использование субагентов

Когда агент создает субагентов через `delegate_task`, родительский пул учетных данных автоматически передается дочерним элементам:

- **Тот же поставщик** — дочерний элемент получает полный пул родительского объекта, что позволяет осуществлять ротацию ключей при ограничениях скорости.
- **Другой провайдер** — дочерний элемент загружает собственный пул этого провайдера (если настроено).
- **Пул не настроен** — дочерний элемент возвращается к унаследованному единственному ключу API.

Это означает, что субагенты получают ту же устойчивость к ограничению скорости, что и родительский агент, без необходимости дополнительной настройки. Аренда учетных данных для каждой задачи гарантирует, что дети не будут конфликтовать друг с другом при одновременной смене ключей.

## Потокобезопасность

Пул учетных данных использует блокировку потоков для всех мутаций состояния (`select()`, `mark_exhausted_and_rotate()`, `try_refresh_current()`, `mark_used()`). Это обеспечивает безопасный одновременный доступ, когда шлюз одновременно обрабатывает несколько сеансов чата.

## Архитектура

Полную схему потока данных см. в [`docs/credential-pool-flow.excalidraw`](https://excalidraw.com/#json=2Ycqhqpi6f12E_3ITyiwh,c7u9jSt5BwrmiVzHGbm87g) в репозитории.

Пул учетных данных интегрируется на уровне разрешения поставщика:

1. **`agent/credential_pool.py`** — Менеджер пула: хранение, выбор, ротация, время восстановления.
2. **`hermes_cli/auth_commands.py`** — команды CLI и интерактивный мастер.
3. **`hermes_cli/runtime_provider.py`** — Разрешение учетных данных с учетом пула
4. **`run_agent.py`** — Исправление ошибки: 429/402/401 → ротация пула → откат

## Хранилище

Состояние пула хранится в `~/.hermes/auth.json` под ключом `credential_pool`:

```json
{
  "version": 1,
  "credential_pool": {
    "openrouter": [
      {
        "id": "abc123",
        "label": "OPENROUTER_API_KEY",
        "auth_type": "api_key",
        "priority": 0,
        "source": "env:OPENROUTER_API_KEY",
        "secret_source": "bitwarden",
        "secret_fingerprint": "sha256:12ab34cd56ef7890",
        "last_status": "ok",
        "request_count": 142
      }
    ],
    "anthropic": [
      {
        "id": "manual1",
        "label": "personal-api-key",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "sk-ant-api03-..."
      }
    ]
  }
}
```

Приведенная выше запись OpenRouter была заимствована из внешнего источника, поэтому необработанный ключ не хранится в `auth.json`. Ручная запись Anthropic была намеренно добавлена ​​в хранилище учетных данных Hermes, поэтому ее токен остается постоянным.

Стратегии хранятся в `config.yaml` (не `auth.json`):

```yaml
credential_pool_strategies:
  openrouter: round_robin
  anthropic: least_used
```