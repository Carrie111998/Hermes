---
title: Excel Author — создавайте проверяемые финансовые книги без головы с помощью
  openpyxl
sidebar_label: Excel Author
description: Создавайте проверяемые финансовые книги без головы с помощью openpyxl
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Автор Excel

Создавайте проверяемые финансовые книги без головы с помощью openpyxl.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/finance/excel-author` |
| Путь | `optional-skills/finance/excel-author` |
| Версия | `1.0.0` |
| Автор | Антропный (адаптировано Nous Research) |
| Лицензия | Апач-2.0 |
| Платформы | Linux, MacOS, Windows |
| Теги | `excel`, `openpyxl`, `finance`, `spreadsheet`, `modeling` |
| Сопутствующие навыки | [`xlsx`](/docs/user-guide/skills/bundled/productivity/productivity-xlsx), [`pptx-author`](/docs/user-guide/skills/optional/finance/finance-pptx-author), [`dcf-model`](/docs/user-guide/skills/optional/finance/finance-dcf-model), [`comps-analysis`](/docs/user-guide/skills/optional/finance/finance-comps-analysis), [`lbo-model`](/docs/user-guide/skills/optional/finance/finance-lbo-model), [`3-statement-model`](/docs/user-guide/skills/optional/finance/finance-3-statement-model) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# автор Excel

Создайте файл .xlsx на диске, используя `openpyxl`. Следуйте приведенным ниже соглашениям банковского уровня, чтобы модель была проверяемой, гибкой и доступной для проверки кем-то, кроме человека, который ее построил.

Адаптировано на основе навыков `xlsx-author` и `audit-xls` Anthropic в репозитории [anthropics/financial-services](https://github.com/anthropics/financial-services). Ветки оригиналов, специфичные для MCP/Office-JS/Cowork, удалены — этот навык предполагает безголовый Python.

## Выходной контракт

- Напишите `./out/<name>.xlsx`. Создайте `./out/`, если он не существует.
- Верните относительный путь в окончательное сообщение, чтобы последующие инструменты могли его уловить.
- Одна логическая модель на файл. Не добавляйте данные в существующую книгу, если об этом явно не попросят.

## Настройка

```bash
pip install "openpyxl>=3.0"
```

## Основные соглашения (не подлежат обсуждению)

### Синий/черный/зеленый цвет ячейки
- **Синий** (`Font(color="0000FF")`) — жестко закодированный ввод, введенный человеком. Драйверы доходов, WACC, рост терминалов, рыночные данные.
- **Черный** (по умолчанию) — формула. Каждая производная ячейка представляет собой живую формулу Excel.
- **Зеленый** (`Font(color="006100")`) — ссылка на другой лист или внешний файл.

Затем рецензент может отсканировать лист и сразу увидеть, что является предположением, а что вычислено.

### Формулы в жестком коде
Каждая ячейка расчета ДОЛЖНА быть строкой формулы, а не числом, вычисленным в Python и вставленным как значение.

```python
# WRONG — silent bug waiting to happen
ws["D20"] = revenue_prior_year * (1 + growth)

