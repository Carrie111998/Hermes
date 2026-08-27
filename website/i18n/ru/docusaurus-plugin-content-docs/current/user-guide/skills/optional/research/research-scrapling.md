---
title: Scrapling — парсинг сайтов со скрытым просмотром и обходом Cloudflare.
sidebar_label: Scrapling
description: Парсинг сайтов со скрытым просмотром и обходом Cloudflare
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Скрэблинг

Парсинг сайтов с помощью скрытого просмотра и обхода Cloudflare.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/scrapling` |
| Путь | `optional-skills/research/scrapling` |
| Версия | `1.0.0` |
| Автор | ФЕАЗЮР |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `Web Scraping`, `Browser`, `Cloudflare`, `Stealth`, `Crawling`, `Spider` |
| Сопутствующие навыки | [`duckduckgo-search`](/docs/user-guide/skills/optional/research/research-duckduckgo-search), [`domain-intel`](/docs/user-guide/skills/optional/research/research-domain-intel) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Скрэблинг

[Scrapling](https://github.com/D4Vinci/Scrapling) — это фреймворк для парсинга веб-страниц с обходом защиты от ботов, скрытой автоматизацией браузера и фреймворком-пауком. Он предоставляет три стратегии получения данных (HTTP, динамический JS, скрытность/Cloudflare) и полный интерфейс командной строки.

**Этот навык предназначен только для образовательных и исследовательских целей.** Пользователи должны соблюдать местные и международные законы о сборе данных и соблюдать Условия обслуживания веб-сайта.

## Когда использовать

- Парсинг статических HTML-страниц (быстрее, чем инструменты браузера)
- Парсинг страниц, обработанных JS, для которых нужен настоящий браузер.
- Обход турникета Cloudflare или обнаружения ботов
- Сканирование нескольких страниц с помощью паука
- Когда встроенный инструмент `web_extract` не возвращает нужные вам данные

## Установка

```bash
pip install "scrapling[all]"
scrapling install
```

Минимальная установка (только HTTP, без браузера):
```bash
pip install scrapling
```

Только при использовании автоматизации браузера:
```bash
pip install "scrapling[fetchers]"
scrapling install
```

## Краткий справочник

| Подход | Класс | Используйте, когда |
|----------|-------|----------|
| HTTP | `Fetcher` / `FetcherSession` | Статические страницы, API, быстрые массовые запросы |
| Динамический | `DynamicFetcher` / `DynamicSession` | JS-рендеринг контента, SPA |
| Стелс | `StealthyFetcher` / `StealthySession` | Cloudflare, сайты, защищенные от ботов |
| Паук | `Spider` | Многостраничное сканирование с переходом по ссылкам |

## Использование CLI

### Извлечь статическую страницу

```bash
scrapling extract get 'https://example.com' output.md
```

С селектором CSS и олицетворением браузера:

```bash
scrapling extract get 'https://example.com' output.md \
  --css-selector '.content' \
  --impersonate 'chrome'
```

### Извлечение страницы, обработанной JS

```bash
scrapling extract fetch 'https://example.com' output.md \
  --css-selector '.dynamic-content' \
  --disable-resources \
  --network-idle
```

### Извлечение страницы, защищенной Cloudflare

```bash
scrapling extract stealthy-fetch 'https://protected-site.com' output.html \
  --solve-cloudflare \
  --block-webrtc \
  --hide-canvas
```

### POST-запрос

```bash
scrapling extract post 'https://example.com/api' output.json \
  --json '{"query": "search term"}'
```

### Выходные форматы

Выходной формат определяется расширением файла:
- `.html` -- необработанный HTML
- `.md` -- преобразовано в Markdown
- `.txt` -- обычный текст
- `.json` / `.jsonl` -- JSON

## Python: парсинг HTTP

### Одиночный запрос

```python
from scrapling.fetchers import Fetcher

page = Fetcher.get('https://quotes.toscrape.com/')
quotes = page.css('.quote .text::text').getall()
for q in quotes:
    print(q)
```

### Сеанс (постоянные файлы cookie)

```python
from scrapling.fetchers import FetcherSession

with FetcherSession(impersonate='chrome') as session:
    page = session.get('https://example.com/', stealthy_headers=True)
    links = page.css('a::attr(href)').getall()
    for link in links[:5]:
        sub = session.get(link)
        print(sub.css('h1::text').get())
```

### ОТПРАВИТЬ/ПОСТАВИТЬ/УДАЛИТЬ

```python
page = Fetcher.post('https://api.example.com/data', json={"key": "value"})
page = Fetcher.put('https://api.example.com/item/1', data={"name": "updated"})
page = Fetcher.delete('https://api.example.com/item/1')
```

### С прокси

```python
page = Fetcher.get('https://example.com', proxy='http://user:pass@proxy:8080')
```

## Python: динамические страницы (JS-рендеринг)

Для страниц, требующих выполнения JavaScript (SPA, лениво загружаемый контент):

```python
from scrapling.fetchers import DynamicFetcher

