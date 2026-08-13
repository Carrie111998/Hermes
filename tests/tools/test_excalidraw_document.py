import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from tools.excalidraw_document import (
    ExcalidrawConflictError,
    ExcalidrawDocumentError,
    mutate_document,
    normalize_live_elements,
    read_document,
)

def _file_ops_for(content):
    return SimpleNamespace(
        read_file_bytes=lambda read_path: SimpleNamespace(
            base64_content=base64.b64encode(content.encode('utf-8')).decode('ascii'),
            error=None,
        ),
    )


def test_read_preserves_unknown_envelope_and_element_fields(tmp_path, monkeypatch):
    path = tmp_path / 'scene.excalidraw'
    content = json.dumps({
        'type': 'excalidraw',
        'version': 2,
        'vendor': {'x': 1},
        'elements': [
            {
                'id': 'r1',
                'type': 'rectangle',
                'x': 0,
                'y': 0,
                'width': 1,
                'height': 1,
                'custom': True,
            },
            {'id': 'gone', 'type': 'rectangle', 'isDeleted': True},
        ],
    })
    path.write_text(content, encoding='utf-8')
    monkeypatch.setattr(
        'tools.excalidraw_document._get_file_ops',
        lambda task_id: _file_ops_for(content),
    )

    document = read_document(str(path), task_id='t', profile='default', runtime='local')

    assert document.envelope['vendor'] == {'x': 1}
    assert normalize_live_elements(document.envelope) == [
        {
            'id': 'r1',
            'type': 'rectangle',
            'x': 0,
            'y': 0,
            'width': 1,
            'height': 1,
            'custom': True,
        },
    ]
    assert document.fingerprint == hashlib.sha256(content.encode('utf-8')).hexdigest()


def test_read_uses_complete_content_beyond_display_pagination(monkeypatch):
    envelope = {
        'type': 'excalidraw',
        'version': 2,
        'elements': [{'id': str(index), 'type': 'rectangle'} for index in range(2_001)],
    }
    content = json.dumps(envelope, indent=2)
    monkeypatch.setattr('tools.excalidraw_document._get_file_ops', lambda task_id: _file_ops_for(content))

    document = read_document('/tmp/scene.excalidraw', task_id='t', profile='default', runtime='local')

    assert len(document.envelope['elements']) == 2_001
    assert document.fingerprint == hashlib.sha256(content.encode('utf-8')).hexdigest()


def test_read_uses_complete_content_beyond_display_line_limit(monkeypatch):
    content = json.dumps({
        'type': 'excalidraw',
        'version': 2,
        'elements': [],
        'appState': {'longValue': 'x' * 20_000},
    })
    monkeypatch.setattr('tools.excalidraw_document._get_file_ops', lambda task_id: _file_ops_for(content))

    document = read_document('/tmp/scene.excalidraw', task_id='t', profile='default', runtime='local')

    assert document.envelope['appState']['longValue'] == 'x' * 20_000
    assert document.fingerprint == hashlib.sha256(content.encode('utf-8')).hexdigest()


@pytest.mark.parametrize(
    ('envelope', 'message'),
    [
        ([], 'root'),
        ({'type': 'wrong', 'version': 2, 'elements': []}, 'type'),
        ({'type': 'excalidraw', 'version': 1, 'elements': []}, 'version'),
        ({'type': 'excalidraw', 'version': 2, 'elements': {}}, 'elements'),
    ],
)
def test_read_rejects_invalid_envelopes(envelope, message, monkeypatch):
    content = json.dumps(envelope)
    monkeypatch.setattr(
        'tools.excalidraw_document._get_file_ops',
        lambda task_id: _file_ops_for(content),
    )

    with pytest.raises(ExcalidrawDocumentError, match=message):
        read_document('/tmp/scene.excalidraw', task_id='t', profile='default', runtime='local')


@pytest.mark.parametrize('path', ['relative.excalidraw', '/tmp/wrong.json'])
def test_read_rejects_non_absolute_or_wrong_extension_before_io(path, monkeypatch):
    def fail_if_called(task_id):
        raise AssertionError('file operations must not be created for invalid paths')

    monkeypatch.setattr('tools.excalidraw_document._get_file_ops', fail_if_called)

    with pytest.raises(ExcalidrawDocumentError, match=r'absolute.*\.excalidraw'):
        read_document(path, task_id='t', profile='default', runtime='local')


