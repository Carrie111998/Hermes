---
sidebar_position: 8
title: Расширение интерфейса командной строки
description: Создавайте интерфейсы командной строки-оболочки, которые расширяют TUI
  Hermes с помощью пользовательских виджетов, сочетаний клавиш и изменений макета.
---

# Расширение CLI

Hermes предоставляет защищенные перехватчики расширений для `HermesCLI`, поэтому интерфейсы командной строки оболочки могут добавлять виджеты, привязки клавиш и настройки макета, не переопределяя метод `run()` из более чем 1000 строк. Это позволит вашему расширению быть отделенным от внутренних изменений.

## Точки расширения

Доступны пять удлинительных швов:

| Крюк | Цель | Переопределить, когда... |
|------|---------|------------------|
| `_get_extra_tui_widgets()` | Внедрение виджетов в макет | Вам нужен постоянный элемент пользовательского интерфейса (панель, строка состояния, мини-плеер) |
| `_register_extra_tui_keybindings(kb, *, input_area)` | Добавить сочетания клавиш | Вам нужны горячие клавиши (переключение панелей, элементы управления транспортом, модальные ярлыки) |
| `_build_tui_layout_children(**widgets)` | Полный контроль над заказом виджетов | Вам необходимо изменить порядок или обернуть существующие виджеты (редко) |
| `process_command()` | Добавить пользовательские команды слэша | Вам нужна обработка `/mycommand` (уже существующий хук) |
| `_build_tui_style_dict()` | Пользовательские стили Prompt_toolkit | Вам нужны пользовательские цвета или стиль (уже существующий крючок) |

Первые три — новые защищенные крючки. Последние два уже существовали.

## Быстрый старт: интерфейс командной строки-оболочки

```python
#!/usr/bin/env python3
"""my_cli.py — Example wrapper CLI that extends Hermes."""

from cli import HermesCLI
from prompt_toolkit.layout import FormattedTextControl, Window
from prompt_toolkit.filters import Condition


class MyCLI(HermesCLI):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._panel_visible = False

    def _get_extra_tui_widgets(self):
        """Add a toggleable info panel above the status bar."""
        cli_ref = self
        return [
            Window(
                FormattedTextControl(lambda: "📊 My custom panel content"),
                height=1,
                filter=Condition(lambda: cli_ref._panel_visible),
            ),
        ]

    def _register_extra_tui_keybindings(self, kb, *, input_area):
        """F2 toggles the custom panel."""
        cli_ref = self

        @kb.add("f2")
        def _toggle_panel(event):
            cli_ref._panel_visible = not cli_ref._panel_visible

    def process_command(self, cmd: str) -> bool:
        """Add a /panel slash command."""
        if cmd.strip().lower() == "/panel":
            self._panel_visible = not self._panel_visible
            state = "visible" if self._panel_visible else "hidden"
            print(f"Panel is now {state}")
            return True
        return super().process_command(cmd)


if __name__ == "__main__":
    cli = MyCLI()
    cli.run()
```

Запустите его:

```bash
cd ~/.hermes/hermes-agent
source .venv/bin/activate
python my_cli.py
```

## Ссылка на крючок

### `_get_extra_tui_widgets()`

Возвращает список виджетов Prompt_toolkit для вставки в макет TUI. Виджеты появляются **между разделителем и строкой состояния** — над областью ввода, но под основным выводом.

```python
def _get_extra_tui_widgets(self) -> list:
    return []  # default: no extra widgets
```

Каждый виджет должен представлять собой контейнер Prompt_toolkit (например, `Window`, `ConditionalContainer`, `HSplit`). Используйте `ConditionalContainer` или `filter=Condition(...)`, чтобы сделать виджеты переключаемыми.

```python
from prompt_toolkit.layout import ConditionalContainer, Window, FormattedTextControl
from prompt_toolkit.filters import Condition

def _get_extra_tui_widgets(self):
    return [
        ConditionalContainer(
            Window(FormattedTextControl("Status: connected"), height=1),
            filter=Condition(lambda: self._show_status),
        ),
    ]
```

