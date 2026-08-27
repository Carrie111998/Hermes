---
sidebar_position: 15
title: МиниМакс OAuth
description: Войдите в MiniMax через браузер OAuth и используйте модели MiniMax-M2.7
  в агенте Hermes — ключ API не требуется.
---

# МиниМакс OAuth

Агент Hermes поддерживает **MiniMax** через процесс входа в систему OAuth на основе браузера, используя те же учетные данные, что и [портал MiniMax](https://www.minimax.io). Никакого API-ключа или кредитной карты не требуется — войдите в систему один раз, и Hermes автоматически обновит ваш сеанс.

Транспорт повторно использует адаптер `anthropic_messages` (MiniMax предоставляет конечную точку, совместимую с Anthropic Messages, в `/anthropic`), поэтому все существующие функции вызова инструментов, потоковой передачи и контекста работают без каких-либо изменений адаптера.

## Обзор

| Товар | Значение |
|------|-------|
| Идентификатор провайдера | `minimax-oauth` |
| Отображаемое имя | МиниМакс (OAuth) |
| Тип аутентификации | OAuth браузера (поток перенаправления PKCE) |
| Транспорт | Совместимость с антропными сообщениями (`anthropic_messages`) |
| Модели | `MiniMax-M2.7`, `MiniMax-M2.7-highspeed` |
| Глобальная конечная точка | `https://api.minimax.io/anthropic` |
| Конечная точка Китай | `https://api.minimaxi.com/anthropic` |
| Требуется env var | Нет (`MINIMAX_API_KEY` **не** используется для этого провайдера) |

## Предварительные условия

- Питон 3.9+
- Установлен агент Гермес
- Учетная запись MiniMax на сайте [minimax.io](https://www.minimax.io) (глобально) или [minimaxi.com](https://www.minimaxi.com) (Китай)
– Браузер, доступный на локальном компьютере (или используйте `--no-browser` для удаленных сеансов).

## Быстрый старт

```bash
# Launch the provider and model picker
hermes model
# → Select "MiniMax (OAuth)" from the provider list
# → Hermes opens your browser to the MiniMax authorization page
# → Approve access in the browser
# → Select a model (MiniMax-M2.7 or MiniMax-M2.7-highspeed)
# → Start chatting

hermes
```

После первого входа в систему учетные данные сохраняются в `~/.hermes/auth.json` и автоматически обновляются перед каждым сеансом.

## Вход в систему вручную

Вы можете инициировать вход в систему, не проходя через средство выбора модели:

```bash
hermes auth add minimax-oauth
```

### Китайский регион

Если ваша учетная запись находится на платформе Китая (`minimaxi.com`), вместо этого используйте поставщика `minimax-cn` на основе ключей API — `minimax-cn` регистрируется только с `auth_type="api_key"` (без потока OAuth). Настройте `MINIMAX_CN_API_KEY` (и, возможно, `MINIMAX_CN_BASE_URL`) напрямую:

```bash
echo 'MINIMAX_CN_API_KEY=your-key' >> ~/.hermes/.env
```

### Удаленные/безголовые сеансы

На серверах или контейнерах, где браузер недоступен:

```bash
hermes auth add minimax-oauth --no-browser
```

Hermes распечатает URL-адрес подтверждения и код пользователя — откройте URL-адрес на любом устройстве и введите код при появлении запроса.

## Процесс OAuth

Hermes реализует поток OAuth браузера PKCE для конечных точек OAuth MiniMax:

1. Hermes генерирует пару верификатор/запрос PKCE и случайное значение состояния.
2. Он отправляет POST на `{base_url}/oauth/code` с запросом и получает `user_code` и `verification_uri`.
3. В вашем браузере откроется `verification_uri`. При появлении запроса введите `user_code`.
4. Hermes опрашивает `{base_url}/oauth/token` до тех пор, пока не прибудет токен (или не пройдет крайний срок).
5. Токены (`access_token`, `refresh_token`, срок действия) сохраняются в `~/.hermes/auth.json` под ключом `minimax-oauth`.

Обновление токена (стандартное разрешение OAuth `refresh_token`) запускается автоматически при каждом запуске сеанса, когда срок действия токена доступа истекает в течение 60 секунд.

## Проверка статуса входа

```bash
hermes doctor
```

В разделе `◆ Auth Providers` будет показано:

```
✓ MiniMax OAuth  (logged in, region=global)
```

или, если вы не вошли в систему:

```
⚠ MiniMax OAuth  (not logged in)
```

## Переключение моделей

```bash
hermes model
# → Select "MiniMax (OAuth)"
# → Pick from the model list
```

Или установите модель напрямую:

```bash
hermes config set model.default MiniMax-M2.7
hermes config set model.provider minimax-oauth
```

## Справочник по конфигурации

После входа в систему `~/.hermes/config.yaml` будет содержать записи, подобные:

```yaml
model:
  default: MiniMax-M2.7
  provider: minimax-oauth
  base_url: https://api.minimax.io/anthropic
```

### Конечные точки региона

| Идентификатор провайдера | Портал | Конечная точка вывода |
|-------------|--------|-------------------|
| `minimax-oauth` (глобальный) | `https://api.minimax.io` | `https://api.minimax.io/anthropic` |
| `minimax-cn` (Китай) | `https://api.minimaxi.com` | `https://api.minimaxi.com/anthropic` |

### Псевдонимы поставщиков

Все следующее приводит к `minimax-oauth`:

```bash
hermes --provider minimax-oauth    # canonical
hermes --provider minimax-portal   # alias
hermes --provider minimax-global   # alias
hermes --provider minimax_oauth    # alias (underscore form)
```

## Переменные среды

Поставщик `minimax-oauth` **не** использует `MINIMAX_API_KEY` или `MINIMAX_BASE_URL`. Эти переменные предназначены только для поставщиков `minimax` и `minimax-cn` на основе ключей API.

| Переменная | Эффект |
|----------|--------|
| `MINIMAX_API_KEY` | Используется только провайдером `minimax` — игнорируется для `minimax-oauth` |
| `MINIMAX_CN_API_KEY` | Используется только провайдером `minimax-cn` — игнорируется для `minimax-oauth` |

Чтобы использовать `minimax-oauth` в качестве активного поставщика, установите `model.provider: minimax-oauth` в `config.yaml` (используйте `hermes setup` для управляемого потока) или передайте `--provider minimax-oauth` для одного вызова:

```bash
hermes --provider minimax-oauth
```

## Модели

| Модель | Лучшее для |
|-------|----------|
| `MiniMax-M2.7` | Рассуждения в длинном контексте, вызов сложных инструментов |
| `MiniMax-M2.7-highspeed` | Меньшая задержка, более легкие задачи, дополнительные вызовы |

Обе модели поддерживают до 200 000 токенов контекста.

`MiniMax-M2.7` также автоматически используется в качестве вспомогательной модели для задач визуализации и делегирования, когда `minimax-oauth` является основным поставщиком.

## Устранение неполадок

### Срок действия токена истек — автоматический повторный вход не выполняется.

Hermes обновляет токен при каждом запуске сеанса, если срок его действия истекает в течение 60 секунд. Если срок действия токена доступа уже истек (например, после длительного периода автономной работы), обновление происходит автоматически при следующем запросе. Если обновление завершается с ошибкой `refresh_token_reused` или `invalid_grant`, Hermes помечает сеанс как требующий повторного входа в систему.

Когда сбой обновления является окончательным (HTTP 4xx, `invalid_grant`, отозван грант и т. д.), Hermes помечает токен обновления как мертвый и помещает его в карантин локально, чтобы он не продолжал воспроизводить обреченный обмен. Агент отображает одно сообщение «требуется повторная аутентификация» и не вмешивается, пока вы снова не войдете в систему.

**Исправление:** снова запустите `hermes auth add minimax-oauth`, чтобы начать новый вход в систему. Карантин снимается при следующем успешном обмене.

### Время авторизации истекло

Поток кода устройства имеет ограниченный срок действия. Если вы не подтвердите вход вовремя, Hermes выдаст ошибку тайм-аута.

**Исправление:** повторно запустите `hermes auth add minimax-oauth` (или `hermes model`). Поток начинается заново.

### Несоответствие состояний (возможно CSRF)

Компания Hermes обнаружила, что значение `state`, возвращаемое сервером авторизации, не соответствует тому, что он отправил.

**Исправление:** повторите вход в систему. Если проблема сохраняется, проверьте наличие прокси-сервера или перенаправления, изменяющего ответ OAuth.

### Вход с удаленного сервера

Если `hermes` не может открыть окно браузера, используйте `--no-browser`:

```bash
hermes auth add minimax-oauth --no-browser
```

Hermes печатает URL-адрес и код. Откройте URL-адрес на любом устройстве и завершите процесс там.

### Ошибка «Не выполнен вход в MiniMax OAuth» во время выполнения

В хранилище аутентификации нет учетных данных для `minimax-oauth`. Вы еще не вошли в систему или файл учетных данных был удален.

**Исправление**: запустите `hermes model` и выберите MiniMax (OAuth) или запустите `hermes auth add minimax-oauth`.

## Выход из системы

Чтобы удалить сохраненные учетные данные MiniMax OAuth:

```bash
hermes auth logout minimax-oauth
```

## См. также

- [Справочник поставщиков ИИ](../integrations/providers.md)
- [Переменные среды](../reference/environment-variables.md)
- [Конфигурация](../user-guide/configuration.md)
- [доктор Гермес](../reference/cli-commands.md)