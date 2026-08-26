"""Behavior tests for the bundled Google Drive move command."""

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


API_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/google_api.py"
)
FOLDER_MIME = "application/vnd.google-apps.folder"


@pytest.fixture
def api_module(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    spec = importlib.util.spec_from_file_location("local_google_api_test", API_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._gws_binary = lambda: "/usr/bin/gws"
    module._ensure_authenticated = lambda: None
    return module


def test_move_defaults_to_preview_without_updating(api_module, capsys):
    calls = []

    def fake_gws(parts, *, params=None, body=None):
        calls.append((parts, params, body))
        if parts[-1] == "get" and params["fileId"] == "file-1":
            return {
                "id": "file-1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "parents": ["old-folder"],
                "webViewLink": "https://drive.example/file-1",
            }
        if parts[-1] == "get" and params["fileId"] == "new-folder":
            return {
                "id": "new-folder",
                "name": "Archive",
                "mimeType": FOLDER_MIME,
                "parents": ["root"],
                "webViewLink": "https://drive.example/new-folder",
            }
        if parts[-1] == "list":
            return {"files": [{"id": "duplicate", "name": "Report.pdf"}]}
        raise AssertionError((parts, params, body))

    api_module._run_gws = fake_gws
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="new-folder",
        execute=False,
        allow_cross_drive=False,
    )

    api_module.drive_move(args)

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "preview"
    assert result["file"] == {"id": "file-1", "name": "Report.pdf"}
    assert result["from"] == {"id": "old-folder"}
    assert result["to"] == {"id": "new-folder", "name": "Archive"}
    assert result["duplicateNameWarning"][0]["id"] == "duplicate"
    assert result["requiresConfirmation"] is True
    assert not any(parts[-1] == "update" for parts, _, _ in calls)


def test_execute_moves_once_and_verifies_parent(api_module, capsys):
    calls = []
    source_reads = 0

    def fake_gws(parts, *, params=None, body=None):
        nonlocal source_reads
        calls.append((parts, params, body))
        if parts[-1] == "get" and params["fileId"] == "file-1":
            source_reads += 1
            return {
                "id": "file-1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "parents": ["old-folder"] if source_reads == 1 else ["new-folder"],
            }
        if parts[-1] == "get" and params["fileId"] == "new-folder":
            return {
                "id": "new-folder",
                "name": "Archive",
                "mimeType": FOLDER_MIME,
                "parents": ["root"],
            }
        if parts[-1] == "list":
            return {"files": [{"id": "duplicate", "name": "Report.pdf"}]}
        if parts[-1] == "update":
            return {"id": "file-1", "parents": ["new-folder"]}
        raise AssertionError((parts, params, body))

    api_module._run_gws = fake_gws
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="new-folder",
        execute=True,
        allow_cross_drive=False,
    )

    api_module.drive_move(args)

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "moved"
    assert result["verified"] is True
    assert result["duplicateNameWarning"] == [{"id": "duplicate", "name": "Report.pdf"}]
    assert result["from"] == {"id": "old-folder"}
    assert result["to"] == {"id": "new-folder", "name": "Archive"}
    assert result["rollback"] == {
        "fileId": "file-1",
        "to": "old-folder",
    }
    updates = [(params, body) for parts, params, body in calls if parts[-1] == "update"]
    assert updates == [({
        "fileId": "file-1",
        "addParents": "new-folder",
        "removeParents": "old-folder",
        "supportsAllDrives": True,
        "fields": api_module._DRIVE_MOVE_FIELDS,
    }, None)]
    assert source_reads == 2


def test_cross_drive_execute_requires_separate_override(api_module):
    calls = []

    def fake_gws(parts, *, params=None, body=None):
        calls.append((parts, params, body))
        if parts[-1] == "get" and params["fileId"] == "file-1":
            return {
                "id": "file-1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "parents": ["old-folder"],
            }
        if parts[-1] == "get" and params["fileId"] == "shared-folder":
            return {
                "id": "shared-folder",
                "name": "Shared Archive",
                "mimeType": FOLDER_MIME,
                "parents": ["shared-root"],
                "driveId": "shared-drive-1",
            }
        if parts[-1] == "list":
            return {"files": []}
        if parts[-1] == "update":
            return {"id": "file-1", "parents": ["shared-folder"]}
        raise AssertionError((parts, params, body))

    api_module._run_gws = fake_gws
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="shared-folder",
        execute=True,
        allow_cross_drive=False,
    )

    with pytest.raises(SystemExit, match="--allow-cross-drive"):
        api_module.drive_move(args)

    assert not any(parts[-1] == "update" for parts, _, _ in calls)


