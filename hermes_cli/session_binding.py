"""Deterministic native-session binding for supervised child runs.

Mission Control (and any other programmatic supervisor) needs to know which
native Hermes session a child run created or resumed, early and unambiguously
— even under ``chat -Q --source mission-control``, where stdout chatter is
suppressed and the ``session_id:`` line only appears at exit.

The mechanism: the supervisor passes ``chat --session-id-file <path>`` (or
sets ``HERMES_SESSION_ID_FILE``), with a path unique to that run. The runtime
atomically writes the live session id to that file the moment the id exists
— at startup for both fresh and resumed sessions, again on ``/new``
rotation, and whenever a mid-run continuation session takes over. The file
therefore ALWAYS names the currently-live session; supervisors never have
to guess from "latest session".

Profile-safe by construction: paths come from the caller, and session ids
are read from the live CLI object — nothing global is consulted.
"""

import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)


def write_session_id_file(path: Optional[str], session_id: Optional[str]) -> bool:
    """Atomically write *session_id* to *path* (temp file + rename).

    Concurrency-safe: each run gets its own binding path (supplied by the
    supervisor), and ``os.replace`` makes each individual write atomic so a
    reader never observes a torn value. Best-effort: failures are logged and
    return False rather than breaking the chat run — the binding file is a
    correlation aid, not a correctness dependency of the session itself.

    Returns True when the file was written.
    """
    if not path or not session_id:
        return False
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".session-id-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(session_id))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:
        logger.warning("Failed to write session-id file %r: %s", path, exc)
        return False


def resolve_session_id_file(env=None) -> Optional[str]:
    """Return the configured binding path, or None.

    Reads ``HERMES_SESSION_ID_FILE`` (set by ``chat --session-id-file`` in
    hermes_cli/main.py, mirroring the ``--source`` bridge). An empty value
    disables the binding.
    """
    value = (env if env is not None else os.environ).get("HERMES_SESSION_ID_FILE")
    return value.strip() or None if value else None
