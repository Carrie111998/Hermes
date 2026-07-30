"""Platform-neutral contract tests for the Windows NT skill filesystem.

These tests exercise orchestration with fake handles on POSIX.  They are not a
claim that the Windows kernel ABI has been executed; real Windows CI remains
the integration authority for the ctypes calls.
"""

from __future__ import annotations

import stat

import pytest

from tools import nt_secure_fs as ntfs


def test_native_backend_probe_is_false_off_windows():
    if ntfs.os.name != "nt":
        assert ntfs.is_available() is False


def _metadata(
    inode: int,
    *,
    directory: bool = False,
    size: int = 0,
) -> ntfs.NtStat:
    return ntfs.NtStat(
        st_dev=7,
        st_ino=inode,
        st_mode=(stat.S_IFDIR if directory else stat.S_IFREG) | 0o600,
        st_size=size,
        st_mtime_ns=11,
        st_ctime_ns=13,
        file_attributes=(
            ntfs.FILE_ATTRIBUTE_DIRECTORY if directory else 0
        ),
    )


class _ContextHandle:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True


def test_read_regular_file_is_identity_and_content_stable():
    class File(_ContextHandle):
        closed = False

        def stat(self):
            return _metadata(3, size=4)

        def read_all(self):
            return b"data"

    file_handle = File()

    class Parent:
        def open_file(self, name, *, writable):
            assert (name, writable) == ("SKILL.md", False)
            return file_handle

    payload, metadata = ntfs.read_regular_file(Parent(), "SKILL.md")
    assert payload == b"data"
    assert metadata.identity == (7, 3)
    assert file_handle.closed is True


def test_read_regular_file_rejects_change_during_read():
    class File(_ContextHandle):
        closed = False
        calls = 0

        def stat(self):
            self.calls += 1
            return _metadata(self.calls, size=4)

        def read_all(self):
            return b"data"

    class Parent:
        def open_file(self, name, *, writable):
            return File()

    with pytest.raises(RuntimeError, match="changed while being read"):
        ntfs.read_regular_file(Parent(), "SKILL.md")


def test_replace_regular_file_renames_temporary_handle_relative():
    events = []

    class Existing(_ContextHandle):
        closed = False

        def stat(self):
            return _metadata(1)

    class Temporary(_ContextHandle):
        closed = False

        def write_all(self, payload):
            events.append(("write", payload))

        def flush(self):
            events.append(("flush",))

        def rename_to(self, parent, name, *, replace):
            events.append(("rename", parent, name, replace))

        def mark_delete(self, *, is_directory):
            events.append(("delete", is_directory))

    temporary = Temporary()

    class Parent:
        def open_file(self, name, *, create=False, writable=False):
            if name == "target.md":
                return Existing()
            assert (name, create, writable) == (
                ".tmp",
                True,
                True,
            )
            return temporary

        def flush(self):
            events.append(("parent-flush",))

    parent = Parent()
    ntfs.replace_regular_file(
        parent,
        "target.md",
        b"new",
        require_existing=True,
        temp_name=".tmp",
    )
    assert events[:3] == [
        ("write", b"new"),
        ("flush",),
        ("rename", parent, "target.md", True),
    ]
    assert not any(event[0] == "delete" for event in events)


