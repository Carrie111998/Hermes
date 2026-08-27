---
title: Shopify — запрос к API администрирования Shopify/витрины GraphQL через Curl
sidebar_label: Shopify
description: Запросить API-интерфейсы администрирования Shopify/витрины GraphQL через
  Curl
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Shopify

Запросить API администратора Shopify/витрины GraphQL через Curl.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/productivity/shopify` |
| Путь | `optional-skills/productivity/shopify` |
| Версия | `1.0.0` |
| Автор | сообщество |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Shopify`, `E-commerce`, `Commerce`, `API`, `GraphQL` |
| Сопутствующие навыки | [`airtable`](/docs/user-guide/skills/bundled/productivity/productivity-airtable), [`xurl`](/docs/user-guide/skills/bundled/social-media/social-media-xurl) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Shopify — API GraphQL для администрирования и витрины магазина

Работайте с магазинами Shopify напрямую через `curl`: составляйте список продуктов, управляйте запасами, оформляйте заказы, обновляйте информацию о клиентах, читайте метаполя. Никакого SDK, никакой платформы приложений — только конечная точка GraphQL и токен доступа к пользовательскому приложению.

REST Admin API является устаревшим с 2024-04 года и получает только исправления безопасности. **Используйте GraphQL Admin** для всей административной работы. Используйте **Storefront GraphQL** для запросов к клиентам, доступных только для чтения (продукты, коллекции, корзина).

## Предварительные условия

1. В админке Shopify: **Настройки → Приложения и каналы продаж → Разработка приложений → Создать приложение**.
2. Нажмите **Настроить области Admin API**, выберите то, что вам нужно (примеры ниже), сохраните.
3. **Установить приложение** → токен доступа к Admin API появляется ОДИН РАЗ. Скопируйте его немедленно — Shopify больше никогда его не покажет. Токены начинаются с `shpat_`.
4. Сохраните в `${HERMES_HOME:-~/.hermes}/.env`:
   ```
   SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxx
   SHOPIFY_STORE_DOMAIN=my-store.myshopify.com
   SHOPIFY_API_VERSION=2026-01
   ```

> **Внимание!** С 1 января 2026 г. новые «устаревшие пользовательские приложения», созданные в администраторе Shopify, исчезли. В новых настройках следует использовать **Панель разработки** (`shopify.dev/docs/apps/build/dev-dashboard`). Существующие приложения, созданные администратором, продолжают работать. Если в магазине пользователя нет пользовательского приложения и оно создано после 1 января 2026 г., направьте его на панель управления разработчиком вместо процесса администрирования.

Общие области применения по задачам:
- Товары/коллекции: `read_products`, `write_products`.
- Инвентарь: `read_inventory`, `write_inventory`, `read_locations`.
- Заказы: `read_orders`, `write_orders` (30 последних без `read_all_orders`)
- Клиенты: `read_customers`, `write_customers`
- Черновики приказов: `read_draft_orders`, `write_draft_orders`.
- Выполнения: `read_fulfillments`, `write_fulfillments`
- Метаполя/метаобъекты: охватываются соответствующими областями ресурсов.

## Основы API

- **Конечная точка:** `https://$SHOPIFY_STORE_DOMAIN/admin/api/$SHOPIFY_API_VERSION/graphql.json`
- **Заголовок аутентификации:** `X-Shopify-Access-Token: $SHOPIFY_ACCESS_TOKEN` (НЕ `Authorization: Bearer`)
- **Метод:** всегда `POST`, всегда `Content-Type: application/json`, тело `{"query": "...", "variables": {...}}`
- **HTTP 200 не означает успех.** GraphQL возвращает ошибки в массиве верхнего уровня `errors` и для каждого поля `userErrors`. Всегда проверяйте оба.
- **Идентификаторы – это строки GID:** `gid://shopify/Product/10079467700516`, `gid://shopify/Variant/...`, `gid://shopify/Order/...`. Передайте это дословно — не удаляйте префикс.
– **Ограничение скорости:** рассчитывается на основе стоимости запроса (дырявое ведро). В каждом ответе есть `extensions.cost` с `requestedQueryCost`, `actualQueryCost`, `throttleStatus.{currentlyAvailable, maximumAvailable, restoreRate}`. Отступите, когда `currentlyAvailable` упадет ниже стоимости вашего следующего запроса. Стандартные магазины = ведро 100 очков, восстановление 50/с; Плюс = 1000/100.

