"""Structured, atomic Excalidraw scene tools."""

from __future__ import annotations

from threading import RLock
from typing import Any

from gateway.session_context import get_session_env
from tools import desktop_ui
from tools.approval import request_tool_approval
from tools.excalidraw_document import ExcalidrawDocumentError, mutate_document, normalize_live_elements, read_document, validate_path
from tools.registry import registry, tool_error, tool_result


_FOCUSED_DRAWINGS: dict[tuple[str, str], tuple[str, ...]] = {}
_FOCUSED_DRAWINGS_LOCK = RLock()


def set_focused_drawings(session_id: str, profile: str, paths: list[str]) -> None:
    """Replace the focused drawing identities for one Desktop session/profile.

    The Desktop transport owns updating this seam as focus changes.  Tool calls
    resolve an omitted path only when this snapshot has exactly one identity.
    """
    with _FOCUSED_DRAWINGS_LOCK:
        _FOCUSED_DRAWINGS[(session_id, profile)] = tuple(paths)


def _resolve_path(path: Any, *, session_id: str | None, profile: str) -> str:
    if isinstance(path, str) and path:
        return path
    with _FOCUSED_DRAWINGS_LOCK:
        paths = _FOCUSED_DRAWINGS.get((session_id or '', profile), ())
    if len(paths) == 1:
        return paths[0]
    if not paths:
        raise ExcalidrawDocumentError('path is required: no focused Desktop drawing is available')
    raise ExcalidrawDocumentError('path is required: exactly one focused Desktop drawing is required')
def _context(kw: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(kw.get('task_id') or 'default'),
        str(kw.get('profile') or 'default'),
        str(kw.get('runtime') or 'local'),
    )


def _path_or_error(path: Any, *, session_id: str | None, profile: str) -> str:
    return _resolve_path(path, session_id=session_id, profile=profile)


def _mutation_response(result) -> str:
    payload = {
        'path': result.path,
        'profile': result.identity.profile,
        'runtime': result.identity.runtime,
        'fingerprint': result.fingerprint,
        'affected_ids': result.affected_ids,
    }
    desktop_ui.emit('excalidraw.changed', {
        'path': result.path,
        'profile': result.identity.profile,
        'runtime': result.identity.runtime,
        'fingerprint': result.fingerprint,
    })
    return tool_result(payload)


def excalidraw_read_tool(*, path: str, task_id: str, profile: str, runtime: str) -> str:
    document = read_document(path, task_id=task_id, profile=profile, runtime=runtime)
    return tool_result({
        'path': path,
        'profile': profile,
        'runtime': runtime,
        'fingerprint': document.fingerprint,
        'elements': normalize_live_elements(document.envelope),
    })


def _mutation_tool(*, operation: str, path: str, payload: list[Any], expected_fingerprint: str | None, task_id: str, profile: str, runtime: str) -> str:
    result = mutate_document(
        path,
        operation=operation,
        payload=payload,
        expected_fingerprint=expected_fingerprint,
        task_id=task_id,
        profile=profile,
        runtime=runtime,
    )
    return _mutation_response(result)


def excalidraw_add_tool(*, path: str, elements: list[Any], expected_fingerprint: str | None, task_id: str, profile: str, runtime: str) -> str:
    return _mutation_tool(operation='add', path=path, payload=elements, expected_fingerprint=expected_fingerprint, task_id=task_id, profile=profile, runtime=runtime)


def excalidraw_update_tool(*, path: str, patches: list[Any], expected_fingerprint: str | None, task_id: str, profile: str, runtime: str) -> str:
    return _mutation_tool(operation='update', path=path, payload=patches, expected_fingerprint=expected_fingerprint, task_id=task_id, profile=profile, runtime=runtime)


def excalidraw_delete_tool(*, path: str, ids: list[str], expected_fingerprint: str | None, task_id: str, profile: str, runtime: str) -> str:
    return _mutation_tool(operation='delete', path=path, payload=ids, expected_fingerprint=expected_fingerprint, task_id=task_id, profile=profile, runtime=runtime)


