---
title: Darwinian Evolver — развивайте подсказки/регулярные выражения/SQL/код с помощью
  цикла эволюции Imbue.
sidebar_label: Darwinian Evolver
description: Развивайте запросы/регулярные выражения/SQL/код с помощью цикла эволюции
  Imbue
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Дарвиновский Эволвер

Развивайте запросы/регулярные выражения/SQL/код с помощью цикла эволюции Imbue.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/research/darwinian-evolver` |
| Путь | `optional-skills/research/darwinian-evolver` |
| Версия | `0.1.0` |
| Автор | Бихрузе (Asahi0x), агент Гермеса |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS |
| Теги | `evolution`, `optimization`, `prompt-engineering`, `research` |
| Сопутствующие навыки | [`arxiv`](/docs/user-guide/skills/bundled/research/research-arxiv), [`jupyter-notebook`](/docs/user-guide/skills/optional/data-science/data-science-jupyter-notebook) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Дарвиновский Эволвер

Запустите [darwinian_evolver] от Imbue (https://github.com/imbue-ai/darwinian_evolver) —
Эволюционный цикл поиска на основе LLM — для оптимизации **подсказок, регулярных выражений, SQL-запросов,
или небольшой фрагмент кода** для фитнес-функции.

Статус: тонкая оболочка вокруг вышестоящего инструмента. Навык устанавливает его, ходит по
агент путем написания определения `Problem` (организм + оценщик + мутатор),
и управляет циклом через вышестоящий интерфейс командной строки или небольшой специальный драйвер Python.

**Лицензия:** исходный инструмент — **AGPL-3.0**. Навык ТОЛЬКО когда-либо вызывает его.
через восходящий интерфейс командной строки или вызов `subprocess`/`uv run` (простая агрегация). НЕ
импортируйте вышестоящие классы в сам Hermes.

## Когда использовать

– Пользователь говорит «оптимизируйте это приглашение», «разработайте регулярное выражение для X», «автоматически улучшите это».
  code/SQL", "поищите лучшую инструкцию".
- У вас есть бомбардир (точное совпадение, проходимость регулярных выражений, модульный тест, судья LLM, время выполнения
  метрика) И стартовый кандидат (организм). Если у вас нет бомбардира, остановитесь
  и сначала определите его — это самая сложная часть.
- Стоимость приемлемая: типичный пробег составляет 50–500 звонков LLM. На gpt-4o-mini это копейки;
  на Клоде Сонете это может быть несколько долларов.

**Не** используйте это, если:
- Цель оптимизации дифференцируема (используйте градиентный спуск/DSPy).
— Вам нужно всего лишь попробовать 2–3 варианта — просто напишите их от руки.
- Сигнал пригодности является чисто субъективным и не имеет поддающихся измерению критериев.

## Предварительные условия

- Питон ≥3.11
- `git`, `uv` (или `pip`)
– Один из: `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY` или `OPENAI_API_KEY`.

Навык включает в себя небольшой драйвер `parrot_openrouter.py`, использующий `OPENROUTER_API_KEY`.
через OpenAI SDK, поэтому любая модель на OpenRouter работает. Сам восходящий CLI
хардкоды Anthropic и требуют `ANTHROPIC_API_KEY`.

## Установка (однократно)

Запустите с помощью инструмента `terminal`:

```bash
mkdir -p ~/.hermes/cache/darwinian-evolver && cd ~/.hermes/cache/darwinian-evolver
[ -d darwinian_evolver ] || git clone --depth 1 https://github.com/imbue-ai/darwinian_evolver.git
cd darwinian_evolver && uv sync
```

Проверьте:

```bash
cd ~/.hermes/cache/darwinian-evolver/darwinian_evolver \
  && uv run darwinian_evolver --help | head -5
```

## Быстрый старт — пример встроенного Parrot

Небольшой дымовой тест (требуется `ANTHROPIC_API_KEY`):

```bash
cd ~/.hermes/cache/darwinian-evolver/darwinian_evolver
uv run darwinian_evolver parrot \
  --num_iterations 2 \
  --num_parents_per_iteration 2 \
  --mutator_concurrency 2 --evaluator_concurrency 2 \
  --output_dir /tmp/parrot_demo
```

Выходы:
- `/tmp/parrot_demo/snapshots/iteration_N.pkl` — маринованная популяция на итерацию
- `/tmp/parrot_demo/<jsonl>` — журнал JSON для каждой итерации (путь указан в конце)

Открыть `~/.hermes/cache/darwinian-evolver/darwinian_evolver/darwinian_evolver/lineage_visualizer.html`
в браузере и загрузите журнал JSON, чтобы увидеть эволюционное дерево.

## Быстрый старт — драйвер OpenRouter (без антропного ключа)

Навык включает `scripts/parrot_openrouter.py` — та же проблема с попугаем, но
Вызов LLM проходит через OpenRouter, поэтому работает любой провайдер.

```bash
# From wherever the skill is installed:
SKILL_DIR=~/.hermes/skills/research/darwinian-evolver
DE_DIR=~/.hermes/cache/darwinian-evolver/darwinian_evolver

cd "$DE_DIR" && \
  EVOLVER_MODEL='openai/gpt-4o-mini' \
  uv run --with openai python "$SKILL_DIR/scripts/parrot_openrouter.py" \
    --num_iterations 3 --num_parents_per_iteration 2 \
    --output_dir /tmp/parrot_or
```

Проверьте результат с помощью `scripts/show_snapshot.py`:

```bash
uv run --with openai python "$SKILL_DIR/scripts/show_snapshot.py" \
  /tmp/parrot_or/snapshots/iteration_3.pkl
