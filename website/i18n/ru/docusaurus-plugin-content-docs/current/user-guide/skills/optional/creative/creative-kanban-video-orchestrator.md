---
title: Kanban Video Orchestrator — планирование и запуск многоагентных конвейеров
  видеопроизводства.
sidebar_label: Kanban Video Orchestrator
description: Планирование и запуск многоагентных конвейеров видеопроизводства
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Канбан-видео оркестратор

Планируйте и запускайте конвейеры мультиагентного производства видео.

## Метаданные навыков

| | |
|---|---|
| Источник | Необязательно — установите с помощью `hermes skills install official/creative/kanban-video-orchestrator` |
| Путь | `optional-skills/creative/kanban-video-orchestrator` |
| Версия | `1.0.0` |
| Автор | ['SHL0MS', 'альтернативный глюк'] |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `video`, `kanban`, `multi-agent`, `orchestration`, `production-pipeline` |
| Сопутствующие навыки | [`ascii-video`](/docs/user-guide/skills/bundled/creative/creative-ascii-video), [`manim-video`](/docs/user-guide/skills/bundled/creative/creative-manim-video), [`p5js`](/docs/user-guide/skills/bundled/creative/creative-p5js), [`comfyui`](/docs/user-guide/skills/bundled/creative/creative-comfyui), [`touchdesigner-mcp`](/docs/user-guide/skills/bundled/creative/creative-touchdesigner-mcp), [`pixel-art`](/docs/user-guide/skills/optional/creative/creative-pixel-art), [`ascii-art`](/docs/user-guide/skills/bundled/creative/creative-ascii-art), [`songwriting-and-ai-music`](/docs/user-guide/skills/bundled/creative/creative-songwriting-and-ai-music), [`heartmula`](/docs/user-guide/skills/optional/creative/creative-heartmula), [`songsee`](/docs/user-guide/skills/bundled/media/media-songsee), [`youtube-content`](/docs/user-guide/skills/bundled/media/media-youtube-content), [`claude-design`](/docs/user-guide/skills/bundled/creative/creative-claude-design), [`excalidraw`](/docs/user-guide/skills/bundled/creative/creative-excalidraw), [`architecture-diagram`](/docs/user-guide/skills/bundled/creative/creative-architecture-diagram), [`concept-diagrams`](/docs/user-guide/skills/optional/creative/creative-concept-diagrams), [`baoyu-comic`](/docs/user-guide/skills/optional/creative/creative-baoyu-comic), [`baoyu-infographic`](/docs/user-guide/skills/bundled/creative/creative-baoyu-infographic), [`humanizer`](/docs/user-guide/skills/bundled/creative/creative-humanizer), [`gif-search`](/docs/user-guide/skills/bundled/media/media-gif-search), [`meme-generation`](/docs/user-guide/skills/optional/creative/creative-meme-generation) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Канбан-видео оркестратор

Оформите любой видеозапрос — от 15-секундного тизера продукта до 5-минутного повествования.
сокращение музыкального видео до цикла ASCII — в конвейере Hermes Kanban, который
разлагает работу на специализированные профили агентов.

Этот навык сам по себе **не** ничего не визуализирует. Это метаконвейер, который:

1. **Определяет** запрос посредством целевого обнаружения.
2. **Разрабатывает** соответствующую команду (какие роли, какие инструменты для каждой роли) на основе стиля
3. **Создает** сценарий установки, который создает профили Hermes, рабочее пространство проекта и первоначальную задачу канбана.
4. **Руки прочь** к профилю директора, который разлагается по канбану
5. **Отслеживает** выполнение, помогает вмешаться, когда задачи останавливаются или выходят из строя.

Фактический рендеринг происходит внутри канбана, как только он запускается, в зависимости от того,
существующие навыки + инструменты соответствуют сценам — `ascii-video`, `manim-video`, `p5js`,
`comfyui`, `touchdesigner-mcp`, `songwriting-and-ai-music`,
`heartmula`, внешние API или простой Python с PIL + ffmpeg.

## Когда НЕ использовать этот навык

— Видео — это один непрерывный процедурный проект, не нуждающийся в специалистах. Просто напишите код напрямую.
- Пользователь хочет выполнить быстрое однократное преобразование (например, «конвертировать этот mp4 в GIF») — напрямую используйте ffmpeg.
– Выходные данные представляют собой статическое изображение, GIF-файл или артефакт, содержащий только аудио. Используйте соответствующий специальный навык (`ascii-art`, `gifs`, `meme-generation`, `songwriting-and-ai-music`).
- Работа полностью соответствует одному существующему навыку (например, видео в чистом формате ASCII — просто используйте `ascii-video`).

