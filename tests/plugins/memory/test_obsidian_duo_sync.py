from plugins.memory.obsidian_duo.sync import CommandSyncAdapter, NoopSyncAdapter


def test_noop_sync_is_successful_without_process():
    result = NoopSyncAdapter().sync("turn")

    assert result.success is True
    assert result.attempted is False


def test_command_sync_debounces_dirty_writes(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("plugins.memory.obsidian_duo.sync.subprocess.run", fake_run)
    adapter = CommandSyncAdapter(["obsidian", "sync"], debounce_seconds=30)

    adapter.mark_dirty("one")
    adapter.mark_dirty("two")
    adapter.mark_dirty("three")
    result = adapter.flush()

    assert result.success is True
    assert len(calls) == 1
    assert calls[0][1]["shell"] is False


def test_three_durable_writes_inside_quiet_window_run_one_sync(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr("plugins.memory.obsidian_duo.sync.subprocess.run", fake_run)
    adapter = CommandSyncAdapter(["obsidian", "sync"], debounce_seconds=30)
    adapter.mark_dirty("promotion")
    adapter.mark_dirty("promotion")
    adapter.mark_dirty("promotion")

    assert adapter.flush().attempted is True
    assert len(calls) == 1


def test_command_failure_is_degraded_and_remains_dirty(monkeypatch):
    monkeypatch.setattr(
        "plugins.memory.obsidian_duo.sync.subprocess.run",
        lambda *args, **kwargs: type("Completed", (), {"returncode": 1, "stderr": "failed"})(),
    )
    adapter = CommandSyncAdapter(["obsidian", "sync"])
    adapter.mark_dirty("turn")

    result = adapter.flush()

    assert result.success is False
    assert adapter.dirty is True
