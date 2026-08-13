"""Validated, preservation-safe reads for Excalidraw document envelopes."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Literal

from tools.file_state import lock_path
from tools.file_tools import _get_file_ops


class ExcalidrawDocumentError(ValueError):
    """Raised when an Excalidraw document cannot be read or validated."""


class ExcalidrawConflictError(ExcalidrawDocumentError):
    """Raised when a mutation's expected document baseline is stale."""


@dataclass(frozen=True)
class DocumentIdentity:
    profile: str
    runtime: str
    path: str


@dataclass(frozen=True)
class ExcalidrawDocument:
    envelope: dict[str, Any]
    fingerprint: str



@dataclass(frozen=True)
class MutationResult:
    path: str
    identity: DocumentIdentity
    fingerprint: str
    affected_ids: list[str]

def validate_path(path: str) -> str:
    """Return the canonical path when it is an absolute Excalidraw document path."""
    if not os.path.isabs(path) or not path.endswith('.excalidraw'):
        raise ExcalidrawDocumentError('path must be absolute and end in .excalidraw')
    return os.path.realpath(path)


def fingerprint_text(text: str) -> str:
    """Return the SHA-256 fingerprint of the exact loaded document text."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def _validate_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise ExcalidrawDocumentError('Excalidraw document root must be a JSON object')
    if envelope.get('type') != 'excalidraw':
        raise ExcalidrawDocumentError("Excalidraw document type must be 'excalidraw'")
    if envelope.get('version') != 2:
        raise ExcalidrawDocumentError('Excalidraw document version must be 2')
    if not isinstance(envelope.get('elements'), list):
        raise ExcalidrawDocumentError('Excalidraw document elements must be a list')
    return envelope


def read_document(path: str, *, task_id: str, profile: str, runtime: str) -> ExcalidrawDocument:
    """Read and validate an Excalidraw envelope through the task file environment."""
    del profile, runtime
    resolved_path = validate_path(path)
    result = _get_file_ops(task_id).read_file_bytes(resolved_path)
    if result.error:
        raise ExcalidrawDocumentError(f'failed to read Excalidraw document: {result.error}')

    try:
        text = base64.b64decode(result.base64_content or '', validate=True).decode('utf-8')
    except (UnicodeDecodeError, ValueError, base64.binascii.Error) as exc:
        raise ExcalidrawDocumentError('failed to decode Excalidraw document text') from exc
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExcalidrawDocumentError(f'failed to parse Excalidraw document: {exc.msg}') from exc

    return ExcalidrawDocument(
        envelope=_validate_envelope(envelope),
        fingerprint=fingerprint_text(text),
    )


def normalize_live_elements(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Return live element dictionaries without altering the stored envelope."""
    return [
        element
        for element in envelope['elements']
        if isinstance(element, dict) and not element.get('isDeleted', False)
    ]



_SUPPORTED_ELEMENT_TYPES = frozenset({
    'arrow', 'diamond', 'ellipse', 'embeddable', 'frame', 'freedraw', 'image', 'line',
    'magicframe', 'rectangle', 'text',
})
_IMMUTABLE_UPDATE_KEYS = frozenset({'id', 'type', 'version', 'versionNonce', 'isDeleted'})
_MUTABLE_UPDATE_KEYS = frozenset({
    'angle', 'backgroundColor', 'boundElements', 'custom', 'customData', 'endArrowhead', 'endBinding',
    'fontFamily', 'fontSize', 'groupIds', 'height', 'link', 'locked', 'opacity', 'points', 'roughness',
    'seed', 'startArrowhead', 'startBinding', 'strokeColor', 'strokeStyle', 'strokeWidth', 'text',
    'verticalAlign', 'width', 'x', 'y',
})


def _require_element_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ExcalidrawDocumentError('element id must be a non-empty string')
    return value