```

Ожидаемый результат: 7 усовершенствованных шаблонов подсказок, ранжированных по баллам, лучший из которых
приземление около 0,6–0,8 (семя `Say {{ phrase }}` набрало 0,000).

## Определение пользовательской проблемы

Навык поставляется `templates/custom_problem_template.py` — копирование, редактирование, запуск.
Три вещи, которые вы должны определить:

1. **`Organism`** — подкласс Pydantic `BaseModel`, содержащий артефакт
   эволюционировали (`prompt_template: str`, `regex_pattern: str`, `sql_query: str`,
   `code_block: str` и т. д.). Добавьте метод `run(*args)`, который его выполняет.

2. **`Evaluator`** — `.evaluate(organism) -> EvaluationResult(score=..., trainable_failure_cases=[...], holdout_failure_cases=[...], is_viable=True)`.
   - **`score`** находится в `[0, 1]`. Чем выше, тем лучше.
   - **`trainable_failure_cases`** — то, что видит мутатор. Включите достаточно
     контекст (входной, ожидаемый, фактический) для диагностики LLM.
   - **`holdout_failure_cases`** — хранится вне поля зрения мутатора. Используйте эти
     для обнаружения переобучения.
   - **`is_viable=True`**, если организм полностью не сломан (поднимает,
     возвращает None и т. д.). Жизнеспособный организм с нулевым баллом — это нормально, он просто становится
     снижен вес в родительском выборе.

3. **`Mutator`** — `.mutate(organism, failure_cases, learning_log_entries) -> list[Organism]`.
   Обычно: создайте приглашение LLM, включающее текущий организм +
   случай сбоя + просьба предложить исправление; проанализировать ответ LLM; возвращение
   новый `Organism`. Возвращает `[]` при ошибке синтаксического анализа — это обрабатывает цикл.

Затем напишите сценарий драйвера, который подключает `Problem(initial_organism, evaluator, [mutators])`
в `EvolveProblemLoop` и перебирает `loop.run(num_iterations=N)` —
отправлен `scripts/parrot_openrouter.py` является ссылкой.

## Гиперпараметры, которые действительно имеют значение

| флаг | по умолчанию | когда менять |
|---|---|---|
| `--num_iterations` | 5 | увеличьте до 10–20, если доверяете оценщику |
| `--num_parents_per_iteration` | 4 | падение до 2 для дешевой разведки |
| `--mutator_concurrency` | 10 | уменьшите до 2–4, чтобы избежать ограничений по ставкам |
| `--evaluator_concurrency` | 10 | такой же; оценщик тоже попадает в магистратуру |
| `--batch_size` | 1 | поднимите до 3–5, как только ваш мутатор обработает несколько ошибок |
| `--verify_mutations` | выключен | включать, когда мутатор становится ненужным (>10-кратная экономия затрат при последующих запусках за Imbue) |
| `--midpoint_score` | `p75` | оставить в покое, если не наберет кластер |
| `--sharpness` | 10 | оставить в покое |

## Подводные камни

1. **`Initial organism must be viable`** — установите `is_viable=True` в вашем
   `EvaluationResult` даже при нулевом результате. Цикл отказывается от нежизнеспособности
   организмы, потому что они подразумевают, что петле не из чего развиваться.
2. **Фильтры содержимого поставщика уничтожают прогоны.** Модели OpenRouter на базе Azure.
   отклонять фразы типа «игнорировать предыдущие инструкции» с помощью HTTP 400. Wrap
   LLM вызывает `try/except` и возвращает `f"<LLM_ERROR: {e}>"` —
   Эволюционер просто наберет этому организму 0 баллов и пойдет дальше.
3. **`loop.run()` — генератор** — его вызов ничего не запускает до тех пор, пока
   вы повторяете. Используйте `for snap in loop.run(num_iterations=N):`.
4. **Снимки представляют собой вложенные пиклусы.** `iteration_N.pkl` содержит текст с
   `population_snapshot` (еще больше маринованных байтов). Для расмариновки необходимо иметь
   `Organism` класс, который можно импортировать по тому же пунктирному пути, по которому он был консервирован.
5. **Настройки параллелизма по умолчанию являются агрессивными.** 10/10 достигают ограничений скорости
   большинство провайдеров. Начните с 2/2.
6. **CLI жестко запрограммирован в Anthropic.** `uv run darwinian_evolver <problem>`
   тянется к `ANTHROPIC_API_KEY` и использует Claude Sonnet. Чтобы использовать любой другой
   провайдера, напишите драйвер типа `parrot_openrouter.py`.
7. **AGPL.** Никогда не `from darwinian_evolver import ...` внутри ядра Гермеса.
   Сценарии пользовательских драйверов под `~/.hermes/skills/...` подходят для пользователя и подходят для них.
8. **Пакет PyPI отсутствует.** `pip install darwinian-evolver` будет загружаться неправильно.
   вещь. Всегда устанавливайте из репозитория GitHub.

## Проверка

После установки + запуска попугая достаточно кода выхода 0:

```bash
DE_DIR=~/.hermes/cache/darwinian-evolver/darwinian_evolver
ls "$DE_DIR/darwinian_evolver/lineage_visualizer.html" >/dev/null && \
cd "$DE_DIR" && uv run darwinian_evolver --help >/dev/null && \
echo "darwinian-evolver: OK"
```

## Ссылки

- [Сообщение об исследовании Imbue](https://imbue.com/research/2026-02-27-darwinian-evolver/)
- [Результаты ARC-AGI-2](https://imbue.com/research/2026-02-27-arc-agi-2-evolution/)
- [imbue-ai/darwinian_evolver](https://github.com/imbue-ai/darwinian_evolver) (AGPL-3.0)
- [Машины Дарвина Гёделя] (https://arxiv.org/abs/2505.22954)
- [PromptBreeder](https://arxiv.org/abs/2309.16797)