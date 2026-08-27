---
title: Hyperliquid — рыночные данные Hyperliquid, история счета, обзор сделок.
sidebar_label: Hyperliquid
description: Данные рынка гиперликвидов, история счета, обзор сделок
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Гипержидкость

Данные гиперликвидного рынка, история счета, обзор сделок.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/blockchain/hyperliquid` |
| Путь | `optional-skills/blockchain/hyperliquid` |
| Версия | `0.1.0` |
| Автор | Хьюго Секье (Hugo-SEQUIER), агент Hermes |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Hyperliquid`, `Blockchain`, `Crypto`, `Trading`, `Perpetuals`, `Spot`, `DeFi` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Гипержидкий навык

Запрашивайте данные рынка и счетов Hyperliquid через общедоступную конечную точку `/info`.
Только для чтения — без ключа API, без подписи и размещения заказа.

12 команд: `dexs`, `markets`, `spots`, `candles`, `funding`, `l2`, `state`,
`spot-balances`, `fills`, `orders`, `review`, `export`. только стандартная библиотека
(`urllib`, `json`, `argparse`).

---

## Когда использовать

- Пользователь запрашивает данные Hyperliquid perp или спотового рынка, свечи, финансирование или книгу L2.
- Пользователь хочет проверить перп-позиции, спотовые балансы, исполнения или ордера кошелька.
- Пользователь хочет получить обзор после сделки, объединяющий недавние исполнения с рыночным контекстом.
- Пользователь хочет проверить развернутые застройщиком индексы преступников или рынки HIP-3.
- Пользователь хочет нормализованный экспорт свечей в формате JSON + финансирование для подготовки к бэктестированию.

---

## Предварительные условия

Только Stdlib — никаких внешних пакетов и ключа API.

Скрипт читает `${HERMES_HOME:-~/.hermes}/.env` для двух дополнительных значений по умолчанию:

- `HYPERLIQUID_API_URL` — по умолчанию `https://api.hyperliquid.xyz`. Установить на
  `https://api.hyperliquid-testnet.xyz` для тестовой сети.
- `HYPERLIQUID_USER_ADDRESS` — адрес по умолчанию для `state`, `spot-balances`,
  `fills`, `orders` и `review`. Если не установлено, передайте адрес первым
  позиционный аргумент.

Проект `.env` в текущем рабочем каталоге считается резервным вариантом для разработчиков.

Вспомогательный скрипт: `~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py`

---

## Как бежать

Вызов с помощью инструмента `terminal`:

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py <command> [args]
```

Добавьте `--json` к любой команде для машиночитаемого вывода.

---

## Краткий справочник

```bash
hyperliquid_client.py dexs
hyperliquid_client.py markets [--dex DEX] [--limit N] [--sort volume|oi|funding_abs|change_abs|name]
hyperliquid_client.py spots [--limit N]
hyperliquid_client.py candles <coin> [--interval 1h] [--hours 24] [--limit N]
hyperliquid_client.py funding <coin> [--hours 72] [--limit N]
hyperliquid_client.py l2 <coin> [--levels N]
hyperliquid_client.py state [address] [--dex DEX]
hyperliquid_client.py spot-balances [address] [--limit N]
hyperliquid_client.py fills [address] [--hours N] [--limit N] [--aggregate-by-time]
hyperliquid_client.py orders [address] [--limit N]
hyperliquid_client.py review [address] [--coin COIN] [--hours N] [--fills N]
hyperliquid_client.py export <coin> [--interval 1h] [--hours N] [--output PATH]
```

Для `state`, `spot-balances`, `fills`, `orders` и `review` адрес
необязательно, если в `${HERMES_HOME:-~/.hermes}/.env` установлено `HYPERLIQUID_USER_ADDRESS`.

---

## Процедура

### 1. Откройте для себя DEX и рынки

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py dexs

python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  markets --limit 15 --sort volume

python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  spots --limit 15
```

- `--dex` применяется только к конечным точкам преступников; опустите для первого преступника декс.
– Пары спотов могут отображаться как `PURR/USDC` или псевдонимами, например `@107`.
- На рынках HIP-3 к монете добавляется префикс dex, например `mydex:BTC`.

### 2. Получение исторических рыночных данных

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  candles BTC --interval 1h --hours 72 --limit 48

python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  funding BTC --hours 168 --limit 30
```

Конечные точки временного диапазона разбиваются на страницы. Для больших окон повторите действия позже.
`startTime` или используйте `export` (ниже).

### 3. Проверьте книгу текущих заказов

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  l2 BTC --levels 10
```

Используйте, когда вас спрашивают о глубине баланса, краткосрочной ликвидности или потенциальном рынке.
Влияние крупного заказа.

### 4. Проверьте учетную запись

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  state 0xabc...

python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  spot-balances
```

`state` возвращает позиции преступников; `spot-balances` возвращает спотовые запасы.
Используйте их для вопросов «как обстоят дела с моими позициями?», «что у меня в руках?», «сколько стоит
съемный?".

### 5. Просмотр исполнений и ордеров

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  fills 0xabc... --hours 72 --limit 25

python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  orders --limit 25
```

### 6. Создайте обзор сделки

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  review 0xabc... --hours 72 --fills 50

python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  review --coin BTC --hours 168
```

Отчеты о реализованных прибылях и убытках, комиссиях, подсчете выигрышей/проигрышей, разбивке монет, тенденциях рынка
и среднее финансирование для каждого торгуемого перса, а также эвристика (перетаскивание комиссий,
концентрация, потери против тренда).

Для более глубокого постторгового анализа: начните с `review`, чтобы найти проблемные монеты.
или Windows → извлеките `fills` и `orders` за этот период → извлеките `candles`
и `funding` за каждую торгуемую монету → качество решения оценивается отдельно
от качества результата.

### 7. Экспортируйте многоразовый набор данных

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  export BTC --interval 1h --hours 168 --output ./btc-1h-7d.json

python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  export BTC --interval 15m --hours 72 --end-time-ms 1760000000000
```

Выходной JSON содержит: версию схемы, исходные метаданные, окно точного времени,
нормализованные строки свечей, нормализованные строки финансирования, сводная статистика. Использование
`--end-time-ms` для воспроизводимых окон.

---

## Подводные камни

- Конечные точки общедоступной информации ограничены по скорости. Большие исторические запросы могут
  вернуть закрытые окна; выполнить итерацию с более поздними значениями `startTime`.
- `fills --hours ...` использует `userFillsByTime`, который предоставляет только
  недавнее скользящее окно — не полная история архива.
- `historicalOrders` возвращает только последние заказы; не полноценный экспорт.
- Команда `review` является эвристической. Он не может реконструировать намерение,
  качество размещения ордеров или истинное проскальзывание только от исполнения.
- Команда `export` записывает нормализованный набор данных, а не бэктест.
  двигатель. Вам по-прежнему нужна собственная модель проскальзывания/заполнения.
- Псевдонимы Spot, такие как `@107`, являются действительными идентификаторами, даже если в пользовательском интерфейсе отображается
  более дружелюбное имя.
- `l2` — это снимок на определенный момент времени, а не временной ряд.

---

## Проверка

```bash
python3 ~/.hermes/skills/blockchain/hyperliquid/scripts/hyperliquid_client.py \
  markets --limit 5
```

Следует напечатать основные объемы продаж гиперликвидов по условному 24-часовому объему.