def test_cross_drive_override_allows_verified_move(api_module, capsys):
    calls = []
    source_reads = 0

    def fake_gws(parts, *, params=None, body=None):
        nonlocal source_reads
        calls.append((parts, params, body))
        if parts[-1] == "get" and params["fileId"] == "file-1":
            source_reads += 1
            return {
                "id": "file-1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "parents": ["old-folder"] if source_reads == 1 else ["shared-folder"],
                **({} if source_reads == 1 else {"driveId": "shared-drive-1"}),
            }
        if parts[-1] == "get" and params["fileId"] == "shared-folder":
            return {
                "id": "shared-folder",
                "name": "Shared Archive",
                "mimeType": FOLDER_MIME,
                "parents": ["shared-root"],
                "driveId": "shared-drive-1",
            }
        if parts[-1] == "list":
            return {"files": []}
        if parts[-1] == "update":
            return {"id": "file-1", "parents": ["shared-folder"]}
        raise AssertionError((parts, params, body))

    api_module._run_gws = fake_gws
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="shared-folder",
        execute=True,
        allow_cross_drive=True,
    )

    api_module.drive_move(args)

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "moved"
    assert result["verified"] is True
    assert result["crossDrive"] is True
    assert sum(parts[-1] == "update" for parts, _, _ in calls) == 1


def test_cross_drive_override_allows_shared_drive_to_my_drive(api_module, capsys):
    calls = []
    source_reads = 0

    def fake_gws(parts, *, params=None, body=None):
        nonlocal source_reads
        assert params is not None
        calls.append((parts, params, body))
        if parts[-1] == "get" and params["fileId"] == "file-1":
            source_reads += 1
            return {
                "id": "file-1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "parents": ["shared-folder"] if source_reads == 1 else ["my-folder"],
                **({"driveId": "shared-drive-1"} if source_reads == 1 else {}),
            }
        if parts[-1] == "get" and params["fileId"] == "my-folder":
            return {
                "id": "my-folder",
                "name": "My Drive Archive",
                "mimeType": FOLDER_MIME,
                "parents": ["root"],
            }
        if parts[-1] == "list":
            return {"files": []}
        if parts[-1] == "update":
            return {"id": "file-1", "parents": ["my-folder"]}
        raise AssertionError((parts, params, body))

    api_module._run_gws = fake_gws
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="my-folder",
        execute=True,
        allow_cross_drive=True,
    )

    api_module.drive_move(args)

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "moved"
    assert result["verified"] is True
    assert result["crossDrive"] is True
    assert sum(parts[-1] == "update" for parts, _, _ in calls) == 1


def test_same_folder_is_verified_noop(api_module, capsys):
    calls = []

    def fake_gws(parts, *, params=None, body=None):
        calls.append((parts, params, body))
        if parts[-1] == "get" and params["fileId"] == "file-1":
            return {
                "id": "file-1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "parents": ["folder-1"],
            }
        if parts[-1] == "get" and params["fileId"] == "folder-1":
            return {
                "id": "folder-1",
                "name": "Current",
                "mimeType": FOLDER_MIME,
                "parents": ["root"],
            }
        if parts[-1] == "list":
            return {"files": []}
        if parts[-1] == "update":
            return {"id": "file-1", "parents": ["folder-1"]}
        raise AssertionError((parts, params, body))

    api_module._run_gws = fake_gws
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="folder-1",
        execute=True,
        allow_cross_drive=False,
    )

    api_module.drive_move(args)

    result = json.loads(capsys.readouterr().out)
    assert result == {
        "status": "unchanged",
        "verified": True,
        "file": {"id": "file-1", "name": "Report.pdf"},
        "parent": {"id": "folder-1", "name": "Current"},
    }
    assert not any(parts[-1] in {"list", "update"} for parts, _, _ in calls)