def test_delete_tree_unlinks_reparse_object_without_traversing_it():
    events = []

    class Reparse(_ContextHandle):
        closed = False

        def stat(self):
            return ntfs.NtStat(
                st_dev=7,
                st_ino=23,
                st_mode=stat.S_IFDIR | 0o600,
                st_size=0,
                st_mtime_ns=11,
                st_ctime_ns=13,
                file_attributes=(
                    ntfs.FILE_ATTRIBUTE_DIRECTORY
                    | ntfs.FILE_ATTRIBUTE_REPARSE_POINT
                ),
            )

        def mark_delete(self, *, is_directory):
            events.append(("delete-reparse", is_directory))

    class Directory:
        def list_entries(self):
            return [
                ntfs.NtDirectoryEntry(
                    "redirect",
                    ntfs.FILE_ATTRIBUTE_DIRECTORY
                    | ntfs.FILE_ATTRIBUTE_REPARSE_POINT,
                    0,
                    file_id=23,
                    st_mtime_ns=11,
                    st_ctime_ns=13,
                )
            ]

        def open_reparse_entry(
            self, name, *, directory, writable
        ):
            events.append(("open-reparse", name, directory, writable))
            return Reparse()

        def open_dir(self, *args, **kwargs):
            raise AssertionError("a reparse target must never be traversed")

    ntfs.delete_tree(Directory())
    assert events == [
        ("open-reparse", "redirect", True, True),
        ("delete-reparse", True),
    ]


def test_delete_tree_rejects_name_reopen_identity_swap():
    class File(_ContextHandle):
        def stat(self):
            return _metadata(99, size=4)

        def mark_delete(self, *, is_directory):
            raise AssertionError("identity mismatch must be rejected before delete")

    class Directory:
        def list_entries(self):
            return [
                ntfs.NtDirectoryEntry(
                    "marker.txt",
                    0,
                    4,
                    file_id=2,
                    st_mtime_ns=11,
                    st_ctime_ns=13,
                )
            ]

        def open_file(self, name, *, writable):
            assert (name, writable) == ("marker.txt", True)
            return File()

    with pytest.raises(RuntimeError, match="changed before recursive delete"):
        ntfs.delete_tree(Directory())


def test_snapshot_rejects_name_reopen_identity_swap(tmp_path):
    class File(_ContextHandle):
        def stat(self):
            return _metadata(99, size=4)

        def read_all(self, *, max_bytes):
            raise AssertionError("identity mismatch must be rejected before read")

    class Directory:
        def stat(self):
            return _metadata(1, directory=True)

        def list_entries(self, *, max_entries):
            assert max_entries == ntfs._SNAPSHOT_MAX_ENTRIES
            return [
                ntfs.NtDirectoryEntry(
                    "marker.txt",
                    0,
                    4,
                    file_id=2,
                    st_mtime_ns=11,
                    st_ctime_ns=13,
                )
            ]

        def open_file(self, name, *, writable, stable_read):
            assert (name, writable, stable_read) == (
                "marker.txt",
                False,
                True,
            )
            return File()

    with pytest.raises(RuntimeError, match="changed before snapshot"):
        ntfs.copy_tree_no_reparse(
            Directory(), tmp_path / "snapshot"
        )


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "NUL",
        "child. ",
        "a?b",
        "COM¹.txt",
        "CONIN$",
        " NUL",
        " foo",
    ],
)
def test_snapshot_rejects_win32_aliasing_names(tmp_path, unsafe_name):
    class Directory:
        def stat(self):
            return _metadata(1, directory=True)

        def list_entries(self, *, max_entries):
            return [ntfs.NtDirectoryEntry(unsafe_name, 0, 0)]

    with pytest.raises(ValueError):
        ntfs.copy_tree_no_reparse(
            Directory(), tmp_path / "snapshot"
        )


@pytest.mark.parametrize(
    "component",
    ["", ".", "..", "a/b", "a\\b", "C:", "nul\x00name"],
)
def test_handle_relative_components_are_single_names(component):
    with pytest.raises(ValueError):
        ntfs._validate_component(component)


def test_native_abi_uses_sdk_boolean_layout_for_legacy_delete_and_rename():
    assert (
        ntfs._FILE_DISPOSITION_INFO_BUFFER.DeleteFile.size == 1
    )
    assert (
        ntfs._FILE_RENAME_INFO_LEGACY_BUFFER.ReplaceIfExists.size == 1
    )
    assert (
        ntfs._FILE_RENAME_INFO_LEGACY_BUFFER.RootDirectory.offset
        > ntfs._FILE_RENAME_INFO_LEGACY_BUFFER.ReplaceIfExists.offset
    )