def _validate_new_element(element: Any, files: Any) -> dict[str, Any]:
    if not isinstance(element, dict):
        raise ExcalidrawDocumentError('add payload elements must be objects')
    _require_element_id(element.get('id'))
    element_type = element.get('type')
    if element_type not in _SUPPORTED_ELEMENT_TYPES:
        raise ExcalidrawDocumentError('unsupported Excalidraw element type')
    for field in ('x', 'y', 'width', 'height'):
        if not isinstance(element.get(field), (int, float)) or isinstance(element[field], bool):
            raise ExcalidrawDocumentError(f'element {field} must be a number')
    if element_type in {'arrow', 'line', 'freedraw'}:
        points = element.get('points')
        if not isinstance(points, list) or len(points) < 2 or any(
            not isinstance(point, list) or len(point) != 2 or any(
                not isinstance(value, (int, float)) or isinstance(value, bool) for value in point
            ) for point in points
        ):
            raise ExcalidrawDocumentError('linear and freedraw elements require valid points')
    if element_type == 'text':
        if not isinstance(element.get('text'), str):
            raise ExcalidrawDocumentError('text elements require text')
        if not isinstance(element.get('fontSize'), int) or isinstance(element['fontSize'], bool):
            raise ExcalidrawDocumentError('text elements require fontSize')
        if not isinstance(element.get('fontFamily'), int) or isinstance(element['fontFamily'], bool):
            raise ExcalidrawDocumentError('text elements require fontFamily')
    if element_type == 'image':
        file_id = element.get('fileId')
        if not isinstance(file_id, str) or not file_id or not isinstance(files, dict) or file_id not in files:
            raise ExcalidrawDocumentError('image elements require a fileId linked to envelope files')
        if not isinstance(element.get('status'), str):
            raise ExcalidrawDocumentError('image elements require status')
        scale = element.get('scale')
        if not isinstance(scale, list) or len(scale) != 2 or any(
            not isinstance(value, (int, float)) or isinstance(value, bool) for value in scale
        ):
            raise ExcalidrawDocumentError('image elements require valid scale')
        if element.get('crop') is not None and not isinstance(element.get('crop'), dict):
            raise ExcalidrawDocumentError('image elements require crop to be an object or null')
    return element


_NUMERIC_UPDATE_KEYS = frozenset({'angle', 'height', 'opacity', 'roughness', 'seed', 'strokeWidth', 'width', 'x', 'y'})
_STRING_UPDATE_KEYS = frozenset({
    'backgroundColor', 'endArrowhead', 'link', 'startArrowhead', 'strokeColor', 'strokeStyle', 'verticalAlign',
})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_points(value: Any) -> None:
    if not isinstance(value, list) or len(value) < 2 or any(
        not isinstance(point, list) or len(point) != 2 or not all(_is_number(coordinate) for coordinate in point)
        for point in value
    ):
        raise ExcalidrawDocumentError('points must be a list of numeric coordinate pairs')


def _validate_update_patch(element: dict[str, Any], patch: dict[str, Any]) -> None:
    element_type = element.get('type')
    for key, value in patch.items():
        if key == 'id':
            continue
        if key in _NUMERIC_UPDATE_KEYS and not _is_number(value):
            raise ExcalidrawDocumentError(f'{key} must be a number')
        if key in _STRING_UPDATE_KEYS and value is not None and not isinstance(value, str):
            raise ExcalidrawDocumentError(f'{key} must be a string or null')
        if key == 'points':
            if element_type not in {'arrow', 'line', 'freedraw'}:
                raise ExcalidrawDocumentError('points can only update linear and freedraw elements')
            _validate_points(value)
        if key == 'text':
            if element_type != 'text' or not isinstance(value, str):
                raise ExcalidrawDocumentError('text must be a string on text elements')
        if key in {'fontFamily', 'fontSize'}:
            if element_type != 'text' or not isinstance(value, int) or isinstance(value, bool):
                raise ExcalidrawDocumentError(f'{key} must be an integer on text elements')
        if key == 'boundElements':
            if not isinstance(value, list) or any(not isinstance(binding, dict) for binding in value):
                raise ExcalidrawDocumentError('boundElements must be a list of objects')
        if key in {'endBinding', 'startBinding'} and value is not None and not isinstance(value, dict):
            raise ExcalidrawDocumentError(f'{key} must be an object or null')
        if key == 'customData' and not isinstance(value, dict):
            raise ExcalidrawDocumentError('customData must be an object')
        if key == 'groupIds' and (not isinstance(value, list) or any(not isinstance(group_id, str) for group_id in value)):
            raise ExcalidrawDocumentError('groupIds must be a list of strings')
        if key == 'locked' and not isinstance(value, bool):
            raise ExcalidrawDocumentError('locked must be a boolean')