### `_register_extra_tui_keybindings(kb, *, input_area)`

Вызывается после того, как Hermes регистрирует свои собственные сочетания клавиш и до построения макета. Добавьте свои сочетания клавиш в `kb`.

```python
def _register_extra_tui_keybindings(self, kb, *, input_area):
    pass  # default: no extra keybindings
```

Параметры:
- **`kb`** — экземпляр `KeyBindings` для приложения Prompt_toolkit.
- **`input_area`** — Основной виджет `TextArea`, если вам нужно читать или манипулировать пользовательским вводом.

```python
def _register_extra_tui_keybindings(self, kb, *, input_area):
    cli_ref = self

    @kb.add("f3")
    def _clear_input(event):
        input_area.text = ""

    @kb.add("f4")
    def _insert_template(event):
        input_area.text = "/search "
```

**Избегайте конфликтов** с помощью встроенных сочетаний клавиш: `Enter` (отправить), `Escape Enter` (новая строка), `Ctrl-C` (прерывание), `Ctrl-D` (выход), `Tab` (автоматическое предложение принять). Комбинации функциональных клавиш F2+ и Ctrl в целом безопасны.

### `_build_tui_layout_children(**widgets)`

Отменяйте это значение только в том случае, если вам нужен полный контроль над порядком виджетов. Вместо этого большинству расширений следует использовать `_get_extra_tui_widgets()`.

```python
def _build_tui_layout_children(self, *, sudo_widget, secret_widget,
    approval_widget, clarify_widget, model_picker_widget=None,
    spinner_widget=None, spacer, status_bar, input_rule_top,
    image_bar, input_area, input_rule_bot, voice_status_bar,
    completions_menu) -> list:
```

Реализация по умолчанию возвращает результат (любые виджеты `None` отфильтровываются):

```python
[
    Window(height=0),       # anchor
    sudo_widget,            # sudo password prompt (conditional)
    secret_widget,          # secret input prompt (conditional)
    approval_widget,        # dangerous command approval (conditional)
    clarify_widget,         # clarify question UI (conditional)
    model_picker_widget,    # model picker overlay (conditional)
    spinner_widget,         # thinking spinner (conditional)
    spacer,                 # fills remaining vertical space
    *self._get_extra_tui_widgets(),  # YOUR WIDGETS GO HERE
    status_bar,             # model/token/context status line
    input_rule_top,         # ─── border above input
    image_bar,              # attached images indicator
    input_area,             # user text input
    input_rule_bot,         # ─── border below input
    voice_status_bar,       # voice mode status (conditional)
    completions_menu,       # autocomplete dropdown
]
```

## Схема компоновки

Расположение по умолчанию сверху вниз:

1. **Область вывода** — прокрутка истории разговоров.
2. **Проставка**
3. **Дополнительные виджеты** — от `_get_extra_tui_widgets()`
4. **Строка состояния** — модель, процент контекста, прошедшее время.
5. **Панель изображений** — количество прикрепленных изображений.
6. **Область ввода** — подсказка пользователю.
7. **Голосовой статус** — индикатор записи.
8. **Меню «Дополнения»** — предложения автозаполнения.

## Советы

- **Аннулировать отображение** после изменения состояния: вызовите `self._invalidate()`, чтобы вызвать перерисовку Prompt_toolkit.
- **Состояние агента доступа**: `self.agent`, `self.model`, `self.conversation_history` доступны.
- **Пользовательские стили**: переопределите `_build_tui_style_dict()` и добавьте записи для своих классов пользовательских стилей.
- **Команды косой черты**: переопределите `process_command()`, обрабатывайте свои команды и вызывайте `super().process_command(cmd)` для всего остального.
- **Не переопределяйте `run()`** без крайней необходимости — хуки расширения существуют специально для того, чтобы избежать такой связи.