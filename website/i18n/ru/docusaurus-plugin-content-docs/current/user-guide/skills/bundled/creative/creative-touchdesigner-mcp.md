---
title: Touchdesigner Mcp — управление TouchDesigner через twozero MCP
sidebar_label: Touchdesigner Mcp
description: Управление TouchDesigner через twozero MCP
---

{/* Эта страница автоматически создается на основе файла SKILL.md навыка с помощью сайта site/scripts/generate-skill-docs.py. Редактируйте исходный код SKILL.md, а не эту страницу. */}

# Touchdesigner Mcp

Управляйте TouchDesigner через twozero MCP.

## Метаданные навыков

| | |
|---|---|
| Источник | В комплекте (устанавливается по умолчанию) |
| Путь | `skills/creative/touchdesigner-mcp` |
| Версия | `1.1.0` |
| Автор | кшитийк4бедный |
| Лицензия | Массачусетский технологический институт |
| Платформы | Linux, MacOS, Windows |
| Теги | `TouchDesigner`, `MCP`, `twozero`, `creative-coding`, `real-time-visuals`, `generative-art`, `audio-reactive`, `VJ`, `installation`, `GLSL` |
| Сопутствующие навыки | [`ascii-video`](/docs/user-guide/skills/bundled/creative/creative-ascii-video), [`manim-video`](/docs/user-guide/skills/bundled/creative/creative-manim-video) |

## Ссылка: полная версия SKILL.md

:::информация
Ниже приведено полное определение навыка, которое Гермес загружает при активации этого навыка. Это то, что агент видит в качестве инструкций, когда навык активен.
:::

# Интеграция TouchDesigner (два нуля MCP)

## ВАЖНЫЕ ПРАВИЛА

1. **НИКОГДА не угадывайте имена параметров.** Вызовите `td_get_par_info` для типа операции ПЕРВЫМ. Ваши данные обучения неверны для TD 2025.32.
2. **Если срабатывает `tdAttributeError`, ОСТАНОВИТЕСЬ.** Прежде чем продолжить, вызовите `td_get_operator_info` на неисправном узле.
3. **НИКОГДА не прописывайте абсолютные пути** в обратных вызовах скриптов. Используйте `me.parent()`/`scriptOp.parent()`.
4. **Предпочитайте собственные инструменты MCP, а не td_execute_python.** Используйте `td_create_operator`, `td_set_operator_pars`, `td_get_errors` и т. д. Возвращайтесь к `td_execute_python` только для сложной многошаговой логики.
5. **Вызовите `td_get_hints` перед созданием.** Он возвращает шаблоны, специфичные для типа операции, с которой вы работаете.

## Архитектура

```
Hermes Agent -> MCP (Streamable HTTP) -> twozero.tox (port 40404) -> TD Python
```

36 встроенных инструментов. Бесплатный плагин (без оплаты/лицензии — подтверждено в апреле 2026 г.).
Контекстно-зависимый (знает выбранный OP, текущую сеть).
Проверка работоспособности хаба: `GET http://localhost:40404/mcp` возвращает JSON с PID экземпляра, названием проекта и версией TD.

## Настройка (автоматическая)

Запустите сценарий установки, чтобы все обработать:

```bash
bash "${HERMES_HOME:-$HOME/.hermes}/skills/creative/touchdesigner-mcp/scripts/setup.sh"
```

Скрипт будет:
1. Проверьте, работает ли ТД
2. Загрузите twozero.tox, если он еще не закеширован.
3. Добавьте сервер `twozero_td` MCP в конфигурацию Hermes (если отсутствует)
4. Проверьте соединение MCP на порту 40404.
5. Сообщите, какие действия вручную остались (перетащите .tox в TD, включите переключатель MCP)

### Действия, выполняемые вручную (однократно, не могут быть автоматизированы)

1. **Перетащите `~/Downloads/twozero.tox` в редактор сети TD** → нажмите «Установить».
2. **Включить MCP:** щелкните значок «два нуля» → «Настройки» → «MCP» → «автозапуск MCP» → «Да».
3. **Перезапустите сеанс Hermes**, чтобы подключить новый сервер MCP.