def _apply_operation(envelope: dict[str, Any], operation: str, payload: list[Any]) -> list[str]:
    if operation not in {'add', 'update', 'delete'}:
        raise ExcalidrawDocumentError('operation must be add, update, or delete')
    if not isinstance(payload, list) or not payload:
        raise ExcalidrawDocumentError('mutation payload must be a non-empty list')
    elements = envelope['elements']
    by_id = {element.get('id'): element for element in elements if isinstance(element, dict)}

    if operation == 'add':
        additions = [_validate_new_element(item, envelope.get('files')) for item in payload]
        ids = [_require_element_id(item['id']) for item in additions]
        if len(ids) != len(set(ids)) or any(element_id in by_id for element_id in ids):
            raise ExcalidrawDocumentError('duplicate element id')
        elements.extend(additions)
        return ids

    if operation == 'delete':
        ids = [_require_element_id(item) for item in payload]
        if len(ids) != len(set(ids)):
            raise ExcalidrawDocumentError('duplicate element id')
        missing = [element_id for element_id in ids if element_id not in by_id]
        if missing:
            raise ExcalidrawDocumentError(f'unknown element id: {missing[0]}')
        for element_id in ids:
            by_id[element_id]['isDeleted'] = True
            by_id[element_id]['version'] = int(by_id[element_id].get('version', 0)) + 1
            by_id[element_id]['versionNonce'] = int(by_id[element_id].get('versionNonce', 0)) + 1
        return ids

    if not all(isinstance(item, dict) for item in payload):
        raise ExcalidrawDocumentError('update payload elements must be objects')
    ids = [_require_element_id(item.get('id')) for item in payload]
    if len(ids) != len(set(ids)):
        raise ExcalidrawDocumentError('duplicate element id')
    missing = [element_id for element_id in ids if element_id not in by_id]
    if missing:
        raise ExcalidrawDocumentError(f'unknown element id: {missing[0]}')
    for patch in payload:
        immutable = _IMMUTABLE_UPDATE_KEYS.intersection(patch) - {'id'}
        if immutable or ('type' in patch and patch['type'] != by_id[patch['id']].get('type')):
            raise ExcalidrawDocumentError('element identity and version fields are immutable')
        unsupported = set(patch) - {'id'} - _MUTABLE_UPDATE_KEYS
        if unsupported:
            raise ExcalidrawDocumentError(f'unsupported update field: {sorted(unsupported)[0]}')
        if len(patch) == 1:
            raise ExcalidrawDocumentError('update patch has no mutable fields')
        _validate_update_patch(by_id[patch['id']], patch)
    for patch in payload:
        element = by_id[patch['id']]
        element.update({key: value for key, value in patch.items() if key != 'id'})
        element['version'] = int(element.get('version', 0)) + 1
        element['versionNonce'] = int(element.get('versionNonce', 0)) + 1
    return ids


def mutate_document(
    path: str,
    *,
    operation: Literal['add', 'update', 'delete'],
    payload: list[Any],
    expected_fingerprint: str | None,
    task_id: str,
    profile: str,
    runtime: str,
) -> MutationResult:
    """Atomically apply one validated structured operation to an Excalidraw scene."""
    resolved_path = validate_path(path)
    with lock_path(resolved_path):
        current = read_document(resolved_path, task_id=task_id, profile=profile, runtime=runtime)
        if expected_fingerprint is not None and expected_fingerprint != current.fingerprint:
            raise ExcalidrawConflictError('document changed since baseline')
        next_envelope = copy.deepcopy(current.envelope)
        affected_ids = _apply_operation(next_envelope, operation, payload)
        serialized = json.dumps(next_envelope, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        result = _get_file_ops(task_id).write_file(resolved_path, serialized)
        if result.error:
            raise ExcalidrawDocumentError(result.error)
    return MutationResult(
        path=resolved_path,
        identity=DocumentIdentity(profile=profile, runtime=runtime, path=resolved_path),
        fingerprint=fingerprint_text(serialized),
        affected_ids=affected_ids,
    )