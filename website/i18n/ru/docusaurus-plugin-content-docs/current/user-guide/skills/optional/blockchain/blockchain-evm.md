---
title: 'Evm — клиент EVM только для чтения: кошельки, токены, газ в 8 цепочках'
sidebar_label: Evm
description: 'Клиент EVM только для чтения: кошельки, токены, газ в 8 цепочках'
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Эвм

Клиент EVM только для чтения: кошельки, токены, газ в 8 цепочках.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/blockchain/evm` |
| Путь | `optional-skills/blockchain/evm` |
| Версия | `1.0.0` |
| Автор | Mibayy (@Mibayy), youssefea (@youssefea), ethernet8023 (@ethernet8023), агент Hermes |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `EVM`, `Ethereum`, `BNB`, `BSC`, `Base`, `Arbitrum`, `Polygon`, `Optimism`, `Avalanche`, `zkSync`, `Blockchain`, `Crypto`, `Web3`, `DeFi`, `NFT`, `ENS`, `Whale`, `Security` |
| Сопутствующие навыки | [`solana`](/docs/user-guide/skills/optional/blockchain/blockchain-solana) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Навыки блокчейна EVM

Запрашивайте EVM-совместимые данные блокчейна в 8 цепочках по ценам в долларах США.
14 команд: портфель кошелька, информация о токене, транзакции, активность, трекер газа,
сетевая статистика, поиск цен, сканирование нескольких цепочек, обнаружение китов, разрешение ENS,
средство проверки разрешений, инспектор контрактов и декодер транзакций.

Поддерживает 8 цепочек: Ethereum, BNB Chain (BSC), Base, Arbitrum One, Polygon,
Оптимизм, Avalanche (C-Chain), Эра zkSync.

Ключ API не требуется. Нулевые внешние зависимости — только стандартная библиотека Python
(urllib, json, argparse, потоковая обработка).

> **Заменяет отдельный навык `base`.** Токены, специфичные для базы (AERO, DEGEN,
> TOSHI, BRETT, WELL, cbETH, cbBTC, wstETH, rETH) и все базовые функции RPC
> ранее проживавшие под `optional-skills/blockchain/base/`, были свернуты
> в этот навык. Передайте `--chain base` любой команде для покрытия базы.

---

## Когда использовать
- Пользователь запрашивает баланс кошелька или портфеля в любой цепочке EVM.
- Пользователь хочет проверить один и тот же кошелек во ВСЕХ цепочках одновременно.
- Пользователь хочет проверить транзакцию по хешу (или расшифровать то, что она сделала)
- Пользователю нужны метаданные токена ERC-20, цена, предложение или рыночная капитализация.
- Пользователь хочет недавнюю историю транзакций для адреса.
- Пользователь хочет текущие цены на газ или сравнить комиссии в разных цепочках.
- Пользователь хочет найти крупные перемещения китов в последних блоках.
- Пользователь просит разрешить имя ENS (vitalik.eth) или выполнить обратный поиск адреса.
- Пользователь хочет проверить, есть ли в контракте одобрение опасных токенов.
- Пользователь хочет проверить смарт-контракт (прокси? ERC-20? ERC-721? размер байт-кода?)
- Пользователь хочет сравнить стоимость газа в разных цепочках перед транзакцией.

---

## Предварительные условия
Только стандартная библиотека Python 3.8+. Никакой установки pip не требуется.
Цены: бесплатный API CoinGecko (ограничена по скорости, ~ 10–30 запросов в минуту).
ENS: общедоступный API ensideas.com.
Декодирование передачи: общедоступный API 4byte.directory.

Переопределить конечную точку RPC: `export EVM_RPC_URL=https://your-rpc.com`

Путь вспомогательного сценария: `~/.hermes/skills/blockchain/evm/scripts/evm_client.py`

---

## Краткий справочник

```
SCRIPT=~/.hermes/skills/blockchain/evm/scripts/evm_client.py

# Network & prices
python3 $SCRIPT stats                            # Ethereum stats
python3 $SCRIPT stats --chain arbitrum           # Arbitrum stats
python3 $SCRIPT compare                          # Gas + prices ALL 8 chains

# Wallet
python3 $SCRIPT wallet 0xd8dA...96045            # Portfolio (ETH + ERC-20)
python3 $SCRIPT wallet 0xd8dA...96045 --chain bsc
python3 $SCRIPT multichain 0xd8dA...96045        # Same wallet on ALL chains

# Tokens & prices
python3 $SCRIPT price ETH
python3 $SCRIPT price 0xdAC1...1ec7              # By contract address
python3 $SCRIPT token 0xdAC1...1ec7              # ERC-20 metadata + market cap

# Transactions
python3 $SCRIPT tx 0x5c50...f060                 # Transaction details
python3 $SCRIPT decode 0x5c50...f060             # Decode input data (4byte.directory)
python3 $SCRIPT activity 0xd8dA...96045          # Recent transactions

# Gas
python3 $SCRIPT gas                              # Gas prices + cost estimates
python3 $SCRIPT gas --chain optimism

# Security
python3 $SCRIPT allowance 0xd8dA...96045         # Dangerous ERC-20 approvals
python3 $SCRIPT contract 0xdAC1...1ec7           # Contract inspection (proxy? standards?)

# ENS
python3 $SCRIPT ens vitalik.eth                  # Name -> address + profile
python3 $SCRIPT ens 0xd8dA...96045               # Address -> ENS name

# Whale detection
python3 $SCRIPT whale                            # Large transfers (last 20 blocks, >$10k)
python3 $SCRIPT whale --blocks 50 --min-usd 100000 --chain arbitrum
```