# CORRECT — flexes when the user changes the assumption
ws["D20"] = "=D19*(1+$B$8)"
```

Разрешены только жестко запрограммированные числа:
1. Исходные исторические данные (фактическая выручка, заявленная EBITDA и т. д.)
2. Факторы допущений, которые пользователь должен гибко изменять (темпы роста, входные данные WACC, терминал g)
3. Текущие рыночные данные (цена акций, баланс долга) — с комментарием к ячейке, документирующим источник + дату.

Если вы поймаете себя на том, что вычисляете значение на Python и записываете результат, остановитесь.

### Именованные диапазоны для межтабличных ссылок
Используйте именованные диапазоны для любого рисунка, на который есть ссылка из другого листа, колоды или заметки.

```python
from openpyxl.workbook.defined_name import DefinedName
wb.defined_names["WACC"] = DefinedName("WACC", attr_text="Inputs!$C$8")
# then elsewhere:
calc["D30"] = "=D29/WACC"
```

### Вкладка «Проверки баланса»
Добавьте вкладку `Checks`, которая связывает все и отображает ИСТИНА/ЛОЖЬ:
- Балансовые остатки (активы = обязательства + собственный капитал)
- Денежный поток связан с изменением денежных средств за период в BS.
- Привязка суммы частей к консолидированным итогам
- Никаких мошеннических жестких кодов внутри диапазонов вычислений.

Пример:
```python
checks = wb.create_sheet("Checks")
checks["A2"] = "BS balances"
checks["B2"] = "=IS!D20-IS!D21-IS!D22"
checks["C2"] = "=ABS(B2)<0.01"  # TRUE/FALSE
```

### Комментарии к ячейке для каждого жестко запрограммированного ввода
Добавьте комментарий ПРИ создании ячейки, а не позже.

```python
from openpyxl.comments import Comment
ws["C2"] = 1_250_000_000
ws["C2"].font = Font(color="0000FF")
ws["C2"].comment = Comment("Source: 10-K FY2024, p.47, revenue line", "analyst")
```

Формат: `Source: [System/Document], [Date], [Reference], [URL if applicable]`.

Никогда не откладывайте поиск. Никогда не пишите `TODO: add source`.

## Скелет: типичная финансовая модель

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.comments import Comment
from openpyxl.utils import get_column_letter
from pathlib import Path

BLUE = Font(color="0000FF")
BLACK = Font(color="000000")
GREEN = Font(color="006100")
BOLD = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True)

wb = Workbook()

# --- Inputs tab ---
inp = wb.active
inp.title = "Inputs"
inp["A1"] = "MARKET DATA & KEY INPUTS"
inp["A1"].font = HEADER_FONT
inp["A1"].fill = HEADER_FILL
inp.merge_cells("A1:C1")

inp["B3"] = "Revenue FY2024"
inp["C3"] = 1_250_000_000
inp["C3"].font = BLUE
inp["C3"].comment = Comment("Source: 10-K FY2024 p.47", "model")

inp["B4"] = "Growth Rate"
inp["C4"] = 0.12
inp["C4"].font = BLUE

# --- Calc tab ---
calc = wb.create_sheet("DCF")
calc["B2"] = "Projected Revenue"
calc["C2"] = "=Inputs!C3*(1+Inputs!C4)"   # formula, black

# --- Checks tab ---
chk = wb.create_sheet("Checks")
chk["A2"] = "BS balances"
chk["B2"] = "=ABS(BS!D20-BS!D21-BS!D22)<0.01"

Path("./out").mkdir(exist_ok=True)
wb.save("./out/model.xlsx")
```

## Заголовки разделов с объединенными ячейками

Причуда openpyxl: при слиянии установите значение в верхней левой ячейке и стилизуйте весь диапазон отдельно.

```python
ws["A7"] = "CASH FLOW PROJECTION"
ws["A7"].font = HEADER_FONT
ws.merge_cells("A7:H7")
for col in range(1, 9):  # A..H
    ws.cell(row=7, column=col).fill = HEADER_FILL
```

## Таблицы чувствительности

Создавайте с помощью циклов, а не жестко запрограммированных формул для каждой ячейки. Правила:

- **Нечетное количество строк/столбцов** (5×5 или 7×7) — гарантирует наличие истинной центральной ячейки.
- **Центральная ячейка = базовый случай.** Заголовок средней строки/столбца должен равняться фактической WACC модели и терминалу g, чтобы центральный результат равнялся подразумеваемой цене акций в базовом случае. Это проверка здравомыслия.
– **Выделите центральную ячейку** заливкой синего цвета (`"BDD7EE"`) и жирным шрифтом.
- Заполните каждую ячейку полной формулой пересчета, а не приближением.