def open_excalidraw(
    path: str, *, task_id: str, session_id: str | None = None,
    profile: str = 'default', runtime: str = 'local',
) -> str:
    """Request one-shot Desktop opening for a validated saved drawing."""
    del session_id
    resolved_path = validate_path(path)
    document = read_document(resolved_path, task_id=task_id, profile=profile, runtime=runtime)
    if get_session_env('HERMES_SESSION_SOURCE', '') != 'desktop' or not desktop_ui.available():
        return resolved_path
    request = f'Open {resolved_path} in the Excalidraw pane?'
    approval = request_tool_approval(
        'open_excalidraw', request,
        allow_permanent=False,
        allow_session=False,
        display_target=request,
    )
    if approval.get('approved'):
        desktop_ui.emit('excalidraw.open', {
            'path': resolved_path,
            'profile': profile,
            'runtime': runtime,
            'fingerprint': document.fingerprint,
        })
    return resolved_path


def _handle_open(args: dict[str, Any], **kw: Any) -> str:
    task_id, profile, runtime = _context(kw)
    try:
        path = _path_or_error(args.get('path'), session_id=kw.get('session_id'), profile=profile)
        return tool_result({'path': open_excalidraw(
            path, task_id=task_id, session_id=kw.get('session_id'), profile=profile, runtime=runtime,
        )})
    except ExcalidrawDocumentError as exc:
        return tool_error(str(exc))


def _handle_read(args: dict[str, Any], **kw: Any) -> str:
    task_id, profile, runtime = _context(kw)
    try:
        return excalidraw_read_tool(
            path=_path_or_error(args.get('path'), session_id=kw.get('session_id'), profile=profile),
            task_id=task_id,
            profile=profile,
            runtime=runtime,
        )
    except ExcalidrawDocumentError as exc:
        return tool_error(str(exc))


def _handle_mutation(operation: str, payload_key: str, args: dict[str, Any], **kw: Any) -> str:
    task_id, profile, runtime = _context(kw)
    try:
        path = _path_or_error(args.get('path'), session_id=kw.get('session_id'), profile=profile)
        payload = args.get(payload_key)
        if not isinstance(payload, list):
            raise ExcalidrawDocumentError(f'{payload_key} must be a list')
        return _mutation_tool(operation=operation, path=path, payload=payload, expected_fingerprint=args.get('expected_fingerprint'), task_id=task_id, profile=profile, runtime=runtime)
    except ExcalidrawDocumentError as exc:
        return tool_error(str(exc))


def _schema(name: str, description: str, payload_key: str, payload_description: str) -> dict[str, Any]:
    return {
        'name': name,
        'description': description,
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Absolute .excalidraw document path.'},
                payload_key: {'type': 'array', 'description': payload_description},
                'expected_fingerprint': {'type': 'string', 'description': 'Fingerprint returned by excalidraw_read.'},
            },
            'required': [payload_key],
        },
    }


EXCALIDRAW_READ_SCHEMA = {
    'name': 'excalidraw_read',
    'description': 'Read the live elements and fingerprint from an Excalidraw document.',
    'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}},
}
EXCALIDRAW_ADD_SCHEMA = _schema('excalidraw_add', 'Atomically add complete Excalidraw elements.', 'elements', 'Complete elements to add.')
EXCALIDRAW_UPDATE_SCHEMA = _schema('excalidraw_update', 'Atomically update mutable fields of existing Excalidraw elements.', 'patches', 'Element patches with id and mutable fields.')
EXCALIDRAW_DELETE_SCHEMA = _schema('excalidraw_delete', 'Atomically soft-delete existing Excalidraw elements.', 'ids', 'Element ids to soft-delete.')

EXCALIDRAW_OPEN_SCHEMA = {
    'name': 'open_excalidraw',
    'description': 'Ask once before opening a saved Excalidraw drawing in the Desktop pane.',
    'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path']},
}

registry.register(name='excalidraw_read', toolset='excalidraw', schema=EXCALIDRAW_READ_SCHEMA, handler=_handle_read)
registry.register(name='excalidraw_add', toolset='excalidraw', schema=EXCALIDRAW_ADD_SCHEMA, handler=lambda args, **kw: _handle_mutation('add', 'elements', args, **kw))
registry.register(name='excalidraw_update', toolset='excalidraw', schema=EXCALIDRAW_UPDATE_SCHEMA, handler=lambda args, **kw: _handle_mutation('update', 'patches', args, **kw))
registry.register(name='excalidraw_delete', toolset='excalidraw', schema=EXCALIDRAW_DELETE_SCHEMA, handler=lambda args, **kw: _handle_mutation('delete', 'ids', args, **kw))
registry.register(name='open_excalidraw', toolset='excalidraw', schema=EXCALIDRAW_OPEN_SCHEMA, handler=_handle_open)