После настройки проверьте:
```bash
nc -z 127.0.0.1 40404 && echo "twozero MCP: READY"
```

## Примечания к среде

- **Некоммерческое TD** ограничивает разрешение 1280×1280. Используйте `outputresolution = 'custom'` и явно задайте ширину/высоту.
– **Кодеки:** `prores` (предпочтительно в macOS) или `mjpa` в качестве резервного варианта. Для H.264/H.265/AV1 требуется коммерческая лицензия.
- Всегда вызывайте `td_get_par_info` перед установкой параметров — имена различаются в зависимости от версии TD (см. КРИТИЧЕСКИЕ ПРАВИЛА №1).

## Рабочий процесс

### Шаг 0: Откройте для себя (прежде чем что-либо создавать)

```
Call td_get_par_info with op_type for each type you plan to use.
Call td_get_hints with the topic you're building (e.g. "glsl", "audio reactive", "feedback").
Call td_get_focus to see where the user is and what's selected.
Call td_get_network to see what already exists.
```

Никаких временных узлов, никакой очистки. Это полностью заменяет старый танец открытия.

### Шаг 1: Очистка + сборка

**ВАЖНО: Разделите очистку и создание на ОТДЕЛЬНЫЕ вызовы MCP.** Уничтожение и воссоздание узлов с одинаковыми именами в одном скрипте `td_execute_python` вызывает ошибку «Недопустимый объект OP». См. подводные камни № 11b.

Используйте `td_create_operator` для каждого узла (автоматически обрабатывает позиционирование области просмотра):

```
td_create_operator(type="noiseTOP", parent="/project1", name="bg", parameters={"resolutionw": 1280, "resolutionh": 720})
td_create_operator(type="levelTOP", parent="/project1", name="brightness")
td_create_operator(type="nullTOP", parent="/project1", name="out")
```

Для массового создания или связывания используйте `td_execute_python`:

```python
# td_execute_python script:
root = op('/project1')
nodes = []
for name, optype in [('bg', noiseTOP), ('fx', levelTOP), ('out', nullTOP)]:
    n = root.create(optype, name)
    nodes.append(n.path)
# Wire chain
for i in range(len(nodes)-1):
    op(nodes[i]).outputConnectors[0].connect(op(nodes[i+1]).inputConnectors[0])
result = {'created': nodes}
```

### Шаг 2: Установите параметры

Предпочитайте собственный инструмент (проверяет параметры, не дает сбоев):

```
td_set_operator_pars(path="/project1/bg", parameters={"roughness": 0.6, "monochrome": true})
```

Для выражений или режимов используйте `td_execute_python`:

```python
op('/project1/time_driver').par.colorr.expr = "absTime.seconds % 1000.0"
```

### Шаг 3: Проволока

Используйте `td_execute_python` — встроенного инструмента для проволоки не существует:

```python
op('/project1/bg').outputConnectors[0].connect(op('/project1/fx').inputConnectors[0])
```

### Шаг 4. Проверьте

```
td_get_errors(path="/project1", recursive=true)
td_get_perf()
td_get_operator_info(path="/project1/out", detail="full")
```

### Шаг 5: Отображение/захват

```
td_get_screenshot(path="/project1/out")
```

Или откройте окно с помощью скрипта:

```python
win = op('/project1').create(windowCOMP, 'display')
win.par.winop = op('/project1/out').path
win.par.winw = 1280; win.par.winh = 720
win.par.winopen.pulse()
```

## Краткое руководство по инструменту MCP

**Основное (используйте чаще всего):**
| Инструмент | Что |
|------|------|
| `td_execute_python` | Запустите произвольный Python в TD. Полный доступ к API. |
| `td_create_operator` | Создать узел с параметрами + автопозиционирование |
| `td_set_operator_pars` | Безопасная установка параметров (проверка, отсутствие сбоев) |
| `td_get_operator_info` | Проверьте один узел: соединения, параметры, ошибки |
| `td_get_operators_info` | Проверка нескольких узлов за один вызов |
| `td_get_network` | См. структуру сети по пути |
| `td_get_errors` | Рекурсивный поиск ошибок/предупреждений |
| `td_get_par_info` | Получить имена параметров для типа OP (заменяет обнаружение) |
| `td_get_hints` | Получите выкройки/советы перед сборкой |
| `td_get_focus` | Какая сеть открыта, что выбрано |