Базовый узор для завитков (многоразовый):

```bash
shop_gql() {
  local query="$1"
  local variables="${2:-{}}"
  curl -sS -X POST \
    "https://${SHOPIFY_STORE_DOMAIN}/admin/api/${SHOPIFY_API_VERSION:-2026-01}/graphql.json" \
    -H "Content-Type: application/json" \
    -H "X-Shopify-Access-Token: ${SHOPIFY_ACCESS_TOKEN}" \
    --data "$(jq -nc --arg q "$query" --argjson v "$variables" '{query: $q, variables: $v}')"
}
```

Перейдите через `jq` для читаемого вывода. `-sS` сохраняет видимость ошибок, но скрывает индикатор выполнения.

## Открытие

### Информация о магазине + текущая версия API
```bash
shop_gql '{ shop { name myshopifyDomain primaryDomain { url } currencyCode plan { displayName } } }' | jq
```

### Список всех поддерживаемых версий API
```bash
shop_gql '{ publicApiVersions { handle supported } }' | jq '.data.publicApiVersions[] | select(.supported)'
```

## Продукты

### Поиск продуктов (первые 20 соответствующих запросов)
```bash
shop_gql '
query($q: String!) {
  products(first: 20, query: $q) {
    edges { node { id title handle status totalInventory variants(first: 5) { edges { node { id sku price inventoryQuantity } } } } }
    pageInfo { hasNextPage endCursor }
  }
}' '{"q":"hoodie status:active"}' | jq
```

Синтаксис запроса поддерживает `title:`, `sku:`, `vendor:`, `product_type:`, `status:active`, `tag:`, `created_at:>2025-01-01`. Полная грамматика: https://shopify.dev/docs/api/usage/search-syntax.

### Разбивка на страницы продуктов (курсор)
```bash
shop_gql '
query($cursor: String) {
  products(first: 100, after: $cursor) {
    edges { cursor node { id handle } }
    pageInfo { hasNextPage endCursor }
  }
}' '{"cursor":null}'
# subsequent calls: pass the previous endCursor
```

### Получить товар с вариантами + метаполями
```bash
shop_gql '
query($id: ID!) {
  product(id: $id) {
    id title handle descriptionHtml tags status
    variants(first: 20) { edges { node { id sku price compareAtPrice inventoryQuantity selectedOptions { name value } } } }
    metafields(first: 20) { edges { node { namespace key type value } } }
  }
}' '{"id":"gid://shopify/Product/10079467700516"}' | jq
```

### Создайте товар с одним вариантом
```bash
shop_gql '
mutation($input: ProductCreateInput!) {
  productCreate(product: $input) {
    product { id handle }
    userErrors { field message }
  }
}' '{"input":{"title":"Test Hoodie","status":"DRAFT","vendor":"Hermes","productType":"Apparel","tags":["test"]}}'
```

В последних версиях варианты теперь имеют свои собственные мутации:

```bash
# Add variants after creating the product
shop_gql '
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkCreate(productId: $productId, variants: $variants) {
    productVariants { id sku price }
    userErrors { field message }
  }
}' '{"productId":"gid://shopify/Product/...","variants":[{"optionValues":[{"optionName":"Size","name":"M"}],"price":"49.00","inventoryItem":{"sku":"HD-M","tracked":true}}]}'
```

### Обновить цену/артикул
```bash
shop_gql '
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id sku price }
    userErrors { field message }
  }
}' '{"productId":"gid://shopify/Product/...","variants":[{"id":"gid://shopify/ProductVariant/...","price":"55.00"}]}'
```

## Заказы

### Список последних заказов (по умолчанию последние 30 без `read_all_orders`)
```bash
shop_gql '
{
  orders(first: 20, reverse: true, query: "financial_status:paid") {
    edges { node {
      id name createdAt displayFinancialStatus displayFulfillmentStatus
      totalPriceSet { shopMoney { amount currencyCode } }
      customer { id displayName email }
      lineItems(first: 10) { edges { node { title quantity sku } } }
    } }
  }
}' | jq
```