page = DynamicFetcher.fetch('https://example.com', headless=True)
data = page.css('.js-loaded-content::text').getall()
```

### Ожидание определенного элемента

```python
page = DynamicFetcher.fetch(
    'https://example.com',
    wait_selector=('.results', 'visible'),
    network_idle=True,
)
```

### Отключить ресурсы для повышения скорости

Блокирует шрифты, изображения, мультимедиа, таблицы стилей (~ на 25 % быстрее):

```python
from scrapling.fetchers import DynamicSession

with DynamicSession(headless=True, disable_resources=True, network_idle=True) as session:
    page = session.fetch('https://example.com')
    items = page.css('.item::text').getall()
```

### Автоматизация пользовательских страниц

```python
from playwright.sync_api import Page
from scrapling.fetchers import DynamicFetcher

def scroll_and_click(page: Page):
    page.mouse.wheel(0, 3000)
    page.wait_for_timeout(1000)
    page.click('button.load-more')
    page.wait_for_selector('.extra-results')

page = DynamicFetcher.fetch('https://example.com', page_action=scroll_and_click)
results = page.css('.extra-results .item::text').getall()
```

## Python: скрытый режим (обход защиты от ботов)

Для сайтов, защищенных Cloudflare или сильно защищенных отпечатками пальцев:

```python
from scrapling.fetchers import StealthyFetcher

page = StealthyFetcher.fetch(
    'https://protected-site.com',
    headless=True,
    solve_cloudflare=True,
    block_webrtc=True,
    hide_canvas=True,
)
content = page.css('.protected-content::text').getall()
```

### Скрытый сеанс

```python
from scrapling.fetchers import StealthySession

with StealthySession(headless=True, solve_cloudflare=True) as session:
    page1 = session.fetch('https://protected-site.com/page1')
    page2 = session.fetch('https://protected-site.com/page2')
```

## Выбор элемента

Все сборщики возвращают объект `Selector` следующими методами:

### CSS-селекторы

```python
page.css('h1::text').get()              # First h1 text
page.css('a::attr(href)').getall()      # All link hrefs
page.css('.quote .text::text').getall() # Nested selection
```

### XPath

```python
page.xpath('//div[@class="content"]/text()').getall()
page.xpath('//a/@href').getall()
```

### Найти методы

```python
page.find_all('div', class_='quote')       # By tag + attribute
page.find_by_text('Read more', tag='a')    # By text content
page.find_by_regex(r'\$\d+\.\d{2}')       # By regex pattern
```

### Подобные элементы

Найдите элементы со схожей структурой (полезно для списков товаров и т. д.):

```python
first_product = page.css('.product')[0]
all_similar = first_product.find_similar()
```

### Навигация

```python
el = page.css('.target')[0]
el.parent                # Parent element
el.children              # Child elements
el.next_sibling          # Next sibling
el.prev_sibling          # Previous sibling
```

## Python: фреймворк Spider

Для многостраничного сканирования со следующей ссылкой:

```python
from scrapling.spiders import Spider, Request, Response

class QuotesSpider(Spider):
    name = "quotes"
    start_urls = ["https://quotes.toscrape.com/"]
    concurrent_requests = 10
    download_delay = 1

    async def parse(self, response: Response):
        for quote in response.css('.quote'):
            yield {
                "text": quote.css('.text::text').get(),
                "author": quote.css('.author::text').get(),
                "tags": quote.css('.tag::text').getall(),
            }

        next_page = response.css('.next a::attr(href)').get()
        if next_page:
            yield response.follow(next_page)

result = QuotesSpider().start()
print(f"Scraped {len(result.items)} quotes")
result.items.to_json("quotes.json")
```

### Многосессионный паук

Направляйте запросы к различным типам сборщиков:

```python
from scrapling.fetchers import FetcherSession, AsyncStealthySession

class SmartSpider(Spider):
    name = "smart"
    start_urls = ["https://example.com/"]

    def configure_sessions(self, manager):
        manager.add("fast", FetcherSession(impersonate="chrome"))
        manager.add("stealth", AsyncStealthySession(headless=True), lazy=True)

    async def parse(self, response: Response):
        for link in response.css('a::attr(href)').getall():
            if "protected" in link:
                yield Request(link, sid="stealth")
            else:
                yield Request(link, sid="fast", callback=self.parse)
```

### Пауза/возобновление сканирования

```python
spider = QuotesSpider(crawldir="./crawl_checkpoint")
spider.start()  # Ctrl+C to pause, re-run to resume from checkpoint
```

## Подводные камни

- **Требуется установка браузера**: запустите `scrapling install` после установки pip — без него `DynamicFetcher` и `StealthyFetcher` завершатся ошибкой.
- **Тайм-ауты**: тайм-аут DynamicFetcher/StealthyFetcher составляет **миллисекунды** (по умолчанию 30 000), тайм-аут Fetcher — **секунды**.
- **Обход Cloudflare**: `solve_cloudflare=True` увеличивает время получения данных на 5–15 секунд — включайте только при необходимости.
- **Использование ресурсов**: StealthyFetcher запускает настоящий браузер — ограничьте одновременное использование.
- **Юридические требования**: всегда проверяйте файл robots.txt и Условия обслуживания веб-сайта перед очисткой. Эта библиотека предназначена для образовательных и исследовательских целей.
- **Версия Python**: требуется Python 3.10+.