def _scene_file_ops(path):
    def read_file_bytes(read_path):
        return SimpleNamespace(
            base64_content=base64.b64encode(path.read_bytes()).decode('ascii'),
            error=None,
        )

    def write_file(write_path, content):
        path.write_text(content, encoding='utf-8')
        return SimpleNamespace(error=None)

    return SimpleNamespace(read_file_bytes=read_file_bytes, write_file=write_file)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    path = tmp_path / 'scene.excalidraw'
    path.write_text(json.dumps({
        'type': 'excalidraw',
        'version': 2,
        'appState': {'gridSize': 20},
        'files': {'asset': {'dataURL': 'opaque'}},
        'vendor': {'preserved': True},
        'elements': [{
            'id': 'r1', 'type': 'rectangle', 'x': 0, 'y': 0, 'width': 1, 'height': 1,
            'version': 1, 'versionNonce': 2, 'custom': 'preserved',
        }],
    }), encoding='utf-8')
    monkeypatch.setattr('tools.excalidraw_document._get_file_ops', lambda task_id: _scene_file_ops(path))
    return path


def test_duplicate_add_rejects_without_changing_file(scene):
    before = scene.read_text(encoding='utf-8')

    with pytest.raises(ExcalidrawDocumentError, match='duplicate element id'):
        mutate_document(str(scene), operation='add', payload=[{
            'id': 'r1', 'type': 'rectangle', 'x': 2, 'y': 2, 'width': 1, 'height': 1,
        }], expected_fingerprint=None, task_id='t', profile='default', runtime='local')

    assert scene.read_text(encoding='utf-8') == before


def test_update_rejects_identity_change_and_stale_baseline(scene):
    with pytest.raises(ExcalidrawDocumentError, match='immutable'):
        mutate_document(str(scene), operation='update', payload=[{'id': 'r1', 'type': 'ellipse'}], expected_fingerprint=None, task_id='t', profile='default', runtime='local')
    with pytest.raises(ExcalidrawConflictError):
        mutate_document(str(scene), operation='delete', payload=['r1'], expected_fingerprint='stale', task_id='t', profile='default', runtime='local')


@pytest.mark.parametrize('operation,payload', [
    ('add', [{'id': 'r2', 'type': 'ellipse', 'x': 2, 'y': 2, 'width': 3, 'height': 4}]),
    ('update', [{'id': 'r1', 'x': 5, 'custom': 'updated'}]),
    ('delete', ['r1']),
])
def test_mutations_preserve_opaque_data_and_return_new_identity(scene, operation, payload):
    before = read_document(str(scene), task_id='t', profile='default', runtime='local')

    result = mutate_document(str(scene), operation=operation, payload=payload, expected_fingerprint=before.fingerprint, task_id='t', profile='default', runtime='local')
    after = read_document(str(scene), task_id='t', profile='default', runtime='local')

    assert result.identity.path == str(scene.resolve())
    assert result.identity.profile == 'default'
    assert result.fingerprint == after.fingerprint
    assert result.fingerprint != before.fingerprint
    assert after.envelope['appState'] == {'gridSize': 20}
    assert after.envelope['files'] == {'asset': {'dataURL': 'opaque'}}
    assert after.envelope['vendor'] == {'preserved': True}
    if operation == 'add':
        assert after.envelope['elements'][-1]['id'] == 'r2'
    elif operation == 'update':
        assert after.envelope['elements'][0]['custom'] == 'updated'
        assert after.envelope['elements'][0]['version'] == 2
    else:
        assert after.envelope['elements'][0]['isDeleted'] is True


@pytest.mark.parametrize('operation,payload,match', [
    ('add', [{'id': 'r2', 'type': 'rectangle', 'x': 0, 'y': 0, 'width': 1, 'height': 1}, {'id': 'r2', 'type': 'rectangle', 'x': 0, 'y': 0, 'width': 1, 'height': 1}], 'duplicate element id'),
    ('add', [{'id': 'r2', 'type': 'unsupported', 'x': 0, 'y': 0, 'width': 1, 'height': 1}], 'unsupported'),
    ('update', [{'id': 'missing', 'x': 1}], 'unknown element id'),
    ('delete', ['missing'], 'unknown element id'),
])
def test_invalid_mutation_batches_leave_bytes_unchanged(scene, operation, payload, match):
    before = scene.read_text(encoding='utf-8')

    with pytest.raises(ExcalidrawDocumentError, match=match):
        mutate_document(str(scene), operation=operation, payload=payload, expected_fingerprint=None, task_id='t', profile='default', runtime='local')

    assert scene.read_text(encoding='utf-8') == before