---

## Процедура

### 0. Проверка настройки
```bash
python3 --version   # 3.8+ required
python3 ~/.hermes/skills/blockchain/evm/scripts/evm_client.py stats
```

### 1. Портфель кошельков
Собственный баланс + известные токены ERC-20, отсортированные по стоимости в долларах США.
```bash
python3 $SCRIPT wallet 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
python3 $SCRIPT wallet 0xd8dA... --chain bsc --no-prices   # faster
```

### 2. Сканирование нескольких цепочек
Сканирует все 8 цепочек одновременно на предмет одного и того же адреса с помощью потоков.
```bash
python3 $SCRIPT multichain 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045
```
Выход: собственный баланс каждой цепочки + запасы токенов + общая сумма в долларах США.

### 3. Сравнить (Газ + Цены)
Все 8 цепочек опрашиваются параллельно. Показывает самую дешевую/самую дорогую сеть.
```bash
python3 $SCRIPT compare
```

### 4. Детали транзакции и декодирование
```bash
python3 $SCRIPT tx 0x5c504ed432cb51138bcf09aa5e8a410dd4a1e204ef84bfed1be16dfba1b22060
python3 $SCRIPT decode 0x5c504ed...   # Shows human-readable function signature
```
Декодирование использует 4byte.directory для перевода 0xa9059cbb -> Transfer(address,uint256).

### 5. Разрешение ENS
```bash
python3 $SCRIPT ens vitalik.eth          # -> 0xd8dA... + avatar + social links
python3 $SCRIPT ens 0xd8dA...96045       # -> vitalik.eth
```

### 6. Проверка разрешений (Безопасность)
Проверяет одобрения ERC-20, предоставленные известным DEX/мостовым контрактам.
```bash
python3 $SCRIPT allowance 0xYourWallet
```
Помечает НЕОГРАНИЧЕННЫЕ одобрения как ВЫСОКИЙ риск.

### 7. Инспектор по контрактам
```bash
python3 $SCRIPT contract 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48   # USDC (proxy)
python3 $SCRIPT contract 0xdAC17F958D2ee523a2206206994597C13D831ec7   # USDT (ERC-20)
```
Обнаруживает: прокси (EIP-1967/EIP-1167), ERC-20, ERC-721, ERC-165. Показывает размер байт-кода и адрес реализации прокси.

### 8. Обнаружение китов
```bash
python3 $SCRIPT whale                                    # ETH, last 20 blocks, >$10k
python3 $SCRIPT whale --blocks 50 --min-usd 50000 --chain bsc
```

### 9. Газовый трекер
```bash
python3 $SCRIPT gas
python3 $SCRIPT gas --chain polygon
```
Показывает цену gwei + стоимость в долларах США за: перевод, перевод ERC-20, одобрение, обмен, монетный двор NFT, перевод NFT.

---

## Поддерживаемые цепочки
| Ключ | Имя | Родной | Идентификатор цепочки |
|-----------|----------------|--------|----------|
| эфириум | Эфириум | ЭТФ | 1 |
| бакалавр наук | Сеть БНБ | БНБ | 56 |
| база | База | ЭТФ | 8453 |
| арбитраж | Арбитрум Один | ЭТФ | 42161 |
| многоугольник | Полигон | ПОЛ | 137 |
| оптимизм | Оптимизм | ЭТФ | 10 |
| лавина | Лавина С | АВАКС | 43114 |
| zksync | zkSync Эра | ЭТФ | 324 |

---

## Подводные камни
- Уровень бесплатного пользования CoinGecko: ~10–30 запросов/мин. Используйте `--no-prices` для более быстрого сканирования кошелька.
- Общественные RPC могут регулироваться. Установите для EVM_RPC_URL частную конечную точку для производства.
- `wallet` и `allowance` проверяют только список известных токенов (~30 токенов на цепочку). Используйте обозреватель блоков для полного обнаружения токенов.
- `activity` сканирует только последние блоки (максимум 200). Для получения полной истории используйте Etherscan API.
- `multichain` запускает 8 параллельных потоков — может активировать ограничения скорости на общедоступных RPC.
- Разрешение ENS зависит от одной общедоступной конечной точки (ensideas.com/ens.vitalik.ca) без резервного варианта. Если эта конечная точка не работает, `ens` завершится неудачно — запустите повторно позже или воспользуйтесь обозревателем блоков.
— Декодирование Tx зависит от одной общедоступной конечной точки (4byte.directory) без резервного варианта. Селекторы, которых нет в их базе данных, отображаются как `unknown`.
- **Оценки газа L2 относятся только к выполнению L2.** В таких объединениях, как Base, Arbitrum, Optimism и zkSync, фактическая стоимость транзакции также включает плату за публикацию данных L1, которая зависит от размера данных вызова и текущих цен на газ L1. Команда `gas` не оценивает этот компонент L1. В частности, для Base см. оракул комиссии L1 сети (контракт `0x420000000000000000000000000000000000000F`).
- Входные данные адреса/tx-хеша проверяются на наличие префикса 0x + правильная длина + шестнадцатеричный код, но регистр контрольной суммы EIP-55 **не** применяется (конечные точки RPC принимают шестнадцатеричный код в любом регистре).

---

## Проверка
```bash
# Should print current block, gas price, ETH price
python3 ~/.hermes/skills/blockchain/evm/scripts/evm_client.py stats

# Should resolve vitalik.eth to 0xd8dA...
python3 ~/.hermes/skills/blockchain/evm/scripts/evm_client.py ens vitalik.eth
```