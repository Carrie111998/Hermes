"""Telegram c12 mixin for ``TelegramAdapter``.

Extracted verbatim from ``plugins/platforms/telegram/adapter.py`` as part
of the god-file decomposition campaign. Picker cluster: model/choice picker senders, paginated provider
and model keyboards, and their inline-callback handlers.

Behavior-neutral: every method is lifted character-for-character from
``TelegramAdapter``. ``self.*`` calls resolve unchanged via the MRO, and
``PickerMixin`` precedes ``BasePlatformAdapter`` in the bases so
resolution order is what it was when these methods sat on the class.

Three details keep the lift observationally identical:

* ``logger`` is bound by explicit name rather than ``__name__``, so records
  emitted from these methods keep the logger name
  ``"plugins.platforms.telegram.adapter"``. ``getLogger`` returns the same
  singleton object the adapter module holds.
* ``InlineKeyboardButton``/``InlineKeyboardMarkup``/``ParseMode`` are
  imported under the same ``ImportError`` guard the adapter uses, falling
  back to ``Any``/``None``. This module deliberately does not enable
  postponed annotation evaluation, matching the adapter, so the
  annotations on the lifted signatures are evaluated exactly as before.
* Shared adapter helpers are re-imported from their home module, exactly
  as the adapter imports them.
"""

import asyncio
import logging

from typing import Any, Dict, Optional

from gateway.platforms.base import SendResult

from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

from plugins.platforms.telegram.adapter import _redact_telegram_error_text

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.constants import ParseMode
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    InlineKeyboardButton = Any
    InlineKeyboardMarkup = Any
    ParseMode = None

# Bind the adapter's logger by name so log records lifted with these methods
# are emitted under exactly the name they were before.
logger = logging.getLogger("plugins.platforms.telegram.adapter")


