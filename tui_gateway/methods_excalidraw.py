"""Focused Excalidraw drawing JSON-RPC handler."""
from pathlib import Path



from .method_ctx import HandlerRegistry

_registry = HandlerRegistry()
method = _registry.method



@method("excalidraw.focus")
def _(rid, params: dict) -> dict:
    """Replace the focused drawing snapshot for one live Desktop session."""
    session, err = _sess(params, rid)
    if err:
        return err
    expected_profile = Path(str(session["profile_home"])).name if session.get("profile_home") else _current_profile_name()

    profile = str(params.get("profile") or "").strip() or "default"
    if profile != expected_profile:
        return _err(rid, 4001, "focused drawing profile does not match the session")

    raw_paths = params.get("paths")
    if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
        return _err(rid, 4001, "paths must be a list of absolute .excalidraw paths")

    try:
        from tools.excalidraw_document import validate_path

        paths = [validate_path(path) for path in raw_paths]
    except ValueError as exc:
        return _err(rid, 4001, str(exc))

    from tools.excalidraw_tools import set_focused_drawings

    session_id = str(session.get("session_key") or "")
    if not session_id:
        return _err(rid, 4001, "focused drawing session is unavailable")
    set_focused_drawings(session_id, profile, paths)
    return _ok(rid, {"paths": paths})


def register(server) -> None:
    _registry.install(server)
