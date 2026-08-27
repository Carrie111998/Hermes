---
title: Pptx Author — создавайте презентации PowerPoint без головы с помощью python-pptx
sidebar_label: Pptx Author
description: Создавайте презентации PowerPoint без головы с помощью python-pptx
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Автор Pptx

Создавайте презентации PowerPoint без головы с помощью python-pptx.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/finance/pptx-author` |
| Путь | `optional-skills/finance/pptx-author` |
| Версия | `1.0.0` |
| Автор | Антропный (адаптировано Nous Research) |
| Лицензия | Апач-2.0 |
| Платформы | Linux, MacOS, Windows |
| Теги | `powerpoint`, `pptx`, `python-pptx`, `presentation`, `finance` |
| Сопутствующие навыки | [`excel-author`](/docs/user-guide/skills/optional/finance/finance-excel-author), [`powerpoint`](/docs/user-guide/skills/bundled/productivity/productivity-powerpoint) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# pptx-автор

Создайте файл .pptx на диске, используя `python-pptx`. Используйте его, когда вам нужно представить колоду в виде файлового артефакта, а не проводить сеанс PowerPoint в реальном времени.

Адаптировано на основе навыков Anthropic `pptx-author` и `pitch-deck` в [anthropics/financial-services](https://github.com/anthropics/financial-services). Ветки MCP/Office-JS оригиналов удалены — это предполагает безголовый Python.

Более широкие, уже реализованные навыки создания PowerPoint (слайды, заметки докладчика, встраивания, мультимедиа) см. во встроенном навыке `powerpoint`. Этот навык представляет собой упрощенный шаблон, настроенный для колод на основе моделей (презентации, заметки IC, заметки о доходах), где каждое число должно быть прослежено до исходной рабочей книги.

## Выходной контракт

- Напишите `./out/<name>.pptx`. Создайте `./out/`, если он не существует.
- Верните относительный путь в ваше последнее сообщение.

## Настройка

```bash
pip install "python-pptx>=0.6"
```

## Основные соглашения

### Одна идея на слайд
В заголовке указывается суть; тело поддерживает его. Слайд под названием «Выручка за 3-й квартал» слабый; «Рост выручки ускорился до 14% г/г в третьем квартале» — это сильный показатель.

### Каждое число соответствует модели
Если рисунок на слайде взят из `./out/model.xlsx`, сделайте сноску на листе и в ячейке.

```
Revenue: $1,250M  (Source: model.xlsx, Inputs!C3)
```

Никогда не переписывайте числа из памяти или из сводки — откройте книгу, прочитайте именованный диапазон и привязывайте к нему значение колоды программно, если это возможно.

### Используйте шаблон фирмы, когда он установлен
Если `./templates/firm-template.pptx` существует, загрузите его, чтобы колода унаследовала фирменные цвета, шрифты и основные макеты.

```python
from pptx import Presentation
from pathlib import Path

template = Path("./templates/firm-template.pptx")
prs = Presentation(str(template)) if template.exists() else Presentation()
```

### Диаграммы: PNG-из-модели превосходят собственные диаграммы pptx
Если точность имеет значение (стиль диаграммы модели должен точно соответствовать колоде), визуализируйте диаграмму в формате PNG из исходной книги и внедрите изображение. Собственные диаграммы `pptx.chart` хрупкие и часто не соответствуют общепринятым соглашениям.

```python
from pptx.util import Inches
slide.shapes.add_picture("./out/charts/football_field.png",
                         Inches(1), Inches(2),
                         width=Inches(8))
```

### Никаких внешних отправок
Этот навык записывает файл. Он никогда не отправляет электронные письма, не загружает и не публикует сообщения. Уровни оркестровки обеспечивают доставку.

## Скелет

```python
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pathlib import Path

template = Path("./templates/firm-template.pptx")
prs = Presentation(str(template)) if template.exists() else Presentation()

# Title slide
slide = prs.slides.add_slide(prs.slide_layouts[0])
slide.shapes.title.text = "Project Aurora — Strategic Alternatives"
slide.placeholders[1].text = "Preliminary Discussion Materials"

# Valuation summary slide (title-only layout)
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = "Valuation implies $38–$52 per share across methodologies"

# Add a table bound to model outputs
rows, cols = 5, 4
tbl_shape = slide.shapes.add_table(rows, cols,
                                   Inches(0.5), Inches(1.5),
                                   Inches(9), Inches(3))
tbl = tbl_shape.table
headers = ["Methodology", "Low ($)", "Mid ($)", "High ($)"]
for c, h in enumerate(headers):
    tbl.cell(0, c).text = h

# In a real deck, read these from the model workbook with openpyxl
data = [
    ("Trading comps",     "35", "41", "48"),
    ("Precedent M&A",     "39", "45", "52"),
    ("DCF (base)",        "36", "43", "51"),
    ("LBO (10% IRR)",     "33", "38", "44"),
]
for r, row in enumerate(data, start=1):
    for c, val in enumerate(row):
        tbl.cell(r, c).text = val

# Embed a chart rendered from the model
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = "Football field — current price $42"
slide.shapes.add_picture("./out/charts/football_field.png",
                         Inches(1), Inches(1.8), width=Inches(8))

Path("./out").mkdir(exist_ok=True)
prs.save("./out/pitch-aurora.pptx")
```

## Привязка номеров колод к исходной книге

Считывайте именованные диапазоны или отдельные ячейки из вашей модели Excel, чтобы номера колод никогда не менялись.

```python
from openpyxl import load_workbook

wb = load_workbook("./out/model.xlsx", data_only=True)
def nr(name):
    """Resolve a named range to its current computed value."""
    rng = wb.defined_names[name]
    sheet, coord = next(rng.destinations)
    return wb[sheet][coord].value

revenue_fy24 = nr("RevenueFY24")
implied_mid  = nr("ImpliedSharePriceBase")
```

Затем создайте контент колоды, используя эти значения:
```python
slide.shapes.title.text = f"Implied share price of ${implied_mid:.2f} (base case)"
```

Не забудьте пересчитать книгу перед ее чтением — openpyxl видит вычисленные значения только в том случае, если что-то уже вычислило лист. Сначала запустите помощник пересчета в навыке `excel-author` или откройте/сохраните его в реальном сеансе Excel.

## Контрольный список в виде слайда для презентаций

Типичная банковская презентация следует этой структуре. Не предписывающий, но полезный в качестве стартового скелета:

1. Обложка/заголовок
2. Отказ от ответственности
3. Содержание
4. Обзор ситуации
5. Снимок компании (цель)
6. Контекст рынка/сектора
7. Итоги оценки (футбольное поле) — денежный слайд
8. Детали торговых комиссий
9. Детали прецедентных транзакций
10. Сводка DCF
11. Показательный случай LBO/спонсора
12. Аспекты процесса
13. Приложение

## Когда НЕ использовать этот навык

- Пользователи, участвующие в сеансе PowerPoint в реальном времени с доступным Office MCP — вместо этого управляют своим живым документом.
– Нефинансовые слайды (ежеквартальные обзоры, маркетинговые презентации) – используйте более широкий навык `powerpoint`.
- Деки с тяжелой анимацией, переходами или заметками докладчика — используйте более широкий навык `powerpoint`.

## Атрибуция

Соглашения адаптированы из набора плагинов Claude for Financial Services от Anthropic, под лицензией Apache-2.0. Оригинал: https://github.com/anthropics/financial-services/tree/main/plugins/agent-plugins/pitch-agent/skills/pptx-author