**Чтение/запись:**
| Инструмент | Что |
|------|------|
| `td_read_dat` | Чтение текстового содержимого DAT |
| `td_write_dat` | Запись/исправление содержимого DAT |
| `td_read_chop` | Чтение значений канала CHOP |
| `td_read_textport` | Чтение вывода консоли TD |

**Визуальное изображение:**
| Инструмент | Что |
|------|------|
| `td_get_screenshot` | Захватить одного зрителя OP в файл |
| `td_get_screenshots` | Захват нескольких ОП одновременно |
| `td_get_screen_screenshot` | Захват фактического экрана через TD |
| `td_navigate_to` | Перейти к сетевому редактору в ОП |

**Поиск:**
| Инструмент | Что |
|------|------|
| `td_find_op` | Поиск операций по имени/типу в проекте |
| `td_search` | Поиск кода, выражений, строковых параметров |

**Система:**
| Инструмент | Что |
|------|------|
| `td_get_perf` | Профилирование производительности (FPS, медленные операции) |
| `td_list_instances` | Список всех запущенных экземпляров TD |
| `td_get_docs` | Подробная документация по теме TD |
| `td_agents_md` | Чтение/запись документации по уценке для каждого COMP |
| `td_reinit_extension` | Перезагрузить расширение после редактирования кода |
| `td_clear_textport` | Очистить консоль перед сеансом отладки |

**Автоматизация ввода:**
| Инструмент | Что |
|------|------|
| `td_input_execute` | Отправить мышь/клавиатуру в TD |
| `td_input_status` | Статус очереди ввода опроса |
| `td_input_clear` | Остановить автоматизацию ввода |
| `td_op_screen_rect` | Получить экранные координаты узла |
| `td_click_screen_point` | Щелкните точку на скриншоте |
| `td_screen_point_to_global` | Преобразование пикселей скриншота в абсолютные координаты экрана |

В таблице выше представлены 32 инструмента, используемых в типичных творческих рабочих процессах. Остальные 4 инструмента (`td_project_quit`, `td_test_session`, `td_dev_log`, `td_clear_dev_log`) являются утилитами режима администратора/разработчика — полный справочник по 36 инструментам с полными схемами параметров см. в `references/mcp-tools.md`.

## Ключевые правила реализации

**Время GLSL:** Нет `uTDCurrentTime` в GLSL TOP. Используйте страницу «Значения»:
```python
# Call td_get_par_info(op_type="glslTOP") first to confirm param names
td_set_operator_pars(path="/project1/shader", parameters={"value0name": "uTime"})
# Then set expression via script:
# op('/project1/shader').par.value0.expr = "absTime.seconds"
# In GLSL: uniform float uTime;
```

Резервный вариант: константа TOP в формате `rgba32float` (8-бит фиксируется на 0–1, замораживая шейдер).

**Обратная связь TOP:** Используйте ссылку на параметр `top`, а не провод прямого ввода. «Недостаточно источников» решается после первого приготовления. Ожидается предупреждение «Петля зависимости приготовления».

**Разрешение:** Некоммерческие ограничения при разрешении 1280×1280. Используйте `outputresolution = 'custom'`.

**Большие шейдеры:** напишите GLSL в `/tmp/file.glsl`, затем используйте `td_write_dat` или `td_execute_python` для загрузки.

**Доступ к вершинам/точкам (TD 2025.32):** `point.P[0]`, `point.P[1]`, `point.P[2]` — НЕ `.x`, `.y`, `.z`.

**Расширения:** Формат `ext0object` — `"op('./datName').module.ClassName(me)"` в режиме CONSTANT. После редактирования кода расширения с помощью `td_write_dat` позвоните по номеру `td_reinit_extension`.

**Обратные вызовы скриптов**: ВСЕГДА используйте относительные пути через `me.parent()`/`scriptOp.parent()`.

**Очистка узлов:** Всегда `list(root.children)` перед итерацией + `child.valid` проверка.

## Запись/экспорт видео