@pytest.mark.parametrize('patch,match', [
    ({'id': 'r1', 'x': True}, 'x must be a number'),
    ({'id': 'r1', 'opacity': 'opaque'}, 'opacity must be a number'),
    ({'id': 'r1', 'points': [[0, 'bad'], [1, 1]]}, 'points'),
    ({'id': 'r1', 'text': 1}, 'text must be a string'),
    ({'id': 'r1', 'fontSize': 12.5}, 'fontSize must be an integer'),
    ({'id': 'r1', 'boundElements': [1]}, 'boundElements'),
])
def test_update_rejects_invalid_typed_values_without_write(scene, patch, match):
    before = scene.read_text(encoding='utf-8')

    with pytest.raises(ExcalidrawDocumentError, match=match):
        mutate_document(str(scene), operation='update', payload=[patch], expected_fingerprint=None, task_id='t', profile='default', runtime='local')

    assert scene.read_text(encoding='utf-8') == before


def test_update_validates_every_patch_before_writing_batch(scene):
    envelope = json.loads(scene.read_text(encoding='utf-8'))
    envelope['elements'].append({'id': 'r2', 'type': 'rectangle', 'x': 0, 'y': 0, 'width': 1, 'height': 1})
    scene.write_text(json.dumps(envelope), encoding='utf-8')
    before = scene.read_text(encoding='utf-8')

    with pytest.raises(ExcalidrawDocumentError, match='x must be a number'):
        mutate_document(
            str(scene),
            operation='update',
            payload=[{'id': 'r1', 'x': 10}, {'id': 'r2', 'x': True}],
            expected_fingerprint=None,
            task_id='t',
            profile='default',
            runtime='local',
        )

    assert scene.read_text(encoding='utf-8') == before


def test_same_baseline_cannot_commit_twice(scene):
    baseline = read_document(str(scene), task_id='t', profile='default', runtime='local').fingerprint
    mutate_document(str(scene), operation='update', payload=[{'id': 'r1', 'x': 1}], expected_fingerprint=baseline, task_id='first', profile='default', runtime='local')

    with pytest.raises(ExcalidrawConflictError):
        mutate_document(str(scene), operation='update', payload=[{'id': 'r1', 'x': 2}], expected_fingerprint=baseline, task_id='second', profile='default', runtime='local')


@pytest.mark.parametrize('element,match', [
    ({'id': 'line', 'type': 'line', 'x': 0, 'y': 0, 'width': 1, 'height': 1}, 'points'),
    ({'id': 'free', 'type': 'freedraw', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'points': [[0]]}, 'points'),
    ({'id': 'text', 'type': 'text', 'x': 0, 'y': 0, 'width': 1, 'height': 1}, 'text'),
    ({'id': 'image', 'type': 'image', 'x': 0, 'y': 0, 'width': 1, 'height': 1}, 'fileId'),
])
def test_add_rejects_malformed_type_specific_shapes_without_write(scene, element, match):
    before = scene.read_text(encoding='utf-8')
    with pytest.raises(ExcalidrawDocumentError, match=match):
        mutate_document(str(scene), operation='add', payload=[element], expected_fingerprint=None, task_id='t', profile='default', runtime='local')
    assert scene.read_text(encoding='utf-8') == before


def test_add_accepts_image_linked_to_envelope_file(scene):
    result = mutate_document(str(scene), operation='add', payload=[{
        'id': 'image', 'type': 'image', 'x': 0, 'y': 0, 'width': 1, 'height': 1,
        'fileId': 'asset', 'status': 'saved', 'scale': [1, 1], 'crop': None,
    }], expected_fingerprint=None, task_id='t', profile='default', runtime='local')

    assert result.affected_ids == ['image']
    assert read_document(str(scene), task_id='t', profile='default', runtime='local').envelope['elements'][-1]['fileId'] == 'asset'


@pytest.mark.parametrize('image,match', [
    ({'id': 'image', 'type': 'image', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'status': 'saved', 'scale': [1, 1], 'crop': None}, 'fileId'),
    ({'id': 'image', 'type': 'image', 'x': 0, 'y': 0, 'width': 1, 'height': 1, 'fileId': 'missing', 'status': 'saved', 'scale': [1, 1], 'crop': None}, 'fileId'),
])
def test_add_rejects_unlinked_image_without_write(scene, image, match):
    before = scene.read_text(encoding='utf-8')
    with pytest.raises(ExcalidrawDocumentError, match=match):
        mutate_document(str(scene), operation='add', payload=[image], expected_fingerprint=None, task_id='t', profile='default', runtime='local')
    assert scene.read_text(encoding='utf-8') == before
