---
title: Solana — Запрос кошельков, токенов, txs и NFT Solana в долларах США.
sidebar_label: Solana
description: Запрос кошельков, токенов, txs и NFT Solana в долларах США
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Солана

Запрашивайте кошельки, токены, транзакции и NFT Solana в долларах США.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/blockchain/solana` |
| Путь | `optional-skills/blockchain/solana` |
| Версия | `0.2.0` |
| Автор | Дениз Алагоз (gizdusum), улучшенный агентом Hermes |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Solana`, `Blockchain`, `Crypto`, `Web3`, `RPC`, `DeFi`, `NFT` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Навык Соланы в области блокчейна

Запрашивайте данные Solana в сети, дополненные ценами в долларах США, через CoinGecko.
8 команд: портфель кошелька, информация о токене, транзакции, активность, NFT,
обнаружение китов, сетевая статистика и поиск цен.

Ключ API не требуется. Использует только стандартную библиотеку Python (urllib, json, argparse).

---

## Когда использовать

- Пользователь запрашивает баланс кошелька Solana, количество токенов или стоимость портфеля.
- Пользователь хочет проверить конкретную транзакцию по подписи.
- Пользователю нужны метаданные токена SPL, цена, предложение или топ-холдеры.
- Пользователь хочет недавнюю историю транзакций для адреса.
- Пользователь хочет, чтобы NFT принадлежали кошельку.
- Пользователь хочет найти крупные передачи SOL (обнаружение китов)
- Пользователь хочет узнать о состоянии сети Solana, TPS, эпохе или цене SOL.
- Пользователь спрашивает: «Какова цена BONK/JUP/SOL?»

---

## Предварительные условия

Вспомогательный скрипт использует только стандартную библиотеку Python (urllib, json, argparse).
Никаких внешних пакетов не требуется.

Данные о ценах поступают из бесплатного API CoinGecko (ключ не требуется, скорость ограничена).
до ~10-30 запросов/мин). Для более быстрого поиска используйте флаг `--no-prices`.

---

## Краткий справочник

Конечная точка RPC (по умолчанию): https://api.mainnet-beta.solana.com.
Переопределить: экспорт SOLANA_RPC_URL=https://your-private-rpc.com

Путь к вспомогательному скрипту: ~/.hermes/skills/blockchain/solana/scripts/solana_client.py

```
python3 solana_client.py wallet   <address> [--limit N] [--all] [--no-prices]
python3 solana_client.py tx       <signature>
python3 solana_client.py token    <mint_address>
python3 solana_client.py activity <address> [--limit N]
python3 solana_client.py nft      <address>
python3 solana_client.py whales   [--min-sol N]
python3 solana_client.py stats
python3 solana_client.py price    <mint_or_symbol>
```

---

## Процедура

### 0. Проверка настройки

```bash
python3 --version

# Optional: set a private RPC for better rate limits
export SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"

# Confirm connectivity
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py stats
```

### 1. Портфель кошельков

Получите баланс SOL, запасы токенов SPL в долларах США, количество NFT и
итог портфеля. Токены отсортированы по стоимости, отфильтрованы от пыли, известные токены
помечены по имени (BONK, JUP, USDC и т. д.).

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py \
  wallet 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
```

Флаги:
- `--limit N` — показать N верхних токенов (по умолчанию: 20)
- `--all` — показать все токены, без пылевого фильтра, без ограничений
- `--no-prices` — пропустить поиск цен CoinGecko (быстрее, только RPC)

Вывод включает в себя: баланс SOL + стоимость в долларах США, список токенов с отсортированными ценами.
по стоимости, количеству пыли, сводке NFT, общей стоимости портфеля в долларах США.

### 2. Детали транзакции

Проверьте полную транзакцию по ее подписи base58. Показывает изменения баланса
как в солях, так и в долларах США.

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py \
  tx 5j7s8K...your_signature_here
```

Вывод: слот, временная метка, комиссия, статус, изменения баланса (SOL + USD),
программные вызовы.

### 3. Информация о токене

Получите метаданные токена SPL, текущую цену, рыночную капитализацию, предложение, десятичные дроби,
органы монетного двора/замораживания и 5 крупнейших держателей.

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py \
  token DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263
```

Выходные данные: имя, символ, десятичные дроби, предложение, цена, рыночная капитализация, топ-5.
держатели с процентами.

### 4. Недавняя активность

Список последних транзакций для адреса (по умолчанию: последние 10, максимум: 25).

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py \
  activity 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM --limit 25
```

### 5. Портфель NFT

Перечислите NFT, принадлежащие кошельку (эвристика: токены SPL с суммой = 1, десятичными знаками = 0).

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py \
  nft 9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM
```

Примечание. Сжатые NFT (cNFT) не обнаруживаются этой эвристикой.

### 6. Детектор китов

Сканируйте самый последний блок на наличие крупных переводов SOL в долларах США.

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py \
  whales --min-sol 500
```

Примечание: сканируется только последний блок — снимок на определенный момент времени, а не исторический.

### 7. Статистика сети

Состояние сети Solana в реальном времени: текущий слот, эпоха, TPS, поставка, валидатор.
версия, цена SOL и рыночная капитализация.

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py stats
```

### 8. Поиск цен

Быстрая проверка цены любого токена по адресу монетного двора или известному символу.

```bash
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py price BONK
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py price JUP
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py price SOL
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py price DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263
```

Известные символы: SOL, USDC, USDT, BONK, JUP, WETH, JTO, mSOL, stSOL,
ПИТ, HNT, RNDR, WEN, W, TNSR, DRIFT, bSOL, JLP, WIF, MEW, BOME, PENGU.

---

## Подводные камни

- **Ограничения скорости CoinGecko** — бесплатный уровень допускает ~10–30 запросов в минуту.
  При поиске цен используется 1 запрос на каждый токен. Кошельки с большим количеством токенов могут
  не получить цены на все из них. Используйте `--no-prices` для скорости.
- **Ограничения скорости общедоступных RPC** — ограничения общедоступных запросов RPC в основной сети Solana.
  Для производственного использования установите SOLANA_RPC_URL в частную конечную точку.
  (Гелиус, QuickNode, Тритон).
- **Обнаружение NFT является эвристическим** — количество=1 + десятичные числа=0. Сжатый
  NFT (cNFT) и NFT Token-2022 не появятся.
- **Детектор китов сканирует только последний блок** — не исторический. Результаты
  меняться в зависимости от момента запроса.
- **История транзакций** — публичный RPC хранится около 2 дней. Старые транзакции
  может быть недоступно.
- **Имена токенов** — около 25 известных токенов помечены по имени. Другие
  показывать сокращенные адреса монетных дворов. Используйте команду `token` для получения полной информации.
- **Повторить попытку по номеру 429** — вызовы RPC и CoinGecko повторяют попытку до 2 раз.
  с экспоненциальной отсрочкой при ошибках ограничения скорости.

---

## Проверка

```bash
# Should print current Solana slot, TPS, and SOL price
python3 ~/.hermes/skills/blockchain/solana/scripts/solana_client.py stats
```