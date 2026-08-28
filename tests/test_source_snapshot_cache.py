"""Pin the inspect-source snapshot installed by ``tests/conftest.py``.

On this box multiple concurrent agent sessions rewrite files in the SHARED
checkout while suites run, so ``inspect.getsource`` mid-session can return
newer (or half-written) content than the code the process imported. The
conftest wraps ``inspect.getsource`` / ``inspect.getsourcelines`` so the
FIRST read of a file wins for the whole session (see the
"inspect-source snapshot" section there for the mechanism: the file's
linecache entry is rewritten with ``mtime=None``, which
``linecache.checkcache`` deliberately skips).

These tests prove the pinned behavior end to end: rewrite the file on disk,
force the revalidation a mid-run traceback would force, and assert the
original content still comes back. The scenario is ARMED by mutation — with
``_SOURCE_SNAPSHOT_ENABLED`` flipped to ``False`` in tests/conftest.py, the
same scenario returns the rewritten content and these tests go red. See the
arm script recorded in the landing claim (sha256-verified restore).
"""

from __future__ import annotations

import importlib.util
import inspect
import linecache
import sys
import uuid

import pytest


def _make_module(tmp_path, body: str):
    """Write a module file, import it, return (module, path).

    A unique module name per call so reruns in one process never collide.
    """
    name = f"_snapmod_{uuid.uuid4().hex[:12]}"
    path = tmp_path / f"{name}.py"
    path.write_text(body, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod, path


@pytest.fixture()
def module_factory(tmp_path):
    created = []

    def factory(body: str):
        mod, path = _make_module(tmp_path, body)
        created.append((mod.__name__, str(path)))
        return mod, path

    yield factory
    for name, filename in created:
        sys.modules.pop(name, None)
        linecache.cache.pop(filename, None)


def _force_revalidation(path) -> None:
    """What a mid-run traceback does: checkcache the file's linecache entry.

    Without the snapshot pin this evicts the entry (mtime and size both
    changed), and the next getsource re-reads the rewritten file.
    """
    linecache.checkcache(str(path))


class TestFirstReadWins:
    def test_getsource_returns_first_read_after_disk_rewrite(self, module_factory):
        mod, path = module_factory("def f():\n    return 'ORIGINAL'\n")
        first = inspect.getsource(mod.f)
        assert "ORIGINAL" in first

        path.write_text(
            "def f():\n    return 'REWRITTEN-BY-A-SIBLING-SESSION'\n",
            encoding="utf-8",
        )
        _force_revalidation(path)

        second = inspect.getsource(mod.f)
        # With the cache neutered (arm mutation) this shows the rewrite.
        assert second == first
        assert "REWRITTEN" not in second

    def test_getsourcelines_returns_first_read_after_disk_rewrite(
        self, module_factory
    ):
        mod, path = module_factory("def g():\n    return 1\n")
        first_lines, first_no = inspect.getsourcelines(mod.g)

        path.write_text("def g():\n    return 2\n", encoding="utf-8")
        _force_revalidation(path)

        second_lines, second_no = inspect.getsourcelines(mod.g)
        assert second_lines == first_lines
        assert second_no == first_no

    def test_whole_module_getsource_is_pinned_too(self, module_factory):
        mod, path = module_factory("X = 'module-level-ORIGINAL'\n")
        first = inspect.getsource(mod)

        path.write_text("X = 'module-level-CHANGED!'\n", encoding="utf-8")
        _force_revalidation(path)

        assert inspect.getsource(mod) == first


class TestMechanism:
    def test_wrappers_are_installed(self):
        """The conftest wrapper, not stock inspect, must be live."""
        assert getattr(inspect.getsource, "_hermes_source_snapshot", False)
        assert getattr(inspect.getsourcelines, "_hermes_source_snapshot", False)

    def test_first_getsource_pins_the_linecache_entry(self, module_factory):
        """The pin is an mtime=None linecache entry — the exact shape
        ``linecache.checkcache`` documents as "skip, loaded via a loader"."""
        mod, path = module_factory("def h():\n    return 'pinned'\n")
        inspect.getsource(mod.h)
        entry = linecache.cache.get(str(path))
        assert entry is not None and len(entry) == 4
        assert entry[1] is None, (
            "linecache entry still carries a real mtime — checkcache can "
            "evict it and a mid-run rewrite becomes visible"
        )

    def test_transparent_when_nothing_rewrites_the_file(self, module_factory):
        """Byte-identical to stock inspect on an untouched file."""
        body = "def stable():\n    return 'same'\n"
        mod, path = module_factory(body)
        via_wrapper = inspect.getsource(mod.stable)
        real = inspect.getsource._hermes_source_snapshot_real
        assert via_wrapper == real(mod.stable)
        assert via_wrapper == "def stable():\n    return 'same'\n"
