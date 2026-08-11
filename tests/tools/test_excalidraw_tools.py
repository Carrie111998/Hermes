import json
import os

from gateway.session_context import set_session_vars
from tools import desktop_ui
from tools import excalidraw_tools
from tools import file_tools
from tools.registry import registry

SCENE_PATH = '/tmp/scene.excalidraw'
RESOLVED_SCENE_PATH = os.path.realpath(SCENE_PATH)


def restore_session_vars(tokens):
    for token in reversed(tokens):
        token.var.reset(token)


def test_registers_structured_scene_tools():
    assert {registry.get_entry(name).toolset for name in (
        'excalidraw_read', 'excalidraw_add', 'excalidraw_update', 'excalidraw_delete',
    )} == {'excalidraw'}


def test_open_tool_requires_approval_before_emitting_desktop_event(monkeypatch):
    document = type('Document', (), {'fingerprint': 'fingerprint'})()
    approvals = []
    events = []
    monkeypatch.setattr(excalidraw_tools, 'read_document', lambda *args, **kwargs: document)
    monkeypatch.setattr(excalidraw_tools.desktop_ui, 'available', lambda: True)
    monkeypatch.setattr(excalidraw_tools, 'request_tool_approval', lambda *args, **kwargs: approvals.append((args, kwargs)) or {'approved': True})
    monkeypatch.setattr(excalidraw_tools.desktop_ui, 'emit', lambda event, payload: events.append((event, payload)) or True)
    tokens = set_session_vars(source='desktop')
    try:
        assert excalidraw_tools.open_excalidraw(SCENE_PATH, task_id='t', session_id='desktop-session') == RESOLVED_SCENE_PATH
    finally:
        restore_session_vars(tokens)
    request = f'Open {RESOLVED_SCENE_PATH} in the Excalidraw pane?'
    assert approvals == [(('open_excalidraw', request), {
        'allow_permanent': False, 'allow_session': False,
        'display_target': request,
    })]
    assert events == [('excalidraw.open', {
        'path': RESOLVED_SCENE_PATH, 'profile': 'default', 'runtime': 'local', 'fingerprint': 'fingerprint',
    })]

def test_open_tool_returns_path_without_desktop_or_after_denial(monkeypatch):
    document = type('Document', (), {'fingerprint': 'fingerprint'})()
    monkeypatch.setattr(excalidraw_tools, 'read_document', lambda *args, **kwargs: document)
    monkeypatch.setattr(excalidraw_tools.desktop_ui, 'available', lambda: False)
    assert excalidraw_tools.open_excalidraw(SCENE_PATH, task_id='t') == RESOLVED_SCENE_PATH

    events = []
    tokens = set_session_vars(source='desktop')
    try:
        monkeypatch.setattr(excalidraw_tools.desktop_ui, 'available', lambda: True)
        monkeypatch.setattr(excalidraw_tools, 'request_tool_approval', lambda *args, **kwargs: {'approved': False})
        monkeypatch.setattr(excalidraw_tools.desktop_ui, 'emit', lambda event, payload: events.append((event, payload)) or True)
        assert excalidraw_tools.open_excalidraw(SCENE_PATH, task_id='t') == RESOLVED_SCENE_PATH
    finally:
        restore_session_vars(tokens)
    assert events == []


def test_open_tool_skips_generic_tui_emitter_without_desktop_attachment(monkeypatch):
    document = type('Document', (), {'fingerprint': 'fingerprint'})()
    approvals = []
    events = []
    monkeypatch.setattr(excalidraw_tools, 'read_document', lambda *args, **kwargs: document)
    monkeypatch.setattr(excalidraw_tools.desktop_ui, 'available', lambda: True)
    monkeypatch.setattr(excalidraw_tools, 'request_tool_approval', lambda *args, **kwargs: approvals.append(True))
    monkeypatch.setattr(excalidraw_tools.desktop_ui, 'emit', lambda event, payload: events.append((event, payload)) or True)
    tokens = set_session_vars(source='tui')
    try:
        assert excalidraw_tools.open_excalidraw(SCENE_PATH, task_id='t') == RESOLVED_SCENE_PATH
    finally:
        restore_session_vars(tokens)
    assert approvals == []
    assert events == []


def test_generic_excalidraw_write_never_requests_open_approval(monkeypatch):
    approvals = []
    events = []
    result = type('Result', (), {'to_dict': lambda self: {}})()
    monkeypatch.setattr(file_tools, '_resolve_path_for_task', lambda path, task_id: path)
    monkeypatch.setattr(file_tools, '_get_file_ops', lambda task_id: type('Ops', (), {'write_file': lambda self, path, content: result})())
    monkeypatch.setattr(file_tools, '_check_file_staleness', lambda path, task_id: None)
    monkeypatch.setattr(file_tools, '_path_resolution_warning', lambda path, resolved, task_id: None)
    monkeypatch.setattr(excalidraw_tools, 'request_tool_approval', lambda *args, **kwargs: approvals.append(True))
    desktop_ui.set_emitter(lambda sid, event, payload: events.append((event, payload)))
    try:
        assert json.loads(file_tools.write_file_tool('/tmp/generic.excalidraw', '{}', task_id='t'))['resolved_path'] == '/tmp/generic.excalidraw'
    finally:
        desktop_ui.set_emitter(None)
    assert approvals == []
    assert events == []