def test_non_folder_destination_is_rejected_before_write(api_module):
    calls = []

    def fake_gws(parts, *, params=None, body=None):
        calls.append((parts, params, body))
        if parts[-1] == "get" and params["fileId"] == "file-1":
            return {
                "id": "file-1",
                "name": "Report.pdf",
                "mimeType": "application/pdf",
                "parents": ["folder-1"],
            }
        if parts[-1] == "get" and params["fileId"] == "not-a-folder":
            return {
                "id": "not-a-folder",
                "name": "Other.pdf",
                "mimeType": "application/pdf",
                "parents": ["root"],
            }
        raise AssertionError((parts, params, body))

    api_module._run_gws = fake_gws
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="not-a-folder",
        execute=True,
        allow_cross_drive=False,
    )

    with pytest.raises(SystemExit, match="not a Google Drive folder"):
        api_module.drive_move(args)

    assert not any(parts[-1] in {"list", "update"} for parts, _, _ in calls)


def test_folder_cannot_be_moved_into_itself(api_module):
    api_module._run_gws = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("No Drive API call should happen for a self move")
    )
    args = argparse.Namespace(
        file_id="folder-1",
        destination_id="folder-1",
        execute=False,
        allow_cross_drive=False,
    )

    with pytest.raises(SystemExit, match="cannot be moved into itself"):
        api_module.drive_move(args)


def test_cli_exposes_preview_execute_and_cross_drive_flags():
    result = subprocess.run(
        [sys.executable, str(API_PATH), "drive", "move", "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "FILE_ID" in result.stdout
    assert "--to DESTINATION_ID" in result.stdout
    assert "--execute" in result.stdout
    assert "--allow-cross-drive" in result.stdout


def test_python_client_fallback_executes_and_verifies(api_module, capsys):
    api_module._gws_binary = lambda: None
    calls = []
    source_reads = 0

    class Request:
        def __init__(self, payload):
            self.payload = payload

        def execute(self):
            return self.payload

    class Files:
        def get(self, **params):
            nonlocal source_reads
            calls.append(("get", params))
            if params["fileId"] == "file-1":
                source_reads += 1
                return Request({
                    "id": "file-1",
                    "name": "Report.pdf",
                    "mimeType": "application/pdf",
                    "parents": ["old-folder"] if source_reads == 1 else ["new-folder"],
                })
            return Request({
                "id": "new-folder",
                "name": "Archive",
                "mimeType": FOLDER_MIME,
                "parents": ["root"],
            })

        def list(self, **params):
            calls.append(("list", params))
            return Request({"files": []})

        def update(self, **params):
            calls.append(("update", params))
            return Request({"id": "file-1", "parents": ["new-folder"]})

    class Service:
        def __init__(self):
            self._files = Files()

        def files(self):
            return self._files

    service = Service()
    api_module.build_service = lambda api, version: service
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="new-folder",
        execute=True,
        allow_cross_drive=False,
    )

    api_module.drive_move(args)

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "moved"
    assert result["verified"] is True
    update = next(params for name, params in calls if name == "update")
    assert update["addParents"] == "new-folder"
    assert update["removeParents"] == "old-folder"
    assert update["supportsAllDrives"] is True
    assert source_reads == 2


def test_execute_fails_loudly_when_parent_readback_disagrees(api_module):
    reads = iter([
        {
            "id": "file-1",
            "name": "Report.pdf",
            "mimeType": "application/pdf",
            "parents": ["old-folder"],
        },
        {
            "id": "new-folder",
            "name": "Archive",
            "mimeType": FOLDER_MIME,
            "parents": ["root"],
        },
        {
            "id": "file-1",
            "name": "Report.pdf",
            "mimeType": "application/pdf",
            "parents": ["old-folder"],
        },
    ])
    api_module._drive_move_get = lambda file_id: next(reads)
    api_module._drive_move_duplicates = lambda *args: []
    api_module._drive_move_update = lambda *args: {
        "id": "file-1",
        "parents": ["new-folder"],
    }
    args = argparse.Namespace(
        file_id="file-1",
        destination_id="new-folder",
        execute=True,
        allow_cross_drive=False,
    )

    with pytest.raises(SystemExit, match="parent verification failed"):
        api_module.drive_move(args)