```python
# 5x5 WACC (rows) x terminal growth (cols) sensitivity
wacc_axis = [0.08, 0.085, 0.09, 0.095, 0.10]        # center row = base 9.0%
term_axis = [0.02, 0.025, 0.03, 0.035, 0.04]        # center col = base 3.0%

start_row = 40
ws.cell(row=start_row, column=1).value = "Implied Share Price ($)"
ws.cell(row=start_row, column=1).font = BOLD

for j, g in enumerate(term_axis):
    ws.cell(row=start_row+1, column=2+j).value = g
    ws.cell(row=start_row+1, column=2+j).font = BLUE

for i, w in enumerate(wacc_axis):
    r = start_row + 2 + i
    ws.cell(row=r, column=1).value = w
    ws.cell(row=r, column=1).font = BLUE
    for j, g in enumerate(term_axis):
        c = 2 + j
        # Full DCF recalc formula (simplified for illustration).
        # In a real model this references the full projection block.
        ws.cell(row=r, column=c).value = (
            f"=SUMPRODUCT(FCF_range,1/(1+{w})^year_offset) + "
            f"FCF_terminal*(1+{g})/({w}-{g})/(1+{w})^terminal_year"
        )

# Highlight center cell (base case)
center = ws.cell(row=start_row+2+len(wacc_axis)//2,
                 column=2+len(term_axis)//2)
center.fill = PatternFill("solid", fgColor="BDD7EE")
center.font = BOLD
```

## Перерасчет перед доставкой

openpyxl записывает строки формул, но не вычисляет их. Excel выполняет перерасчет при открытии, но последующим потребителям (скрипты автоматической проверки, CI) нужны вычисленные значения.

Запустите LibreOffice или специальный этап пересчета перед доставкой:

```bash
# LibreOffice headless recalc
libreoffice --headless --calc --convert-to xlsx ./out/model.xlsx --outdir ./out/
```

Или используйте помощник пересчета Python (см. `scripts/recalc.py` в этом навыке).

## Планирование компоновки модели

Прежде чем писать какую-либо формулу:
1. Определите ВСЕ позиции строк раздела.
2. Напишите ВСЕ заголовки и метки.
3. Напишите ВСЕ разделители разделов и пустые строки.
4. ЗАТЕМ напишите формулы, используя заблокированные позиции строк.

Это предотвращает шаблон разрушения каскадных формул, при котором вставка строки заголовка после записи формул смещает все последующие ссылки.

## Пошаговое согласование с пользователем

Для больших моделей (DCF, 3 оператора, LBO) перед продолжением остановитесь и покажите пользователю промежуточные артефакты. Выявление неправильного предположения о марже до того, как вы построите таблицы чувствительности последующих этапов, сэкономит час.

Схема контрольной точки:
- После блока «Входы» → показать необработанные входы, подтвердить перед проецированием
- После прогноза выручки → подтвердить выручку + рост
- После построения FCF → подтвердите полный график
- После WACC → подтвердить ввод
- После оценки → подтвердить мост долевого участия
- ЗАТЕМ постройте таблицы чувствительности

## Когда НЕ использовать этот навык

- Пользователи в реальном сеансе Excel с доступным Office MCP — вместо этого управляйте своей активной книгой.
- Чистый экспорт табличных данных без формул — `csv` или `pandas.to_excel` проще.
- Панели мониторинга/диаграммы с высокой степенью интерактивности — используйте настоящий инструмент BI.

## Атрибуция

Условные обозначения (синий/черный/зеленый, формулы поверх жестких кодов, именованные диапазоны, правила чувствительности), адаптированные из пакета плагинов Anthropic Claude for Financial Services, лицензия Apache-2.0. Оригинал: https://github.com/anthropics/financial-services/tree/main/plugins/vertical-plugins/financial-anaанализ/skills/xlsx-author