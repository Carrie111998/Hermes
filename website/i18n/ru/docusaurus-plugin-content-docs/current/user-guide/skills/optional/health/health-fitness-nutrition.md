---
title: Фитнес-питание — планирование тренировок, макросы и показатели тела через wger/USDA.
sidebar_label: Fitness Nutrition
description: Планирование тренировок, макросы и показатели тела через wger/USDA
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Фитнес-питание

Планирование тренировок, макросы и показатели тела через wger/USDA.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/health/fitness-nutrition` |
| Путь | `optional-skills/health/fitness-nutrition` |
| Версия | `1.0.0` |
| Автор | Хейли Маршалл (haileymarshall), агент Hermes |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `health`, `fitness`, `nutrition`, `gym`, `workout`, `diet`, `exercise` |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Фитнес и питание

Опытный фитнес-тренер и спортивный диетолог. Два источника данных
плюс офлайн-калькуляторы — все, что нужно любителю тренажерного зала, в одном месте.

**Источники данных (все бесплатно, без зависимостей от pip):**

- **wger** (https://wger.de/api/v2/) — открытая база данных упражнений, более 690 упражнений с мышцами, оборудованием, изображениями. Публичным конечным точкам не требуется нулевая аутентификация.
- **USDA FoodData Central** (https://api.nal.usda.gov/fdc/v1/) — база данных правительства США о питании, более 380 000 продуктов питания. `DEMO_KEY` работает мгновенно; бесплатная регистрация для более высоких лимитов.

**Офлайн-калькуляторы (чистый стандартный Python):**

- ИМТ, TDEE (Миффлин-Сент-Джор), одноповторный максимум (Эпли/Бжицки/Ломбарди), макро-сплиты, % телесного жира (метод ВМС США)

---

## Когда использовать

Активируйте этот навык, когда пользователь спрашивает о:
- Упражнения, тренировки, упражнения в тренажерном зале, группы мышц, сплиты тренировок.
- Пищевые макросы, калории, содержание белка, планирование еды, подсчет калорий.
- Состав тела: ИМТ, жировые отложения, TDEE, избыток/дефицит калорий.
- Оценка одноповторного максимума, процент тренировок, прогрессивная перегрузка.
- Макропропорции для резки, набора массы или ухода.

---

## Процедура

### Поиск упражнений (wger API)

Все общедоступные конечные точки wger возвращают JSON и не требуют аутентификации. Всегда добавлять
`format=json` и `language=2` (на английском языке) для выполнения запросов.

**Шаг 1. Определите, чего хочет пользователь:**

- По мышцам → используйте `/api/v2/exercise/?muscles={id}&language=2&status=2&format=json`
- По категориям → используйте `/api/v2/exercise/?category={id}&language=2&status=2&format=json`
- По оборудованию → используйте `/api/v2/exercise/?equipment={id}&language=2&status=2&format=json`
- По имени → используйте `/api/v2/exercise/search/?term={query}&language=english&format=json`
- Полную информацию → используйте `/api/v2/exerciseinfo/{exercise_id}/?format=json`

**Шаг 2. Ссылочные идентификаторы (чтобы вам не требовались дополнительные вызовы API):**

Категории упражнений:

| удостоверение личности | Категория |
|----|-------------|
| 8 | Оружие |
| 9 | Ноги |
| 10 | Пресс |
| 11 | Сундук |
| 12 | Назад |
| 13 | Плечи |
| 14 | Телята |
| 15 | Кардио |

Мышцы:

| удостоверение личности | Мышцы | удостоверение личности | Мышцы |
|----|---------------------------|----|-------------------------|
| 1 | Двуглавая мышца плеча | 2 | Передняя дельтовидная мышца |
| 3 | Передняя зубчатая мышца | 4 | Большая грудная мышца |
| 5 | Наружная косая мышца | 6 | Икроножные |
| 7 | Прямая мышца живота | 8 | Большая ягодичная мышца |
| 9 | Трапеция | 10 | Четырехглавая мышца бедра |
| 11 | Двуглавая мышца бедра | 12 | Широчайшая мышца спины |
| 13 | Брахиалис | 14 | Трехглавая мышца плеча |
| 15 | Солеус |    |                         |

Оборудование:

| удостоверение личности | Оборудование |
|----|----------------|
| 1 | Штанга |
| 3 | Гантель |
| 4 | Коврик для спортзала |
| 5 | Швейцарский мяч |
| 6 | Перекладина |
| 7 | нет (собственный вес) |
| 8 | Скамейка |
| 9 | Наклонная скамья |
| 10 | Гиря |

**Шаг 3. Получение и представление результатов:**

```bash
# Search exercises by name
QUERY="$1"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$QUERY")
curl -s "https://wger.de/api/v2/exercise/search/?term=${ENCODED}&language=english&format=json" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
for s in data.get('suggestions',[])[:10]:
    d=s.get('data',{})
    print(f\"  ID {d.get('id','?'):>4} | {d.get('name','N/A'):<35} | Category: {d.get('category','N/A')}\")
"
```

```bash
# Get full details for a specific exercise
EXERCISE_ID="$1"
curl -s "https://wger.de/api/v2/exerciseinfo/${EXERCISE_ID}/?format=json" \
  | python3 -c "
import json,sys,html,re
data=json.load(sys.stdin)
trans=[t for t in data.get('translations',[]) if t.get('language')==2]
t=trans[0] if trans else data.get('translations',[{}])[0]
desc=re.sub('<[^>]+>','',html.unescape(t.get('description','N/A')))
print(f\"Exercise  : {t.get('name','N/A')}\")
print(f\"Category  : {data.get('category',{}).get('name','N/A')}\")
print(f\"Primary   : {', '.join(m.get('name_en','') for m in data.get('muscles',[])) or 'N/A'}\")
print(f\"Secondary : {', '.join(m.get('name_en','') for m in data.get('muscles_secondary',[])) or 'none'}\")
print(f\"Equipment : {', '.join(e.get('name','') for e in data.get('equipment',[])) or 'bodyweight'}\")
print(f\"How to    : {desc[:500]}\")
imgs=data.get('images',[])
if imgs: print(f\"Image     : {imgs[0].get('image','')}\")
"
```

```bash
# List exercises filtering by muscle, category, or equipment
# Combine filters as needed: ?muscles=4&equipment=1&language=2&status=2
FILTER="$1"  # e.g. "muscles=4" or "category=11" or "equipment=3"
curl -s "https://wger.de/api/v2/exercise/?${FILTER}&language=2&status=2&limit=20&format=json" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'Found {data.get(\"count\",0)} exercises.')
for ex in data.get('results',[]):
    print(f\"  ID {ex['id']:>4} | muscles: {ex.get('muscles',[])} | equipment: {ex.get('equipment',[])}\")
"
```

### Поиск по питанию (USDA FoodData Central)

Использует переменную окружения `USDA_API_KEY`, если она установлена, в противном случае возвращается к `DEMO_KEY`.
DEMO_KEY = 30 запросов/час. Бесплатный ключ регистрации = 1000 запросов/час.

```bash
# Search foods by name
FOOD="$1"
API_KEY="${USDA_API_KEY:-DEMO_KEY}"
ENCODED=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$FOOD")
curl -s "https://api.nal.usda.gov/fdc/v1/foods/search?api_key=${API_KEY}&query=${ENCODED}&pageSize=5&dataType=Foundation,SR%20Legacy" \
  | python3 -c "
import json,sys
data=json.load(sys.stdin)
foods=data.get('foods',[])
if not foods: print('No foods found.'); sys.exit()
for f in foods:
    n={x['nutrientName']:x.get('value','?') for x in f.get('foodNutrients',[])}
    cal=n.get('Energy','?'); prot=n.get('Protein','?')
    fat=n.get('Total lipid (fat)','?'); carb=n.get('Carbohydrate, by difference','?')
    print(f\"{f.get('description','N/A')}\")
    print(f\"  Per 100g: {cal} kcal | {prot}g protein | {fat}g fat | {carb}g carbs\")
    print(f\"  FDC ID: {f.get('fdcId','N/A')}\")
    print()
"
```

```bash
# Detailed nutrient profile by FDC ID
FDC_ID="$1"
API_KEY="${USDA_API_KEY:-DEMO_KEY}"
curl -s "https://api.nal.usda.gov/fdc/v1/food/${FDC_ID}?api_key=${API_KEY}" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"Food: {d.get('description','N/A')}\")
print(f\"{'Nutrient':<40} {'Amount':>8} {'Unit'}\")
print('-'*56)
for x in sorted(d.get('foodNutrients',[]),key=lambda x:x.get('nutrient',{}).get('rank',9999)):
    nut=x.get('nutrient',{}); amt=x.get('amount',0)
    if amt and float(amt)>0:
        print(f\"  {nut.get('name',''):<38} {amt:>8} {nut.get('unitName','')}\")
"
```

### Офлайн-калькуляторы

Используйте вспомогательные сценарии в `scripts/` для пакетных операций,
или запустите встроенный режим для отдельных вычислений:

- `python3 scripts/body_calc.py bmi <weight_kg> <height_cm>`
- `python3 scripts/body_calc.py tdee <weight_kg> <height_cm> <age> <M|F> <activity 1-5>`
- `python3 scripts/body_calc.py 1rm <weight> <reps>`
- `python3 scripts/body_calc.py macros <tdee_kcal> <cut|maintain|bulk>`
- `python3 scripts/body_calc.py bodyfat <M|F> <neck_cm> <waist_cm> [hip_cm] <height_cm>`

См. `references/FORMULAS.md`, чтобы узнать о научных обоснованиях каждой формулы.

---

## Подводные камни

- конечная точка упражнения wger возвращает **все языки по умолчанию** — всегда добавляйте `language=2` для английского языка.
- wger включает **непроверенные пользовательские материалы** — добавьте `status=2`, чтобы получать только одобренные упражнения.
- USDA `DEMO_KEY` имеет **30 запросов в час** — добавьте `sleep 2` между пакетными запросами или получите бесплатный ключ
– Данные Министерства сельского хозяйства США указаны **на 100 г** — напоминайте пользователям о необходимости масштабирования до фактического размера порции.
- ИМТ не отличает мышцы от жира: высокий ИМТ у мускулистых людей не обязательно вреден для здоровья.
- Формулы содержания жира в организме являются **оценочными** (±3–5%) — для точности рекомендуется использовать сканирование DEXA.
- Формулы 1ПМ теряют точность после 10 повторений — для получения наилучших оценок используйте подходы по 3–5 повторений.
- конечная точка `exercise/search` wger использует `term`, а не `query` в качестве имени параметра.

---

## Проверка

После запуска поиска упражнений: результаты подтверждения включают названия упражнений, группы мышц и оборудование.
После просмотра питания: убедитесь, что макросы на 100 г возвращаются с ккал, белками, жирами и углеводами.
После калькуляторов: результаты проверки работоспособности (например, TDEE должно составлять 1500–3500 для большинства взрослых).

---

## Краткий справочник

| Задача | Источник | Конечная точка |
|------|--------|----------|
| Поиск упражнений по названию | вгер | `GET /api/v2/exercise/search/?term=&language=english` |
| Подробности упражнения | вгер | `GET /api/v2/exerciseinfo/{id}/` |
| Фильтровать по мышцам | вгер | `GET /api/v2/exercise/?muscles={id}&language=2&status=2` |
| Фильтровать по оборудованию | вгер | `GET /api/v2/exercise/?equipment={id}&language=2&status=2` |
| Список категорий | вгер | `GET /api/v2/exercisecategory/` |
| Список мышц | вгер | `GET /api/v2/muscle/` |
| Поиск продуктов | Министерство сельского хозяйства США | `GET /fdc/v1/foods/search?query=&dataType=Foundation,SR Legacy` |
| Детали еды | Министерство сельского хозяйства США | `GET /fdc/v1/food/{fdcId}` |
| ИМТ/TDEE/1ПМ/макросы | оффлайн | `python3 scripts/body_calc.py` |