```python
# via td_execute_python:
root = op('/project1')
rec = root.create(moviefileoutTOP, 'recorder')
op('/project1/out').outputConnectors[0].connect(rec.inputConnectors[0])
rec.par.type = 'movie'
rec.par.file = '/tmp/output.mov'
rec.par.videocodec = 'prores'  # Apple ProRes — NOT license-restricted on macOS
rec.par.record = True   # start
# rec.par.record = False  # stop (call separately later)
```

H.264/H.265/AV1 требует коммерческой лицензии. Используйте `prores` в macOS или `mjpa` в качестве резервного варианта.
Извлечь кадры: `ffmpeg -i /tmp/output.mov -vframes 120 /tmp/frames/frame_%06d.png`

**TOP.save() бесполезен для анимации** — каждый раз захватывает одну и ту же текстуру графического процессора. Всегда используйте MovieFileOut.

### Перед записью: контрольный список

1. **Проверьте FPS > 0** через `td_get_perf`. Если FPS=0, запись будет пустой. См. подводные камни № 38–39.
2. **Убедитесь, что вывод шейдера не черный** через `td_get_screenshot`. Черный вывод = ошибка шейдера или отсутствие ввода. См. подводные камни №8, №40.
3. **При записи со звуком** сначала включите звук, а затем задержите запись на 3 кадра. См. подводные камни № 19.
4. **Установить путь вывода перед началом записи** — установка обоих в одном скрипте может привести к состязанию.

## Аудио-реактивный GLSL (проверенный рецепт)

### Правильная цепочка сигналов (проверено в апреле 2026 г.)

```
AudioFileIn CHOP (playmode=sequential)
  → AudioSpectrum CHOP (FFT=512, outputmenu=setmanually, outlength=256, timeslice=ON)
  → Math CHOP (gain=10)
  → CHOP to TOP (dataformat=r, layout=rowscropped)
  → GLSL TOP input 1 (spectrum texture, 256x2)

Constant TOP (rgba32float, time) → GLSL TOP input 0
GLSL TOP → Null TOP → MovieFileOut
```

### Критические правила реагирования на звук (проверено эмпирически)

1. **TimeSlice должен оставаться включенным** для AudioSpectrum. ВЫКЛ = обрабатывает весь аудиофайл → более 24000 сэмплов → переполнение CHOP to TOP.
2. **Установите длину вывода вручную** на 256 с помощью `outputmenu='setmanually'` и `outlength=256`. По умолчанию выводится 22050 образцов.
3. **НЕ ИСПОЛЬЗУЙТЕ Lag CHOP для сглаживания спектра.** Lag CHOP работает в режиме временного интервала и расширяет 256 выборок до 2400+, усредняя все значения почти до нуля (~1e-06). Шейдер не получает никаких полезных данных. Это был сбой синхронизации звука №1 за время тестирования.
4. **НЕ ИСПОЛЬЗУЙТЕ Filter CHOP** — та же проблема с расширением временного интервала с данными спектра.
5. **Сглаживание выполняется в шейдере GLSL**, если необходимо, с помощью временного лерпа с текстурой обратной связи: `mix(prevValue, newValue, 0.3)`. Это обеспечивает идеальную синхронизацию кадров с нулевой задержкой конвейера.
6. **CHOP to TOP dataformat = 'r'**, макет = 'rowscropping'. Спектральный выход — 256x2 (стерео). Выборка при y=0,25 для первого канала.
7. **Успех по математике = 10** (а не 5). Значения необработанного спектра составляют ~0,19 в диапазоне низких частот. Прирост в 10 дает пригодные для использования шейдера ~5,0.
8. **Resample CHOP не требуется.** Управляйте выходным размером напрямую с помощью параметра `outlength` AudioSpectrum.

### Выборка спектра GLSL

```glsl
// Input 0 = time (1x1 rgba32float), Input 1 = spectrum (256x2)
float iTime = texture(sTD2DInputs[0], vec2(0.5)).r;

// Sample multiple points per band and average for stability:
// NOTE: y=0.25 for first channel (stereo texture is 256x2, first row center is 0.25)
float bass = (texture(sTD2DInputs[1], vec2(0.02, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.05, 0.25)).r) / 2.0;
float mid  = (texture(sTD2DInputs[1], vec2(0.2, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.35, 0.25)).r) / 2.0;
float hi   = (texture(sTD2DInputs[1], vec2(0.6, 0.25)).r +
              texture(sTD2DInputs[1], vec2(0.8, 0.25)).r) / 2.0;
```