Полезные фильтры запросов заказов: `financial_status:paid|pending|refunded`, `fulfillment_status:unfulfilled|fulfilled`, `created_at:>2025-01-01`, `tag:gift`, `email:foo@example.com`.

### Получить один заказ с адресом доставки
```bash
shop_gql '
query($id: ID!) {
  order(id: $id) {
    id name email
    shippingAddress { name address1 address2 city province country zip phone }
    lineItems(first: 50) { edges { node { title quantity variant { sku } originalUnitPriceSet { shopMoney { amount currencyCode } } } } }
    transactions { id kind status amountSet { shopMoney { amount currencyCode } } }
  }
}' '{"id":"gid://shopify/Order/...."}' | jq
```

## Клиенты

```bash
# Search
shop_gql '
{
  customers(first: 10, query: "email:*@example.com") {
    edges { node { id email displayName numberOfOrders amountSpent { amount currencyCode } } }
  }
}'

# Create
shop_gql '
mutation($input: CustomerInput!) {
  customerCreate(input: $input) {
    customer { id email }
    userErrors { field message }
  }
}' '{"input":{"email":"test@example.com","firstName":"Test","lastName":"User","tags":["api-created"]}}'
```

## Инвентарь

Запасы основаны на **инвентарных позициях**, привязанных к вариантам, количество отслеживается по **местоположению**.

```bash
# Get inventory for a variant across all locations
shop_gql '
query($id: ID!) {
  productVariant(id: $id) {
    id sku
    inventoryItem {
      id tracked
      inventoryLevels(first: 10) {
        edges { node { location { id name } quantities(names: ["available","on_hand","committed"]) { name quantity } } }
      }
    }
  }
}' '{"id":"gid://shopify/ProductVariant/..."}'
```

Корректировка запаса (дельта) — используется `inventoryAdjustQuantities`:

```bash
shop_gql '
mutation($input: InventoryAdjustQuantitiesInput!) {
  inventoryAdjustQuantities(input: $input) {
    inventoryAdjustmentGroup { reason changes { name delta } }
    userErrors { field message }
  }
}' '{
  "input": {
    "reason": "correction",
    "name": "available",
    "changes": [{"delta": 5, "inventoryItemId": "gid://shopify/InventoryItem/...", "locationId": "gid://shopify/Location/..."}]
  }
}'
```

Установить абсолютный запас (не дельту) — `inventorySetQuantities`:

```bash
shop_gql '
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { id }
    userErrors { field message }
  }
}' '{"input":{"reason":"correction","name":"available","ignoreCompareQuantity":true,"quantities":[{"inventoryItemId":"gid://shopify/InventoryItem/...","locationId":"gid://shopify/Location/...","quantity":100}]}}'
```

## Метаполя и метаобъекты

Метаполя прикрепляют пользовательские данные к ресурсам (товарам, клиентам, заказам, магазину).

```bash
# Read
shop_gql '
query($id: ID!) {
  product(id: $id) {
    metafields(first: 10, namespace: "custom") {
      edges { node { key type value } }
    }
  }
}' '{"id":"gid://shopify/Product/..."}'

# Write (works for any owner type)
shop_gql '
mutation($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id key namespace }
    userErrors { field message code }
  }
}' '{"metafields":[{"ownerId":"gid://shopify/Product/...","namespace":"custom","key":"care_instructions","type":"multi_line_text_field","value":"Wash cold. Tumble dry low."}]}'
```

## API витрины (публичный, только для чтения)

Другая конечная точка, другой токен, используемый для приложений, ориентированных на клиента, или автономных установок в стиле Hydrogen. Заголовки различаются:

- **Конечная точка:** `https://$SHOPIFY_STORE_DOMAIN/api/$SHOPIFY_API_VERSION/graphql.json`
- **Заголовок аутентификации (общедоступный):** `X-Shopify-Storefront-Access-Token: <public token>` — встраивается в браузер
- **Заголовок аутентификации (частный):** `Shopify-Storefront-Private-Token: <private token>` — только для сервера.

