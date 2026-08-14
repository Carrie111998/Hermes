"""Registry-owned slash command execution (thin slice).

Shared, surface-independent executors for informational slash commands.
``CommandDef.execute`` (hermes_cli/commands.py) names a key in
:data:`EXECUTORS`; each surface (CLI REPL, gateway, TUI slash worker via the
CLI) resolves that key through :func:`run_execute` and applies only its own
decoration (Rich markup, emoji/markdown, ``_telegramize_command_mentions``)
to the canonical :class:`CommandReply`.

Invariant: an executor's output depends only on ``ctx.args`` / ``ctx.options``
— never on ``ctx.surface`` — so the core text is identical across surfaces
for a fixed context (enforced by tests/hermes_cli/test_commands_execute.py).

Import discipline: this module imports nothing heavy at module level and
``hermes_cli.commands`` does NOT import this module (the ``execute`` field is
a plain string), so the gateway can keep importing ``commands.py`` without
prompt_toolkit and without cycles.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


def _resolve_python_executable() -> str:
    return shutil.which("python") or shutil.which("python3") or sys.executable


_PYTHON = _resolve_python_executable()

__all__ = [
    "CommandContext",
    "CommandReply",
    "EXECUTORS",
    "execute_command",
    "resolve_executor",
    "run_execute",
]


# ---------------------------------------------------------------------------
# Context / reply dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandContext:
    """Surface-provided inputs for a shared command executor."""

    surface: str = "cli"                # "cli" | "gateway" | "tui" — decoration only
    args: str = ""                      # raw argument string after the command word
    options: Mapping[str, Any] = field(default_factory=dict)  # surface params (page_size, ...)
    config_get: Callable[[str, Any], Any] | None = None       # optional config accessor


@dataclass(frozen=True)
class CommandReply:
    """Canonical result of a shared executor.

    ``text`` is the surface-independent core text.  ``data`` carries the
    structured values the executor derived so a surface may re-render them
    with its own decoration (Rich columns, markdown bullets) without
    duplicating the computation.  ``format`` is a rendering hint only.
    """

    text: str
    data: Mapping[str, Any] = field(default_factory=dict)
    format: str = "plain"               # "plain" | "markdown" (hint, not a contract)


# ---------------------------------------------------------------------------
# Executors — pure formatters, no agent/session mutation
# ---------------------------------------------------------------------------

def _exec_version(ctx: CommandContext) -> CommandReply:
    """Core /version text — the banner version label."""
    from hermes_cli.banner import format_banner_version_label

    return CommandReply(format_banner_version_label())


def _exec_egress(ctx: CommandContext) -> CommandReply:
    """Core /egress text — Docker egress proxy status."""
    from hermes_cli.proxy_cli import format_status_text

    return CommandReply(format_status_text())


def _exec_profile(ctx: CommandContext) -> CommandReply:
    """Core /profile data — active profile name + home directory.

    A multiplexed gateway may pre-resolve the per-source profile/home and pass
    them via ``options`` (``profile_name`` / ``home_display``); otherwise the
    process-level values are used (identical to the old CLI + non-multiplex
    gateway behavior).
    """
    profile_name = str(ctx.options.get("profile_name") or "").strip()
    home_display = str(ctx.options.get("home_display") or "").strip()

    if not profile_name:
        from hermes_cli.profiles import get_active_profile_name

        profile_name = get_active_profile_name()
    if not home_display:
        from hermes_constants import display_hermes_home

        home_display = display_hermes_home()

    return CommandReply(
        f"Profile: {profile_name}\nHome: {home_display}",
        data={"profile": profile_name, "home": home_display},
    )


def _exec_bundles(ctx: CommandContext) -> CommandReply:
    """Core /bundles data — installed skill bundles listing."""
    try:
        from agent.skill_bundles import _bundles_dir, list_bundles
    except Exception as exc:  # pragma: no cover - env-specific
        return CommandReply(
            f"Bundles subsystem unavailable: {exc}",
            data={"error": str(exc)},
        )

    bundles = list_bundles()
    bundles_dir = str(_bundles_dir())
    if not bundles:
        return CommandReply(
            "No skill bundles installed.\n"
            "Create one with: hermes bundles create <name> --skill <s1> --skill <s2>\n"
            f"Directory: {bundles_dir}",
            data={"bundles": [], "dir": bundles_dir},
        )

    lines = [f"Skill Bundles ({len(bundles)} installed):"]
    for info in bundles:
        skill_count = len(info.get("skills", []))
        desc = info.get("description") or f"Load {skill_count} skills"
        lines.append(f"/{info['slug']} — {desc} ({skill_count} skills)")
        for s in info.get("skills", []):
            lines.append(f"    · {s}")
    lines.append("Invoke a bundle with /<slug> to load all its skills.")
    return CommandReply(
        "\n".join(lines),
        data={"bundles": bundles, "dir": bundles_dir},
    )


def _exec_help(ctx: CommandContext) -> CommandReply:
    """Core gateway /help body (pre platform mention decoration)."""
    from agent.i18n import t
    from hermes_cli.commands import gateway_help_lines

    lines = [
        t("gateway.help.header"),
        *gateway_help_lines(),
    ]
    try:
        from agent.skill_commands import get_skill_commands
        skill_cmds = get_skill_commands()
        if skill_cmds:
            lines.append(t("gateway.help.skill_header", count=len(skill_cmds)))
            # Show first 10, then point to /commands for the rest
            sorted_cmds = sorted(skill_cmds)
            for cmd in sorted_cmds[:10]:
                lines.append(f"`{cmd}` — {skill_cmds[cmd]['description']}")
            if len(sorted_cmds) > 10:
                lines.append(t("gateway.help.more_use_commands", count=len(sorted_cmds) - 10))
    except Exception:
        pass
    return CommandReply("\n".join(lines), format="markdown")


def _exec_status(ctx: CommandContext) -> CommandReply:
    """Core /status text — lightweight Hermes dashboard."""
    from pathlib import Path
    import json
    from datetime import datetime

    ROOT = Path.home() / ".hermes"

    def load_json(path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    gateway = load_json(ROOT / "gateway_state.json")
    platform = gateway.get("platforms", {}).get("telegram", {})
    lines = [
        f"📊 Hermes Status {now}",
        f"- Gateway: {gateway.get('gateway_state', 'unknown')}",
        f"- Telegram: {platform.get('state', 'unknown')}",
    ]

    hardware = load_json(ROOT / "hardware_monitor_state.json")
    if hardware:
        alerts = [k for k in ["cpu", "mem", "gpu", "temp", "disk"] if hardware.get(f"{k}_last_alert")]
        if alerts:
            lines.append(f"- Letzte Alarme: {', '.join([a.upper() for a in alerts])}")
        else:
            lines.append("- Hardware-Alarme: keine")

    capture = load_json(ROOT / "visual_capture_state.json").get("last_capture", {})
    if capture:
        lines.append(f"- Letzter Capture: {capture.get('mode', '—')} ({capture.get('time', '')})")

    return CommandReply("\n".join(lines))


def _exec_morning_briefing(ctx: CommandContext) -> CommandReply:
    """Core /briefing text — run morning briefing script."""
    import subprocess
    from pathlib import Path

    script = Path.home() / ".hermes" / "scripts" / "hermes_morning_briefing.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        text = (proc.stdout or "").strip() or "Morning Briefing konnte nicht erstellt werden."
        return CommandReply(text)
    except Exception as exc:
        return CommandReply(f"Morning Briefing fehlgeschlagen: {exc}")


def _exec_manual_backup(ctx: CommandContext) -> CommandReply:
    """Run the backup script now."""
    import subprocess
    from pathlib import Path

    script = Path.home() / ".hermes" / "scripts" / "weekly_backup.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0:
            return CommandReply(out or "Backup erfolgreich gestartet.")
        return CommandReply(f"Backup fehlgeschlagen:\n{err or out}")
    except subprocess.TimeoutExpired:
        return CommandReply("Backup läuft noch... Das kann bei großen Datenmengen ein paar Minuten dauern.")
    except Exception as exc:
        return CommandReply(f"Backup fehlgeschlagen: {exc}")


def _exec_manual_cleanup(ctx: CommandContext) -> CommandReply:
    """Run the auto-cleanup script now."""
    import subprocess
    from pathlib import Path

    script = Path.home() / ".hermes" / "scripts" / "hermes_cleanup.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0:
            return CommandReply(out or "Cleanup erfolgreich.")
        return CommandReply(f"Cleanup fehlgeschlagen:\n{err or out}")
    except subprocess.TimeoutExpired:
        return CommandReply("Cleanup läuft noch...")
    except Exception as exc:
        return CommandReply(f"Cleanup fehlgeschlagen: {exc}")


def _exec_health(ctx: CommandContext) -> CommandReply:
    """Show system health summary."""
    import subprocess
    from pathlib import Path

    lines = ["Health-Check:"]

    # Gateway
    try:
        proc = subprocess.run(
            [_PYTHON, "-c",
             "import json; p=__import__('pathlib').Path.home()/'.hermes'/'gateway_state.json'; "
             "print(json.loads(p.read_text(encoding='utf-8')).get('gateway_state','?'))"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        gw = (proc.stdout or "").strip() or "?"
    except Exception:
        gw = "?"
    lines.append(f"- Gateway: {gw}")

    # Telegram
    try:
        proc = subprocess.run(
            [_PYTHON, "-c",
             "import json; p=__import__('pathlib').Path.home()/'.hermes'/'gateway_state.json'; "
             "print(json.loads(p.read_text(encoding='utf-8')).get('platforms',{}).get('telegram',{}).get('state','?'))"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        tg = (proc.stdout or "").strip() or "?"
    except Exception:
        tg = "?"
    lines.append(f"- Telegram: {tg}")

    # Hardware quick metrics
    script = Path.home() / ".hermes" / "scripts" / "hermes_hardware_monitor.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script), "--status"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        text = (proc.stdout or "").strip()
        if text:
            lines.append("")
            lines.append("Hardware:")
            for raw in text.splitlines()[:12]:
                lines.append(f"- {raw}")
    except Exception:
        pass

    return CommandReply("\n".join(lines))


def _exec_logs(ctx: CommandContext) -> CommandReply:
    """Show recent Hermes logs."""
    from pathlib import Path
    import os

    logs_dir = Path.home() / ".hermes" / "logs"
    candidates = [
        logs_dir / "gateway.log",
        logs_dir / "gateway_error.log",
        logs_dir / "agent.log",
    ]
    parts = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-80:])
            parts.append(f"=== {path.name} ===\n{tail}")
        except Exception as exc:
            parts.append(f"=== {path.name} ===\nFehler: {exc}")

    if not parts:
        return CommandReply("Keine Logdateien gefunden.")
    return CommandReply("\n\n".join(parts))


def _exec_dashboard(ctx: CommandContext) -> CommandReply:
    """Run the dashboard script now."""
    import subprocess
    from pathlib import Path

    script = Path.home() / ".hermes" / "scripts" / "hermes_dashboard.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        text = (proc.stdout or "").strip() or "Dashboard konnte nicht erstellt werden."
        return CommandReply(text)
    except Exception as exc:
        return CommandReply(f"Dashboard fehlgeschlagen: {exc}")


def _exec_capture(ctx: CommandContext) -> CommandReply:
    """Trigger a visual capture now."""
    import subprocess
    from pathlib import Path

    script = Path.home() / ".hermes" / "scripts" / "hermes_visual_capture.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script), "--now"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        text = (proc.stdout or "").strip() or "Capture fehlgeschlagen."
        return CommandReply(text)
    except Exception as exc:
        return CommandReply(f"Capture fehlgeschlagen: {exc}")


def _exec_update(ctx: CommandContext) -> CommandReply:
    """Core /update text — non-blocking update status for gateway/Telegram."""
    from pathlib import Path

    project_root = Path.home() / ".hermes" / "hermes-agent"
    git_dir = project_root / ".git"

    if not git_dir.exists():
        return CommandReply("Update-Status:\n- Installationspfad ist kein Git-Repo\n- Automatisches Update läuft täglich um 05:00 via Cron")

    try:
        import subprocess
        git_cmd = ["git"]
        if sys.platform == "win32":
            git_cmd = ["git", "-c", "windows.appendAtomically=false"]

        # Quick local check only — no network fetch
        rev_local = subprocess.run(
            git_cmd + ["rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()[:12]

        branch = subprocess.run(
            git_cmd + ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

        return CommandReply(
            f"Update-Status:\n- Branch: {branch}\n- Lokaler Commit: {rev_local}\n- Volles Update läuft täglich um 05:00\n- Manuell: `hermes update` im Terminal"
        )
    except Exception as exc:
        return CommandReply(f"Update-Status nicht verfügbar: {exc}")


def _exec_listen(ctx: CommandContext) -> CommandReply:
    """Run local STT listen script and return transcript."""
    from pathlib import Path

    script = Path.home() / ".hermes" / "scripts" / "hermes_voice_listen.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script), "listen", "8"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        text = (proc.stdout or "").strip()
        if text:
            return CommandReply(f"🎤 {text}")
        err = (proc.stderr or "").strip()
        return CommandReply(f"Kein Transkript.\n{err or 'Bitte Mikro prüfen.'}")
    except Exception as exc:
        return CommandReply(f"Spracheingabe fehlgeschlagen: {exc}")


def _exec_voice_control(ctx: CommandContext) -> CommandReply:
    """Voice control status or test."""
    from pathlib import Path

    script = Path.home() / ".hermes" / "scripts" / "hermes_voice_listen.py"
    try:
        proc = subprocess.run(
            [_PYTHON, str(script), "status"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        text = (proc.stdout or "").strip()
        if text:
            return CommandReply(text)
        return CommandReply("Sprachmodul: Status nicht verfügbar.")
    except Exception as exc:
        return CommandReply(f"Sprachstatus fehlgeschlagen: {exc}")


def _exec_commands(ctx: CommandContext) -> CommandReply:
    """Core gateway /commands body — paginated command + skill listing.

    ``ctx.options["page_size"]`` is a surface parameter (Telegram uses 15,
    everything else 20) — for a fixed context the text is surface-invariant.
    """
    from agent.i18n import t
    from hermes_cli.commands import gateway_help_lines

    raw_args = (ctx.args or "").strip()
    if raw_args:
        try:
            requested_page = int(raw_args)
        except ValueError:
            return CommandReply(t("gateway.commands.usage"), format="markdown")
    else:
        requested_page = 1

    # Build combined entry list: built-in commands + skill commands
    entries = list(gateway_help_lines())
    try:
        from agent.skill_commands import get_skill_commands
        skill_cmds = get_skill_commands()
        if skill_cmds:
            entries.append("")
            entries.append(t("gateway.commands.skill_header"))
            for cmd in sorted(skill_cmds):
                desc = skill_cmds[cmd].get("description", "").strip() or t("gateway.commands.default_desc")
                entries.append(f"`{cmd}` — {desc}")
    except Exception:
        pass

    if not entries:
        return CommandReply(t("gateway.commands.none"), format="markdown")

    try:
        page_size = int(ctx.options.get("page_size", 20))
    except (TypeError, ValueError):
        page_size = 20
    page_size = max(1, page_size)
    total_pages = max(1, (len(entries) + page_size - 1) // page_size)
    page = max(1, min(requested_page, total_pages))
    start = (page - 1) * page_size
    page_entries = entries[start:start + page_size]

    lines = [
        t("gateway.commands.header", total=len(entries), page=page, total_pages=total_pages),
        "",
        *page_entries,
    ]
    if total_pages > 1:
        nav_parts = []
        if page > 1:
            nav_parts.append(t("gateway.commands.nav_prev", page=page - 1))
        if page < total_pages:
            nav_parts.append(t("gateway.commands.nav_next", page=page + 1))
        lines.extend(["", " | ".join(nav_parts)])
    if page != requested_page:
        lines.append(t("gateway.commands.out_of_range", requested=requested_page, page=page))
    return CommandReply("\n".join(lines), format="markdown")


# ---------------------------------------------------------------------------
# Registry + resolution
# ---------------------------------------------------------------------------

EXECUTORS: dict[str, Callable[[CommandContext], CommandReply]] = {
    "version": _exec_version,
    "egress": _exec_egress,
    "profile": _exec_profile,
    "bundles": _exec_bundles,
    "gateway_help": _exec_help,
    "gateway_commands": _exec_commands,
    "status": _exec_status,
    "morning_briefing": _exec_morning_briefing,
    "manual_backup": _exec_manual_backup,
    "manual_cleanup": _exec_manual_cleanup,
    "health": _exec_health,
    "logs": _exec_logs,
    "dashboard": _exec_dashboard,
    "capture": _exec_capture,
    "update": _exec_update,
    "listen": _exec_listen,
    "voice_control": _exec_voice_control,
}


def get_executor_keys() -> frozenset[str]:
    return frozenset(EXECUTORS)


def resolve_executor(cmd_def: Any) -> Callable[[CommandContext], CommandReply] | None:
    """Return the shared executor for ``cmd_def`` (or None when not migrated)."""
    key = getattr(cmd_def, "execute", None)
    if not key:
        return None
    return EXECUTORS.get(key)


def run_execute(cmd_def: Any, ctx: CommandContext) -> CommandReply | None:
    """Run ``cmd_def``'s registry-owned executor, if any."""
    fn = resolve_executor(cmd_def)
    if fn is None:
        return None
    return fn(ctx)


def execute_command(name: str, ctx: CommandContext) -> CommandReply:
    """Run the shared executor for the command named ``name``.

    Raises ``LookupError`` when the command is unknown or not migrated —
    call sites use this only for commands they know carry ``execute``.
    """
    from hermes_cli.commands import resolve_command

    cmd_def = resolve_command(name)
    reply = run_execute(cmd_def, ctx) if cmd_def is not None else None
    if reply is None:
        raise LookupError(f"no registry-owned executor for /{name}")
    return reply