## Рабочий процесс

```
DISCOVER  →  BRIEF  →  TEAM DESIGN  →  SETUP  →  EXECUTE  →  MONITOR
```

### Шаг 1 — Узнайте (задайте правильные вопросы)

Процесс открытия является **адаптивным**: спрашивайте только то, что действительно необходимо. Всегда
начните с трех вопросов, чтобы определить общую форму:

– **Что это за видео?** (краткое предложение из одного предложения)
- **Как долго?** (тизер 5–30 секунд, короткометражка 30–90 секунд, пояснение 90–3 минуты, фильм 3–10 минут, более длинный)
– **Какое соотношение сторон + целевая платформа?** (1:1 / 9:16 / 16:9; X, IG, YouTube, внутреннее и т. д.)

По ответу классифицируйте категорию стиля. Стиль определяет, какой
дополнительные вопросы, которые следует задать. **Не задавайте все вопросы сразу.** Задавайте 2–4 вопроса за раз.
время, послушайте, а затем продолжайте. Делайте разумные предположения всякий раз, когда пользователь
подразумевает ответ.

Полные схемы приема и банки вопросов по каждому стилю см.
**[references/intake.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/kanban-video-orchestrator/references/intake.md)**.

### Шаг 2. Краткое описание

Как только станет известно достаточно, создайте структурированный `brief.md`, используя шаблон в
`assets/brief.md.tmpl`. Этапы:

1. **Концепция** – презентация из одного предложения + эмоциональная северная звезда.
2. **Объем** — продолжительность, аспект, платформа, крайний срок.
3. **Стиль** — визуальные ориентиры, ограничения бренда, тон.
4. **Сцены** — разбивка по тактам (длительность, содержание, целевой инструмент)
5. **Аудио** — повествование/музыка/звуковые эффекты/без звука (при необходимости для каждой сцены)
6. **Результаты** — формат файла, разрешение, дополнительные альтернативы (вертикальный разрез, GIF и т. д.).

Покажите бриф пользователю для подтверждения перед проектированием команды. **
Контракт краток** — каждая последующая задача ссылается на него.

### Шаг 3 — Формирование команды

Выберите из библиотеки ролевые архетипы, подходящие к этому видео. **Сочиняй, не надо
клон.** Для большинства видео требуется 4–7 профилей. Директор всегда присутствует; тот
остальные выбираются в соответствии с тем, что на самом деле требуется в задании.

Информацию о библиотеке ролей и составах команд по каждому стилю см.
**[references/role-archetypes.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/kanban-video-orchestrator/references/role-archetypes.md)**.

Информацию о роли сопоставления → какие навыки и наборы инструментов Hermes он загружает, см.
**[references/tool-matrix.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/kanban-video-orchestrator/references/tool-matrix.md)**.

### Шаг 4 — Настройка

Создайте сценарий установки (`setup.sh`) и запустите его. Сценарий:

1. Создает рабочую область проекта (`~/projects/video-pipeline/<slug>/`).
2. Копирует все предоставленные ресурсы в `taste/`, `audio/`, `assets/`.
3. Создает каждый профиль Hermes через `hermes profile create --clone`.
4. Записывает для каждого профиля `SOUL.md` (личность + определение роли)
5. Настраивает профиль YAML (наборы инструментов, навыки Always_load, CWD).
6. Записывает контент `brief.md`, `TEAM.md` и `taste/`.
7. Запускает первоначальную задачу `hermes kanban create`, назначенную директору.

Используйте `scripts/bootstrap_pipeline.py` для создания файла setup.sh из краткого описания +
командный дизайн JSON. См. **[references/kanban-setup.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/kanban-video-orchestrator/references/kanban-setup.md)**
для структуры сценария установки, шаблонов конфигурации профиля и критических
правило «общего рабочего пространства».

### Шаг 5 — Выполнение

Запустите `setup.sh`. Затем предоставьте пользователю команды мониторинга:

```bash
hermes kanban watch --tenant <project-tenant>     # live events
hermes kanban list  --tenant <project-tenant>     # board snapshot
hermes dashboard                                   # visual board UI
```

Отсюда берет верх профиль директора, декомпозируя работу и маршрутизируя ее.
задачи для профилей специалистов с помощью набора инструментов Канбан.

### Шаг 6. Мониторинг и вмешательство