def test_registers_open_tool_without_generic_write_hook():
    assert registry.get_entry('open_excalidraw').toolset == 'excalidraw'
    assert 'excalidraw' not in registry.get_entry('write_file').name


def test_skill_opens_only_after_successful_write():
    with open('skills/creative/excalidraw/SKILL.md', encoding='utf-8') as file:
        skill = file.read()
    assert skill.index('write_file') < skill.index('Only after `write_file` succeeds') < skill.index('open_excalidraw')


def test_add_tool_returns_mutation_identity(monkeypatch):
    captured = {}

    class Result:
        path = '/tmp/scene.excalidraw'
        fingerprint = 'new-fingerprint'
        affected_ids = ['r2']
        identity = type('Identity', (), {'profile': 'default', 'runtime': 'local', 'path': path})()

    def mutate(path, **kwargs):
        captured['path'] = path
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(excalidraw_tools, 'mutate_document', mutate)

    output = json.loads(registry.dispatch('excalidraw_add', {
        'path': '/tmp/scene.excalidraw',
        'elements': [{'id': 'r2', 'type': 'rectangle', 'x': 1, 'y': 2, 'width': 3, 'height': 4}],
        'expected_fingerprint': 'baseline',
    }, task_id='t', profile='default', runtime='local'))

    assert captured == {
        'path': '/tmp/scene.excalidraw',
        'operation': 'add',
        'payload': [{'id': 'r2', 'type': 'rectangle', 'x': 1, 'y': 2, 'width': 3, 'height': 4}],
        'expected_fingerprint': 'baseline',
        'task_id': 't',
        'profile': 'default',
        'runtime': 'local',
    }
    assert output == {
        'path': '/tmp/scene.excalidraw',
        'profile': 'default',
        'runtime': 'local',
        'fingerprint': 'new-fingerprint',
        'affected_ids': ['r2'],
    }


def test_mutation_emits_changed_event_only_after_durable_write(monkeypatch):
    calls = []
    desktop_ui.set_emitter(lambda sid, event, payload: calls.append((event, payload)))
    try:
        class Result:
            path = '/tmp/scene.excalidraw'
            fingerprint = 'new-fingerprint'
            affected_ids = ['r1']
            identity = type('Identity', (), {'profile': 'default', 'runtime': 'local', 'path': path})()

        monkeypatch.setattr(excalidraw_tools, 'mutate_document', lambda *args, **kwargs: Result())
        output = json.loads(excalidraw_tools.excalidraw_delete_tool(
            path='/tmp/scene.excalidraw', ids=['r1'], expected_fingerprint=None,
            task_id='t', profile='default', runtime='local',
        ))
    finally:
        desktop_ui.set_emitter(None)

    assert output['fingerprint'] == 'new-fingerprint'
    assert calls == [('excalidraw.changed', {
        'path': '/tmp/scene.excalidraw', 'profile': 'default', 'runtime': 'local', 'fingerprint': 'new-fingerprint',
    })]



def test_omitted_path_requires_exactly_one_focused_drawing():
    excalidraw_tools.set_focused_drawings('desktop-session', 'default', [])
    assert 'path is required' in json.loads(excalidraw_tools._handle_read({}, session_id='desktop-session', profile='default'))['error']

    excalidraw_tools.set_focused_drawings('desktop-session', 'default', ['/tmp/one.excalidraw'])
    assert excalidraw_tools._resolve_path(None, session_id='desktop-session', profile='default') == '/tmp/one.excalidraw'

    excalidraw_tools.set_focused_drawings('desktop-session', 'default', ['/tmp/one.excalidraw', '/tmp/two.excalidraw'])
    assert 'exactly one focused' in json.loads(excalidraw_tools._handle_read({}, session_id='desktop-session', profile='default'))['error']


def test_failed_durable_write_returns_error_without_changed_event(monkeypatch):
    calls = []
    desktop_ui.set_emitter(lambda sid, event, payload: calls.append((event, payload)))
    try:
        def fail(*args, **kwargs):
            raise excalidraw_tools.ExcalidrawDocumentError('write denied')

        monkeypatch.setattr(excalidraw_tools, 'mutate_document', fail)
        output = json.loads(registry.dispatch('excalidraw_delete', {
            'path': '/tmp/scene.excalidraw', 'ids': ['r1'],
        }, task_id='t', profile='default', runtime='local'))
    finally:
        desktop_ui.set_emitter(None)

    assert output['error'] == 'write denied'
    assert calls == []