```bash
curl -sS -X POST \
  "https://${SHOPIFY_STORE_DOMAIN}/api/${SHOPIFY_API_VERSION:-2026-01}/graphql.json" \
  -H "Content-Type: application/json" \
  -H "X-Shopify-Storefront-Access-Token: ${SHOPIFY_STOREFRONT_TOKEN}" \
  -d '{"query":"{ shop { name } products(first: 5) { edges { node { id title handle } } } }"}' | jq
```

## Массовые операции

Для отвалов, превышающих допустимые тарифы (полный каталог продукции, все заказы за год):

```bash
# 1. Start bulk query
shop_gql '
mutation {
  bulkOperationRunQuery(query: """
    { products { edges { node { id title handle variants { edges { node { sku price } } } } } } }
  """) {
    bulkOperation { id status }
    userErrors { field message }
  }
}'

# 2. Poll status
shop_gql '{ currentBulkOperation { id status errorCode objectCount fileSize url partialDataUrl } }'

# 3. When status=COMPLETED, download the JSONL file
curl -sS "$URL" > products.jsonl
```

Каждая строка JSONL является узлом, а вложенные соединения создаются как отдельные строки с помощью `__parentId`. При необходимости пересоберите клиентскую часть.

## Вебхуки

Подпишитесь на события, чтобы не проводить опросы:

```bash
shop_gql '
mutation($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id topic endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } }
    userErrors { field message }
  }
}' '{"topic":"ORDERS_CREATE","sub":{"callbackUrl":"https://example.com/webhook","format":"JSON"}}'
```

Проверьте входящий веб-перехватчик HMAC, используя секрет клиента приложения (а не токен доступа):

```bash
echo -n "$REQUEST_BODY" | openssl dgst -sha256 -hmac "$APP_SECRET" -binary | base64
# Compare to X-Shopify-Hmac-Sha256 header
```

## Подводные камни

- **Конечные точки REST все еще существуют, но заморожены.** Не создавайте новые интеграции с `/admin/api/.../products.json`. Используйте ГрафQL.
- **Проверка формата токена.** Токены администратора начинаются с `shpat_`. Публичные токены витрины магазина с `shpua_`. Если у вас один и неправильный заголовок, каждый запрос возвращает 401 без полезного тела ошибки.
- **403 с действительным токеном = отсутствует область действия.** Shopify возвращает `{"errors":[{"message":"Access denied for ..."}]}`. Перенастройте области Admin API в приложении, а затем переустановите его, чтобы повторно создать токен.
- **`userErrors` пусто != успех.** Также проверьте, что `data.<mutation>.<resource>` не равен нулю. Некоторые ошибки не заполняют ни один из них — проверьте весь ответ.
- **GID против числового идентификатора.** Устаревший REST предоставлял числовые идентификаторы; GraphQL требует полные строки GID. Чтобы преобразовать: `gid://shopify/Product/<numeric>`.
- **Сюрприз по ограничению скорости.** Один `products(first: 250)` с глубоким вложением может стоить более 1000 баллов и сразу же дросселироваться в магазине стандартного плана. Начните с узкого, прочитайте `extensions.cost`, скорректируйте.
- **Порядок нумерации страниц.** `products(first: N, reverse: true)` сортируется по `id DESC`, а не по `created_at`. Используйте `sortKey: CREATED_AT, reverse: true` для «сначала самые новые».
- **`read_all_orders` для исторических данных.** Без него `orders(...)` автоматически ограничивает 60-дневное окно. Вы не получите ошибки, просто результатов будет меньше, чем ожидалось. Для продавцов Shopify Plus с большим количеством заказов запросите эту область через настройки защищенных данных приложения.
– **Валюты представляют собой строки.** Суммы возвращаются как `"49.00"`, а не как `49.0`. Не делайте `jq tonumber` вслепую, если вас волнует заполнение нулями.
- **Поля «Мультивалютные деньги»** содержат `shopMoney` (валюта магазина) И `presentmentMoney` (валюта клиента). Выбирайте один последовательно.

## Безопасность

Мутации в Shopify реальны — они создают продукты, взимают возвраты, отменяют заказы, отправляют заказы. Прежде чем запускать `productDelete`, `orderCancel`, `refundCreate` или любую массовую мутацию: четко укажите, в чем заключается изменение, в каком магазине, и подтвердите это пользователю. Промежуточного клона производственных данных не существует, если у пользователя нет отдельного хранилища разработки.