class PickerMixin:
    """c12 cluster lifted verbatim from ``TelegramAdapter``."""

    async def send_model_picker(
        self,
        chat_id: str,
        providers: list,
        current_model: str,
        current_provider: str,
        session_key: str,
        on_model_selected,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive inline-keyboard model picker.

        Two-step drill-down: provider selection → model selection.
        Edits the same message in-place as the user navigates.
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            from hermes_cli.providers import get_label
        except ImportError:
            def get_label(slug):
                return slug

        try:
            # Build provider buttons — folds provider groups (display only).
            keyboard, provider_page_info = self._build_provider_keyboard(providers, 0)

            provider_label = get_label(current_provider)
            text = self.format_message(
                (
                    f"⚙ *Model Configuration*\n\n"
                    f"Current model: `{current_model or 'unknown'}`\n"
                    f"Provider: {provider_label}\n\n"
                    f"Select a provider:{provider_page_info}"
                )
            )

            thread_id = metadata.get("thread_id") if metadata else None
            reply_to_id = self._reply_to_message_id_for_send(None, metadata, reply_to_mode=self._reply_to_mode)
            msg = await self._send_message_with_thread_fallback(
                chat_id=normalize_telegram_chat_id(chat_id),
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
                reply_to_message_id=reply_to_id,
                **self._thread_kwargs_for_send(
                    chat_id,
                    thread_id,
                    metadata,
                    reply_to_message_id=reply_to_id,
                    reply_to_mode=self._reply_to_mode
                ),
                **self._link_preview_kwargs(),
            )

            # Store picker state keyed by chat_id
            self._model_picker_state[str(chat_id)] = {
                "msg_id": msg.message_id,
                "providers": providers,
                "session_key": session_key,
                "on_model_selected": on_model_selected,
                "current_model": current_model,
                "current_provider": current_provider,
                "provider_page": 0,
            }

            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning("[%s] send_model_picker failed: %s", self.name, _redact_telegram_error_text(e))
            return SendResult(success=False, error=_redact_telegram_error_text(e))

    async def send_choice_picker(
        self,
        chat_id: str,
        title: str,
        choices: list,
        session_key: str,
        on_choice_selected,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a flat inline-keyboard choice picker (one tap → one value).

        Generic single-level companion to ``send_model_picker`` used by
        `/reasoning`, `/fast`, and any future finite-choice command. Each
        choice dict: ``{"value": str, "label": str, "is_current": bool}``.
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            buttons = []
            for i, choice in enumerate(choices):
                label = str(choice.get("label") or choice.get("value") or "")
                if choice.get("is_current"):
                    label = f"✓ {label}"
                buttons.append(
                    InlineKeyboardButton(label, callback_data=f"cp:{i}")
                )
            if not buttons:
                return SendResult(success=False, error="No choices")
            # Two buttons per row keeps labels readable on mobile.
            keyboard = InlineKeyboardMarkup(
                [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            )

            thread_id = metadata.get("thread_id") if metadata else None
            reply_to_id = self._reply_to_message_id_for_send(None, metadata, reply_to_mode=self._reply_to_mode)
            msg = await self._send_message_with_thread_fallback(
                chat_id=normalize_telegram_chat_id(chat_id),
                text=self.format_message(title),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
                reply_to_message_id=reply_to_id,
                **self._thread_kwargs_for_send(
                    chat_id,
                    thread_id,
                    metadata,
                    reply_to_message_id=reply_to_id,
                    reply_to_mode=self._reply_to_mode
                ),
                **self._link_preview_kwargs(),
            )

            self._choice_picker_state[str(chat_id)] = {
                "msg_id": msg.message_id,
                "choices": choices,
                "session_key": session_key,
                "on_choice_selected": on_choice_selected,
            }
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning("[%s] send_choice_picker failed: %s", self.name, _redact_telegram_error_text(e))
            return SendResult(success=False, error=_redact_telegram_error_text(e))

    async def _handle_choice_picker_callback(
        self, query, data: str, chat_id: str
    ) -> None:
        """Handle choice picker button taps (cp:<index>)."""
        state = self._choice_picker_state.get(chat_id)
        if not state:
            await query.answer(text="Picker expired — run the command again.")
            return

        # Same authorization gate as approval buttons: unauthorized users in a
        # shared group must not flip session/config state via someone else's
        # picker message.
        query_message = getattr(query, "message", None)
        query_chat = getattr(query_message, "chat", None)
        if not self._is_callback_user_authorized(
            str(getattr(query.from_user, "id", "")),
            chat_id=getattr(query_message, "chat_id", None),
            chat_type=str(getattr(query_chat, "type", None)) if getattr(query_chat, "type", None) is not None else None,
            thread_id=str(getattr(query_message, "message_thread_id", None)) if getattr(query_message, "message_thread_id", None) is not None else None,
            user_name=getattr(query.from_user, "first_name", None),
        ):
            await query.answer(text="⛔ You are not authorized to change this setting.")
            return

        try:
            idx = int(data[3:])
            choice = state["choices"][idx]
        except (ValueError, IndexError):
            await query.answer(text="Invalid selection.")
            return

        callback = state.get("on_choice_selected")
        if not callback:
            await query.answer(text="Picker expired.")
            return

        try:
            result_text = await callback(chat_id, str(choice.get("value") or ""))
        except Exception as exc:
            logger.error("Choice picker selection failed: %s", exc)
            result_text = f"Error applying selection: {exc}"

        try:
            await query.edit_message_text(
                text=self.format_message(result_text),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=None,
            )
        except Exception:
            try:
                await query.edit_message_text(
                    text=result_text, parse_mode=None, reply_markup=None,
                )
            except Exception:
                pass
        await query.answer()
        self._choice_picker_state.pop(chat_id, None)

    def _build_provider_keyboard(self, providers: list, page: int = 0) -> tuple:
        """Build the paginated top-level provider keyboard, folding groups.

        Provider families (Kimi/Moonshot, MiniMax, xAI Grok, ...) collapse to
        a single ``mpg:<gid>`` button; tapping it drills into a member
        sub-keyboard. Single providers (and groups with only one authenticated
        member) render as direct ``mp:<slug>`` buttons. Grouping mirrors the
        CLI ``hermes model`` picker via the shared ``group_providers`` fold,
        so all surfaces stay consistent.
        """
        try:
            from hermes_cli.models import group_providers
        except Exception:
            group_providers = None

        by_slug = {p.get("slug"): p for p in providers}

        def _provider_button(p):
            count = p.get("total_models", len(p.get("models", [])))
            label = f"{p['name']} ({count})"
            if p.get("is_current"):
                label = f"✓ {label}"
            return InlineKeyboardButton(label, callback_data=f"mp:{p['slug']}")

        buttons: list = []
        if group_providers is not None:
            for row in group_providers([p.get("slug") for p in providers]):
                if row["kind"] == "group":
                    members = [by_slug[m] for m in row["members"] if m in by_slug]
                    count = sum(
                        m.get("total_models", len(m.get("models", []))) for m in members
                    )
                    label = f"{row['label']} ▸ ({count})"
                    if any(m.get("is_current") for m in members):
                        label = f"✓ {label}"
                    buttons.append(
                        InlineKeyboardButton(label, callback_data=f"mpg:{row['group_id']}")
                    )
                else:
                    p = by_slug.get(row["slug"])
                    if p is not None:
                        buttons.append(_provider_button(p))
        else:
            for p in providers:
                buttons.append(_provider_button(p))

        page_buttons, page_meta = self._format_choice_page(
            buttons, page, self._PROVIDER_PAGE_SIZE
        )
        page = page_meta["page"]
        total_pages = page_meta["total_pages"]

        rows = [page_buttons[i : i + 2] for i in range(0, len(page_buttons), 2)]

        if total_pages > 1:
            nav: list = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"mpv:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="mx:noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ▶", callback_data=f"mpv:{page + 1}"))
            rows.append(nav)

        rows.append([InlineKeyboardButton("✗ Cancel", callback_data="mx")])

        return InlineKeyboardMarkup(rows), page_meta["page_info"]

    def _build_model_keyboard(self, models: list, page: int) -> tuple:
        """Build paginated model buttons. Returns (keyboard, page_info_text)."""
        page_models, page_meta = self._format_choice_page(
            models, page, self._MODEL_PAGE_SIZE
        )
        page = page_meta["page"]
        total_pages = page_meta["total_pages"]
        start = page_meta["start"]

        buttons: list = []
        for i, model_id in enumerate(page_models):
            abs_idx = start + i
            short = model_id.split("/")[-1] if "/" in model_id else model_id
            if len(short) > 38:
                short = short[:35] + "..."
            buttons.append(
                InlineKeyboardButton(short, callback_data=f"mm:{abs_idx}")
            )

        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

        # Pagination row (if needed)
        if total_pages > 1:
            nav: list = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"mg:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="mx:noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ▶", callback_data=f"mg:{page + 1}"))
            rows.append(nav)

        rows.append([
            InlineKeyboardButton("◀ Back", callback_data="mb"),
            InlineKeyboardButton("✗ Cancel", callback_data="mx"),
        ])

        return InlineKeyboardMarkup(rows), page_meta["page_info"]

    async def _handle_model_picker_callback(
        self, query, data: str, chat_id: str
    ) -> None:
        """Handle model picker inline keyboard callbacks (mp:/mm:/mc:/mb:/mx:/mg:)."""
        state = self._model_picker_state.get(chat_id)
        if not state:
            await query.answer(text="Picker expired — use /model again.")
            return

        try:
            from hermes_cli.providers import get_label
        except ImportError:
            def get_label(slug):
                return slug

        if data.startswith("mp:"):
            # --- Provider selected: show model buttons (page 0) ---
            provider_slug = data[3:]
            provider = next(
                (p for p in state["providers"] if p["slug"] == provider_slug),
                None,
            )
            if not provider:
                await query.answer(text="Provider not found.")
                return

            models = provider.get("models", [])
            state["selected_provider"] = provider_slug
            state["selected_provider_name"] = provider.get("name", provider_slug)
            state["model_list"] = models
            state["model_page"] = 0

            keyboard, page_info = self._build_model_keyboard(models, 0)

            pname = provider.get("name", provider_slug)
            total = provider.get("total_models", len(models))
            shown = len(models)
            extra = f"\n_{total - shown} more available — type `/model <name>` directly_" if total > shown else ""

            await query.edit_message_text(
                text=self.format_message(
                    (
                        f"⚙ *Model Configuration*\n\n"
                        f"Provider: *{pname}*{page_info}\n"
                        f"Select a model:{extra}"
                    )
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data.startswith("mg:"):
            # --- Page navigation ---
            try:
                page = int(data[3:])
            except ValueError:
                await query.answer(text="Invalid page.")
                return

            models = state.get("model_list", [])
            state["model_page"] = page

            keyboard, page_info = self._build_model_keyboard(models, page)

            pname = state.get("selected_provider_name", "")
            provider_slug = state.get("selected_provider", "")
            provider = next(
                (p for p in state["providers"] if p["slug"] == provider_slug),
                None,
            )
            total = provider.get("total_models", len(models)) if provider else len(models)
            shown = len(models)
            extra = f"\n_{total - shown} more available — type `/model <name>` directly_" if total > shown else ""

            await query.edit_message_text(
                text=self.format_message(
                    (
                        f"⚙ *Model Configuration*\n\n"
                        f"Provider: *{pname}*{page_info}\n"
                        f"Select a model:{extra}"
                    )
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data.startswith("mpv:"):
            # --- Provider page navigation ---
            try:
                page = int(data[4:])
            except ValueError:
                await query.answer(text="Invalid page.")
                return

            state["provider_page"] = page
            keyboard, provider_page_info = self._build_provider_keyboard(
                state["providers"], page
            )

            try:
                provider_label = get_label(state["current_provider"])
            except Exception:
                provider_label = state["current_provider"]

            await query.edit_message_text(
                text=self.format_message(
                    (
                        f"⚙ *Model Configuration*\n\n"
                        f"Current model: `{state['current_model'] or 'unknown'}`\n"
                        f"Provider: {provider_label}\n\n"
                        f"Select a provider:{provider_page_info}"
                    )
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data.startswith("mc:"):
            # --- Expensive model confirmed: perform the switch ---
            try:
                idx = int(data[3:])
            except ValueError:
                await query.answer(text="Invalid selection.")
                return

            model_list = state.get("model_list", [])
            if idx < 0 or idx >= len(model_list):
                await query.answer(text="Invalid model index.")
                return

            model_id = model_list[idx]
            provider_slug = state.get("selected_provider", "")
            callback = state.get("on_model_selected")

            if not callback:
                await query.answer(text="Picker expired.")
                return

            switch_failed = False
            try:
                result_text = await callback(chat_id, model_id, provider_slug)
            except Exception as exc:
                logger.error("Model picker switch failed: %s", exc)
                result_text = f"Error switching model: {exc}"
                switch_failed = True

            try:
                await query.edit_message_text(
                    text=self.format_message(result_text),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=None,
                )
            except Exception:
                try:
                    await query.edit_message_text(
                        text=result_text,
                        parse_mode=None,
                        reply_markup=None,
                    )
                except Exception:
                    pass
            await query.answer(
                text="Switch failed." if switch_failed else "Model switched!"
            )
            self._model_picker_state.pop(chat_id, None)

        elif data.startswith("mm:"):
            # --- Model selected: perform the switch ---
            try:
                idx = int(data[3:])
            except ValueError:
                await query.answer(text="Invalid selection.")
                return

            model_list = state.get("model_list", [])
            if idx < 0 or idx >= len(model_list):
                await query.answer(text="Invalid model index.")
                return

            model_id = model_list[idx]
            provider_slug = state.get("selected_provider", "")
            callback = state.get("on_model_selected")

            if not callback:
                await query.answer(text="Picker expired.")
                return

            try:
                from hermes_cli.model_cost_guard import expensive_model_warning

                # Pricing lookup can hit models.dev / a /models endpoint on a
                # cache miss — keep it off the event loop.
                warning = await asyncio.to_thread(
                    expensive_model_warning,
                    model_id,
                    provider=provider_slug,
                )
            except Exception:
                warning = None
            if warning is not None:
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Switch anyway", callback_data=f"mc:{idx}")],
                    [
                        InlineKeyboardButton("◀ Back", callback_data="mb"),
                        InlineKeyboardButton("✗ Cancel", callback_data="mx"),
                    ],
                ])
                await query.edit_message_text(
                    text=self.format_message(
                        f"⚠ *Expensive Model Warning*\n\n{warning.message}"
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=keyboard,
                )
                await query.answer(text="Confirm expensive model")
                return

            switch_failed = False
            try:
                result_text = await callback(chat_id, model_id, provider_slug)
            except Exception as exc:
                logger.error("Model picker switch failed: %s", exc)
                result_text = f"Error switching model: {exc}"
                switch_failed = True

            # Edit message to show confirmation, remove buttons
            try:
                await query.edit_message_text(
                    text=self.format_message(result_text),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=None,
                )
            except Exception:
                # Markdown parse failure — retry as plain text
                try:
                    await query.edit_message_text(
                        text=result_text,
                        parse_mode=None,
                        reply_markup=None,
                    )
                except Exception:
                    pass
            await query.answer(
                text="Switch failed." if switch_failed else "Model switched!"
            )

            # Clean up state
            self._model_picker_state.pop(chat_id, None)

        elif data.startswith("mpg:"):
            # --- Provider group selected: show member providers ---
            group_id = data[4:]
            try:
                from hermes_cli.models import PROVIDER_GROUPS
                _label, _desc, member_slugs = PROVIDER_GROUPS.get(group_id, ("", "", []))
            except Exception:
                _label, member_slugs = "", []

            by_slug = {p["slug"]: p for p in state["providers"]}
            members = [by_slug[m] for m in member_slugs if m in by_slug]
            if not members:
                await query.answer(text="Group not found.")
                return

            buttons = []
            for p in members:
                count = p.get("total_models", len(p.get("models", [])))
                label = f"{p['name']} ({count})"
                if p.get("is_current"):
                    label = f"✓ {label}"
                buttons.append(
                    InlineKeyboardButton(label, callback_data=f"mp:{p['slug']}")
                )
            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append([
                InlineKeyboardButton("◀ Back", callback_data="mb"),
                InlineKeyboardButton("✗ Cancel", callback_data="mx"),
            ])
            keyboard = InlineKeyboardMarkup(rows)

            await query.edit_message_text(
                text=self.format_message(
                    (
                        f"⚙ *Model Configuration*\n\n"
                        f"Provider family: *{_label or group_id}*\n\n"
                        f"Select a provider:"
                    )
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data == "mb":
            # --- Back to provider list (folds groups) ---
            page = int(state.get("provider_page", 0) or 0)
            keyboard, provider_page_info = self._build_provider_keyboard(
                state["providers"], page
            )

            try:
                provider_label = get_label(state["current_provider"])
            except Exception:
                provider_label = state["current_provider"]

            await query.edit_message_text(
                text=self.format_message(
                    (
                        f"⚙ *Model Configuration*\n\n"
                        f"Current model: `{state['current_model'] or 'unknown'}`\n"
                        f"Provider: {provider_label}\n\n"
                        f"Select a provider:{provider_page_info}"
                    )
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data == "mx":
            # --- Cancel ---
            self._model_picker_state.pop(chat_id, None)
            await query.edit_message_text(
                text="Model selection cancelled.",
                reply_markup=None,
            )
            await query.answer()

        else:
            # Catch-all (e.g. page counter button "mx:noop")
            await query.answer()