Оставайтесь вовлеченными — канбан работает автономно, но задача зависла или результат плохой.
нуждается в человеческом (или ИИ) суждении.

Шаблоны мониторинга: периодически опрашивайте `kanban list`, проверяйте любую ВЫПОЛНЯЕМУЮ задачу.
который превышает ожидаемую продолжительность с `kanban show <id>`, и проверьте
сердцебиение. Если результаты работы работника не проходят проверку, стандартными мерами являются:

1. Прокомментируйте задачу работника, оставив конкретную обратную связь (`kanban_comment`).
2. Создайте задачу повторного запуска, сделав исходную задачу родительской.
3. Скорректируйте объем задания и позвольте режиссеру заново его разложить.

Диагностические шаблоны, рецепты вмешательства и «задача застряла»
книгу, см. **[references/monitoring.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/kanban-video-orchestrator/references/monitoring.md)**.

## Ссылка: проработанные примеры

Шесть конкретных конвейеров, охватывающих самые разные стили видео — повествовательный фильм,
продукт/маркетинг, музыкальное видео, объяснение математики/алгоритмов, видео ASCII, режим реального времени
установка — показывает, как один и тот же рабочий процесс приводит к появлению очень разных команд и
графики задач. См. **[references/examples.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/creative/kanban-video-orchestrator/references/examples.md)**.

## Критические правила

1. **Открытие перед действием.** Никогда не начинайте составлять задание или команду без
   задавая как минимум три основных вопроса. Плохое краткое описание каскадом проходит через
   весь трубопровод.

2. **Сопоставьте команду с видео.** Не используйте повторно одну и ту же настройку из 4 профилей для
   каждая работа. Музыкальное видео, не имеющее профиля бит-анализа, будет
   осечка. Повествовательный фильм, у которого нет профиля писателя, будет производиться
   бессвязные сцены. См. `references/role-archetypes.md`.

3. **Одно рабочее пространство для каждого проекта.** Все профили для данного видео используются одинаково.
   `dir:` рабочая область. Задачи передают артефакты через общую файловую систему и структурируют
   передача. **Каждый** вызов `kanban_create` проходит
   `workspace_kind="dir"` + `workspace_path="<absolute project path>"`.

4. **Арендуйте каждый проект.** Используйте арендатора для конкретного проекта.
   (`--tenant <project-slug>`). Сохраняет область действия панели мониторинга и предотвращает
   перекрестное опыление с другими текущими канбанами.

5. **Уважайте имеющиеся навыки.** Если сцена соответствует существующим навыкам,
   соответствующий рендерер должен загрузить этот навык через `--skill <name>` в своей задаче
   или `always_load` в своем профиле. Не перезанимайте тот навык, который уже есть
   обеспечивает.

6. **Директор никогда не исполняет.** Даже при полном канбане + терминале +
   правила file` toolset, the director's `SOUL.md` запрещают его выполнение
   сама работа. Он лишь разлагает и маршрутизирует — каждая конкретная задача становится
   `hermes kanban create` звонок профильному специалисту. Канбан
   руководство по оркестрации автоматически внедряется в систему каждого канбан-работника
   подсказка поясняет это дальше.

7. **Не переусердствуйте.** Для 30-секундного видео о продукте НЕ требуется 20 задач.
   Стремитесь к наименьшему графу задач, который по-прежнему хорошо распараллеливается и раскрывает возможности
   правые ворота человеческого обзора.

8. **Проверяйте ключи API ПЕРЕД активацией.** Внешние API (TTS, image-gen,
   преобразование изображения в видео) нужны ключи в `${HERMES_HOME:-~/.hermes}/.env` или секретном хранилище пользователя.
   Работник, обнаруживший ошибку отсутствия ключа, теряет слот задачи. Установка
   Помощник сценария `check_key` автоматически прерывает работу, если требуемый ключ отсутствует.

## Карта файлов

```
SKILL.md                            ← this file (workflow + rules)
references/
  intake.md                         ← discovery question banks per style
  role-archetypes.md                ← role library (writer, designer, animator, …)
  tool-matrix.md                    ← skill + toolset mapping per role
  kanban-setup.md                   ← setup script structure & profile config
  monitoring.md                     ← watch + intervene patterns
  examples.md                       ← six worked pipelines
assets/
  brief.md.tmpl                     ← brief skeleton
  setup.sh.tmpl                     ← setup script skeleton
  soul.md.tmpl                      ← profile personality skeleton
scripts/
  bootstrap_pipeline.py             ← generate setup.sh from brief + team JSON
  monitor.py                        ← polling + intervention helpers
```