См. `references/network-patterns.md` для получения полных сценариев сборки и кода шейдера.

## Краткий справочник оператора

| Семья | Цвет | Класс Python/тип MCP | Суффикс |
|--------|-------|-------------|--------|
| ТОП | Фиолетовый | NoiseTOP, glslTOP, CompositeTOP, levelTop, BlurTOP, textTOP, nullTOP | ТОП |
| ЧОП | Зеленый | audiofileinCHOP, audiospectrumCHOP, mathCHOP, lfoCHOP, константаCHOP | ЧОП |
| СОП | Синий | GridSOP,SphereSOP, TransformSOP, NoiseSOP | СОП |
| ДАТ | Белый | textDAT, tableDAT, scriptDAT, webserverDAT | ДАТ |
| МАТ | Желтый | phongMAT, pbrMAT, glslMAT, constMAT | МАТ |
| КОМП | Серый | геометрияCOMP, контейнерCOMP, cameraCOMP, LightCOMP, windowCOMP | КОМП |

## Примечания по безопасности

- MCP работает только на локальном хосте (порт 40404). Никакой аутентификации — любой локальный процесс может отправлять команды.
- `td_execute_python` имеет неограниченный доступ к среде и файловой системе TD Python в качестве пользователя процесса TD.
- `setup.sh` загружает twozero.tox с официального URL-адреса 404zero.com. Проверьте загрузку, если это необходимо.
— Навык никогда не отправляет данные за пределы локального хоста. Вся связь MCP является локальной.

## Ссылки

| Файл | Что |
|------|------|
| `references/pitfalls.md` | С трудом добытые уроки реальных сессий |
| `references/operators.md` | Все семейства операторов с параметрами и вариантами использования |
| `references/network-patterns.md` | Рецепты: аудиореактивный, генеративный, GLSL, создание экземпляров |
| `references/mcp-tools.md` | Полные схемы параметров инструмента MCP с двумя нулями |
| `references/python-api.md` | TD Python: op(), сценарии, расширения |
| `references/troubleshooting.md` | Диагностика подключения, отладка |
| `references/glsl.md` | Униформы GLSL, встроенные функции, шаблоны шейдеров |
| `references/postfx.md` | Post-FX: блум, ЭЛТ, хроматическая аберрация, свечение обратной связи |
| `references/layout-compositor.md` | Шаблоны макетов HUD, сетки панелей, макеты в стиле BSP |
| `references/operator-tips.md` | Каркасный рендеринг, обратная связь ТОП-настройка |
| `references/geometry-comp.md` | Geometry COMP: создание экземпляров, POP vs SOP, морфинг |
| `references/audio-reactive.md` | Извлечение аудиодиапазона, обнаружение ритма, следование огибающей |
| `references/animation.md` | LFO, таймеры, ключевые кадры, замедление, движение, управляемое экспрессией |
| `references/midi-osc.md` | Контроллеры MIDI/OSC, TouchOSC, синхронизация с несколькими машинами |
| `references/particles.md` | СОЗ и устаревшие частицыSOP — выбросы, силы, столкновения |
| `references/projection-mapping.md` | Многооконный вывод, угловой штифт, деформация сетки, сглаживание краев |
| `references/external-data.md` | HTTP, WebSocket, MQTT, последовательный порт, TCP, веб-серверDAT |
| `references/panel-ui.md` | Пользовательские параметры, панели COMP, кнопка/ползунок/поле, PanelExecuteDAT |
| `references/replicator.md` | ReplicatorCOMP — клонирование на основе данных, макеты, обратные вызовы |
| `references/dat-scripting.md` | Выполнить семейство DAT — прерывание/dat/параметр/панель/оп/executeDAT |
| `references/3d-scene.md` | Освещение, тени, IBL/кубические карты, мультикамера, PBR |
| `scripts/setup.sh` | Скрипт автоматической настройки |

---

> Ты не пишешь код. Вы проводите свет.