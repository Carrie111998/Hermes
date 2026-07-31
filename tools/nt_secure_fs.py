"""Windows NT handle-relative filesystem operations for skill packages.

The ordinary Win32 path APIs resolve the complete path again for every
operation.  That is not strong enough for skill discovery or mutation: a
junction swapped into any parent between validation and use can redirect a
read, replacement, rename, or recursive delete.

This module binds an absolute root with ``NtCreateFile`` and
``OBJ_DONT_REPARSE`` and resolves every descendant one component at a time
through ``OBJECT_ATTRIBUTES.RootDirectory``.  Every returned object is
identified from its handle, and namespace mutations use handle-relative
``SetFileInformationByHandle`` calls.  No security decision is made from a
subsequent string-path resolution.

The module is importable on non-Windows hosts so its public contract can be
mock-tested there.  Native calls are initialized lazily and fail explicitly
when invoked off Windows.
"""

from __future__ import annotations

import ctypes
import os
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence


# OBJECT_ATTRIBUTES flags.
OBJ_CASE_INSENSITIVE = 0x00000040
OBJ_DONT_REPARSE = 0x00001000

# Native create dispositions and options.
FILE_OPEN = 0x00000001
FILE_CREATE = 0x00000002
FILE_DIRECTORY_FILE = 0x00000001
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000

# File-specific access rights.
FILE_READ_DATA = 0x00000001
FILE_LIST_DIRECTORY = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_ADD_FILE = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_ADD_SUBDIRECTORY = 0x00000004
FILE_READ_EA = 0x00000008
FILE_WRITE_EA = 0x00000010
FILE_TRAVERSE = 0x00000020
FILE_DELETE_CHILD = 0x00000040
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
DELETE = 0x00010000
READ_CONTROL = 0x00020000
SYNCHRONIZE = 0x00100000

FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
_ALL_SHARES = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE

FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

# SetFileInformationByHandle classes and flags.
FILE_RENAME_INFO = 3
FILE_DISPOSITION_INFO = 4
FILE_DISPOSITION_INFO_EX = 21
FILE_RENAME_INFO_EX = 22
FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x00000001
FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x00000002
FILE_DISPOSITION_FLAG_DELETE = 0x00000001
FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE = 0x00000010

ERROR_NO_MORE_FILES = 18
ERROR_INVALID_PARAMETER = 87
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class NtSecureFsUnavailable(OSError):
    """The native Windows backend is not available in this process."""


@dataclass(frozen=True)
class NtStat:
    """The stat fields used by the skill subsystems, obtained from a handle."""

    st_dev: int
    st_ino: int
    st_mode: int
    st_size: int
    st_mtime_ns: int
    st_ctime_ns: int
    file_attributes: int

    @property
    def identity(self) -> tuple[int, int]:
        return self.st_dev, self.st_ino

    @property
    def signature(self) -> tuple[int, int, int, int, int]:
        return (
            self.st_dev,
            self.st_ino,
            self.st_size,
            self.st_mtime_ns,
            self.st_ctime_ns,
        )

    @property
    def is_dir(self) -> bool:
        return stat_module.S_ISDIR(self.st_mode)

    @property
    def is_file(self) -> bool:
        return stat_module.S_ISREG(self.st_mode)


@dataclass(frozen=True)
class NtDirectoryEntry:
    name: str
    file_attributes: int
    size: int
    file_id: int = 0
    st_mtime_ns: int = 0
    st_ctime_ns: int = 0

    @property
    def is_dir(self) -> bool:
        return bool(self.file_attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse(self) -> bool:
        return bool(self.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
        ("Attributes", ctypes.c_ulong),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IO_STATUS_BLOCK_UNION(ctypes.Union):
    _fields_ = [("Status", ctypes.c_long), ("Pointer", ctypes.c_void_p)]


class _IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [
        ("u", _IO_STATUS_BLOCK_UNION),
        ("Information", ctypes.c_size_t),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_ulong),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_ulong),
        ("nFileSizeHigh", ctypes.c_ulong),
        ("nFileSizeLow", ctypes.c_ulong),
        ("nNumberOfLinks", ctypes.c_ulong),
        ("nFileIndexHigh", ctypes.c_ulong),
        ("nFileIndexLow", ctypes.c_ulong),
    ]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", ctypes.c_ulong), ("ReparseTag", ctypes.c_ulong)]


class _FILE_BASIC_INFO(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_ulong),
    ]


class _FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
    _fields_ = [
        ("NextEntryOffset", ctypes.c_ulong),
        ("FileIndex", ctypes.c_ulong),
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("AllocationSize", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_ulong),
        ("FileNameLength", ctypes.c_ulong),
        ("EaSize", ctypes.c_ulong),
        ("ShortNameLength", ctypes.c_byte),
        ("ShortName", ctypes.c_wchar * 12),
        ("FileId", ctypes.c_longlong),
        ("FileName", ctypes.c_wchar * 1),
    ]


class _FILE_RENAME_INFO_BUFFER(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_ulong),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_ulong),
        ("FileName", ctypes.c_wchar * 1),
    ]


class _FILE_RENAME_INFO_LEGACY_BUFFER(ctypes.Structure):
    _fields_ = [
        ("ReplaceIfExists", ctypes.c_ubyte),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_ulong),
        ("FileName", ctypes.c_wchar * 1),
    ]


class _FILE_DISPOSITION_INFO_EX_BUFFER(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_ulong)]


class _FILE_DISPOSITION_INFO_BUFFER(ctypes.Structure):
    _fields_ = [("DeleteFile", ctypes.c_ubyte)]


class _NativeApi:
    def __init__(self) -> None:
        if os.name != "nt":
            raise NtSecureFsUnavailable(
                "NT handle-relative filesystem operations require Windows"
            )

        from ctypes import wintypes

        self.wintypes = wintypes
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.NtCreateFile = self.ntdll.NtCreateFile
        self.NtCreateFile.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong,
            ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self.NtCreateFile.restype = ctypes.c_long

        self.RtlNtStatusToDosError = self.ntdll.RtlNtStatusToDosError
        self.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
        self.RtlNtStatusToDosError.restype = ctypes.c_ulong

        self.CloseHandle = self.kernel32.CloseHandle
        self.CloseHandle.argtypes = [ctypes.c_void_p]
        self.CloseHandle.restype = ctypes.c_int

        self.GetFileInformationByHandle = self.kernel32.GetFileInformationByHandle
        self.GetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        self.GetFileInformationByHandle.restype = ctypes.c_int

        self.GetFileInformationByHandleEx = (
            self.kernel32.GetFileInformationByHandleEx
        )
        self.GetFileInformationByHandleEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self.GetFileInformationByHandleEx.restype = ctypes.c_int

        self.GetFinalPathNameByHandleW = self.kernel32.GetFinalPathNameByHandleW
        self.GetFinalPathNameByHandleW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        self.GetFinalPathNameByHandleW.restype = ctypes.c_ulong

        self.ReadFile = self.kernel32.ReadFile
        self.ReadFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
        ]
        self.ReadFile.restype = ctypes.c_int

        self.WriteFile = self.kernel32.WriteFile
        self.WriteFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
        ]
        self.WriteFile.restype = ctypes.c_int

        self.FlushFileBuffers = self.kernel32.FlushFileBuffers
        self.FlushFileBuffers.argtypes = [ctypes.c_void_p]
        self.FlushFileBuffers.restype = ctypes.c_int

        self.SetFileInformationByHandle = (
            self.kernel32.SetFileInformationByHandle
        )
        self.SetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self.SetFileInformationByHandle.restype = ctypes.c_int

    def winerror(self, code: Optional[int] = None) -> OSError:
        if code is None:
            code = ctypes.get_last_error()
        return ctypes.WinError(code)

    def raise_status(self, status: int, operation: str) -> None:
        unsigned = status & 0xFFFFFFFF
        dos_error = int(self.RtlNtStatusToDosError(ctypes.c_long(status)))
        error = ctypes.WinError(dos_error)
        error.args = (
            dos_error,
            f"{operation} failed (NTSTATUS 0x{unsigned:08x}): {error.strerror}",
        )
        raise error


_native_api: Optional[_NativeApi] = None


def is_available() -> bool:
    if os.name != "nt":
        return False
    try:
        _api()
    except (AttributeError, OSError):
        return False
    return True


def _api() -> _NativeApi:
    global _native_api
    if _native_api is None:
        _native_api = _NativeApi()
    return _native_api


def _validate_component(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "\\" in name
        or "/" in name
        or "\x00" in name
        or ":" in name
    ):
        raise ValueError(f"invalid handle-relative path component: {name!r}")


def _filetime_ticks_to_unix_ns(ticks: int) -> int:
    return max(0, (int(ticks) - 116444736000000000) * 100)


def _validate_snapshot_component(name: str) -> None:
    """Reject NT names that Win32 destination paths cannot preserve exactly."""
    _validate_component(name)
    if name[0] == " " or name[-1] in (" ", ".") or any(
        ord(character) < 32 or character in '<>"|?*'
        for character in name
    ):
        raise ValueError(f"unsafe Win32 snapshot component: {name!r}")
    device_base = name.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    device_digits = tuple("123456789¹²³")
    reserved.update(f"COM{digit}" for digit in device_digits)
    reserved.update(f"LPT{digit}" for digit in device_digits)
    if device_base in reserved:
        raise ValueError(f"reserved Win32 snapshot component: {name!r}")


def _nt_absolute_name(path: Path) -> str:
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\UNC\\"):
        return "\\??\\UNC\\" + absolute[8:]
    if absolute.startswith("\\\\?\\"):
        return "\\??\\" + absolute[4:]
    if absolute.startswith("\\\\"):
        return "\\??\\UNC\\" + absolute[2:]
    return "\\??\\" + absolute


def _unicode_string(value: str) -> tuple[ctypes.Array, _UNICODE_STRING]:
    buffer = ctypes.create_unicode_buffer(value)
    length = len(value.encode("utf-16-le"))
    return buffer, _UNICODE_STRING(
        Length=length,
        MaximumLength=length + 2,
        Buffer=ctypes.cast(buffer, ctypes.c_void_p),
    )


def _open_native(
    name: str,
    *,
    root: Optional["NtHandle"],
    directory: bool,
    create: bool,
    writable: bool,
    allow_final_reparse: bool = False,
    share_access: int = _ALL_SHARES,
) -> "NtHandle":
    api = _api()
    if root is not None:
        _validate_component(name)
    name_buffer, unicode_name = _unicode_string(name)
    attributes = _OBJECT_ATTRIBUTES(
        Length=ctypes.sizeof(_OBJECT_ATTRIBUTES),
        RootDirectory=ctypes.c_void_p(root.raw if root is not None else 0),
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=(
            OBJ_CASE_INSENSITIVE
            | (0 if allow_final_reparse else OBJ_DONT_REPARSE)
        ),
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    if directory:
        access = (
            FILE_LIST_DIRECTORY
            | FILE_TRAVERSE
            | FILE_READ_ATTRIBUTES
            | SYNCHRONIZE
        )
        if writable:
            access |= (
                FILE_ADD_FILE
                | FILE_ADD_SUBDIRECTORY
                | FILE_DELETE_CHILD
                | FILE_WRITE_ATTRIBUTES
                | DELETE
            )
        options = (
            FILE_DIRECTORY_FILE
            | FILE_SYNCHRONOUS_IO_NONALERT
            | FILE_OPEN_REPARSE_POINT
        )
    else:
        access = FILE_READ_DATA | FILE_READ_ATTRIBUTES | SYNCHRONIZE
        if writable or create:
            access |= (
                FILE_WRITE_DATA
                | FILE_APPEND_DATA
                | FILE_WRITE_ATTRIBUTES
                | DELETE
            )
        options = (
            FILE_NON_DIRECTORY_FILE
            | FILE_SYNCHRONOUS_IO_NONALERT
            | FILE_OPEN_REPARSE_POINT
        )

    handle_value = ctypes.c_void_p()
    io_status = _IO_STATUS_BLOCK()
    status = api.NtCreateFile(
        ctypes.byref(handle_value),
        access,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        FILE_ATTRIBUTE_NORMAL,
        share_access,
        FILE_CREATE if create else FILE_OPEN,
        options,
        None,
        0,
    )
    # Keep the backing UTF-16 allocation live through NtCreateFile.  The
    # UNICODE_STRING stores only its address, not a Python reference.
    del name_buffer
    if status < 0:
        api.raise_status(status, f"NtCreateFile({name!r})")
    handle = NtHandle(int(handle_value.value), writable=writable or create)
    try:
        metadata = handle.stat()
        if (
            metadata.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT
            and not allow_final_reparse
        ):
            raise OSError(f"refusing reparse point: {name}")
        if directory != metadata.is_dir:
            raise NotADirectoryError(name) if directory else IsADirectoryError(name)
    except BaseException:
        handle.close()
        raise
    return handle


class NtHandle:
    """Owned native handle opened without traversing a reparse point."""

    __slots__ = ("raw", "writable", "_closed")

    def __init__(self, raw: int, *, writable: bool) -> None:
        if not raw or raw == INVALID_HANDLE_VALUE:
            raise ValueError("invalid NT handle")
        self.raw = raw
        self.writable = writable
        self._closed = False

    def __enter__(self) -> "NtHandle":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            if not _api().CloseHandle(ctypes.c_void_p(self.raw)):
                raise _api().winerror()
            self._closed = True

    def stat(self) -> NtStat:
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _api().GetFileInformationByHandle(
            ctypes.c_void_p(self.raw), ctypes.byref(info)
        ):
            raise _api().winerror()
        attrs = int(info.dwFileAttributes)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            tag_info = _FILE_ATTRIBUTE_TAG_INFO()
            if not _api().GetFileInformationByHandleEx(
                ctypes.c_void_p(self.raw),
                9,  # FileAttributeTagInfo
                ctypes.byref(tag_info),
                ctypes.sizeof(tag_info),
            ):
                raise _api().winerror()
            if tag_info.ReparseTag:
                attrs |= FILE_ATTRIBUTE_REPARSE_POINT
        basic_info = _FILE_BASIC_INFO()
        if not _api().GetFileInformationByHandleEx(
            ctypes.c_void_p(self.raw),
            0,  # FileBasicInfo
            ctypes.byref(basic_info),
            ctypes.sizeof(basic_info),
        ):
            raise _api().winerror()
        file_id = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)

        mode = (
            stat_module.S_IFDIR | 0o700
            if attrs & FILE_ATTRIBUTE_DIRECTORY
            else stat_module.S_IFREG | 0o600
        )
        return NtStat(
            st_dev=int(info.dwVolumeSerialNumber),
            st_ino=file_id,
            st_mode=mode,
            st_size=size,
            st_mtime_ns=_filetime_ticks_to_unix_ns(
                basic_info.LastWriteTime
            ),
            st_ctime_ns=_filetime_ticks_to_unix_ns(
                basic_info.ChangeTime
            ),
            file_attributes=attrs,
        )

    @property
    def identity(self) -> tuple[int, int]:
        return self.stat().identity

    def final_path(self) -> Path:
        api = _api()
        needed = api.GetFinalPathNameByHandleW(
            ctypes.c_void_p(self.raw), None, 0, 0
        )
        if not needed:
            raise api.winerror()
        buffer = ctypes.create_unicode_buffer(needed + 1)
        written = api.GetFinalPathNameByHandleW(
            ctypes.c_void_p(self.raw), buffer, len(buffer), 0
        )
        if not written or written >= len(buffer):
            raise api.winerror()
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(value)

    def open_dir(
        self, name: str, *, create: bool = False, writable: Optional[bool] = None
    ) -> "NtHandle":
        return _open_native(
            name,
            root=self,
            directory=True,
            create=create,
            writable=self.writable if writable is None else writable,
            allow_final_reparse=False,
        )

    def open_file(
        self,
        name: str,
        *,
        create: bool = False,
        writable: bool = False,
        stable_read: bool = False,
    ) -> "NtHandle":
        return _open_native(
            name,
            root=self,
            directory=False,
            create=create,
            writable=writable,
            allow_final_reparse=False,
            share_access=(
                FILE_SHARE_READ if stable_read else _ALL_SHARES
            ),
        )

    def open_reparse_entry(
        self, name: str, *, directory: bool, writable: bool
    ) -> "NtHandle":
        """Open the final reparse object itself below a held real directory."""
        return _open_native(
            name,
            root=self,
            directory=directory,
            create=False,
            writable=writable,
            allow_final_reparse=True,
        )

    def entry_identity(self, name: str, *, directory: bool) -> tuple[int, int]:
        with (
            self.open_dir(name, writable=False)
            if directory
            else self.open_file(name, writable=False)
        ) as child:
            return child.identity

    def exists(self, name: str, *, directory: Optional[bool] = None) -> bool:
        _validate_component(name)
        if directory is None:
            folded = name.casefold()
            return any(
                entry.name.casefold() == folded
                for entry in self.list_entries()
            )
        try:
            if directory is True:
                child = self.open_dir(name, writable=False)
            else:
                child = self.open_file(name, writable=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        else:
            child.close()
            return True

    def list_entries(
        self, *, max_entries: Optional[int] = None
    ) -> list[NtDirectoryEntry]:
        api = _api()
        buffer_size = 64 * 1024
        buffer = ctypes.create_string_buffer(buffer_size)
        entries: list[NtDirectoryEntry] = []
        info_class = 11  # FileIdBothDirectoryRestartInfo
        while True:
            if not api.GetFileInformationByHandleEx(
                ctypes.c_void_p(self.raw),
                info_class,
                buffer,
                buffer_size,
            ):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    break
                raise api.winerror(error)
            info_class = 10  # FileIdBothDirectoryInfo (continue enumeration)
            offset = 0
            while True:
                record = _FILE_ID_BOTH_DIR_INFO.from_buffer(buffer, offset)
                name_address = (
                    ctypes.addressof(buffer)
                    + offset
                    + _FILE_ID_BOTH_DIR_INFO.FileName.offset
                )
                name = ctypes.wstring_at(
                    name_address, int(record.FileNameLength) // 2
                )
                if name not in (".", ".."):
                    entries.append(
                        NtDirectoryEntry(
                            name=name,
                            file_attributes=int(record.FileAttributes),
                            size=int(record.EndOfFile),
                            file_id=(
                                int(record.FileId) & 0xFFFFFFFFFFFFFFFF
                            ),
                            st_mtime_ns=_filetime_ticks_to_unix_ns(
                                record.LastWriteTime
                            ),
                            st_ctime_ns=_filetime_ticks_to_unix_ns(
                                record.ChangeTime
                            ),
                        )
                    )
                    if (
                        max_entries is not None
                        and len(entries) > max_entries
                    ):
                        raise OSError(
                            "directory enumeration exceeds the safe "
                            "entry limit"
                        )
                if not record.NextEntryOffset:
                    break
                offset += int(record.NextEntryOffset)
        return entries

    def read_all(self, *, max_bytes: Optional[int] = None) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            buffer = ctypes.create_string_buffer(64 * 1024)
            read = ctypes.c_ulong()
            if not _api().ReadFile(
                ctypes.c_void_p(self.raw),
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                raise _api().winerror()
            if not read.value:
                break
            total += int(read.value)
            if max_bytes is not None and total > max_bytes:
                raise OSError("file exceeds the safe read limit")
            chunks.append(buffer.raw[: read.value])
        return b"".join(chunks)

    def write_all(self, payload: bytes) -> None:
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            chunk = bytes(view[offset : offset + 64 * 1024])
            buffer = ctypes.create_string_buffer(chunk)
            written = ctypes.c_ulong()
            if not _api().WriteFile(
                ctypes.c_void_p(self.raw),
                buffer,
                len(chunk),
                ctypes.byref(written),
                None,
            ):
                raise _api().winerror()
            if not written.value:
                raise OSError("short write through NT handle")
            offset += int(written.value)

    def flush(self) -> None:
        if not _api().FlushFileBuffers(ctypes.c_void_p(self.raw)):
            raise _api().winerror()

    def rename_to(
        self,
        destination_parent: "NtHandle",
        destination_name: str,
        *,
        replace: bool,
    ) -> None:
        _validate_component(destination_name)
        encoded = destination_name.encode("utf-16-le")
        total = _FILE_RENAME_INFO_BUFFER.FileName.offset + len(encoded)
        buffer = ctypes.create_string_buffer(total)
        header = _FILE_RENAME_INFO_BUFFER.from_buffer(buffer)
        header.Flags = (
            FILE_RENAME_FLAG_POSIX_SEMANTICS
            | (FILE_RENAME_FLAG_REPLACE_IF_EXISTS if replace else 0)
        )
        header.RootDirectory = destination_parent.raw
        header.FileNameLength = len(encoded)
        ctypes.memmove(
            ctypes.addressof(buffer) + _FILE_RENAME_INFO_BUFFER.FileName.offset,
            encoded,
            len(encoded),
        )
        if _api().SetFileInformationByHandle(
            ctypes.c_void_p(self.raw),
            FILE_RENAME_INFO_EX,
            buffer,
            total,
        ):
            return
        error = ctypes.get_last_error()
        if error != ERROR_INVALID_PARAMETER:
            raise _api().winerror(error)

        # Pre-FileRenameInfoEx fallback has a one-byte BOOLEAN followed by
        # architecture-dependent padding before HANDLE. Build that SDK layout
        # independently instead of reusing the DWORD-flags structure.
        legacy_total = (
            _FILE_RENAME_INFO_LEGACY_BUFFER.FileName.offset + len(encoded)
        )
        legacy_buffer = ctypes.create_string_buffer(legacy_total)
        legacy = _FILE_RENAME_INFO_LEGACY_BUFFER.from_buffer(legacy_buffer)
        legacy.ReplaceIfExists = 1 if replace else 0
        legacy.RootDirectory = destination_parent.raw
        legacy.FileNameLength = len(encoded)
        ctypes.memmove(
            ctypes.addressof(legacy_buffer)
            + _FILE_RENAME_INFO_LEGACY_BUFFER.FileName.offset,
            encoded,
            len(encoded),
        )
        if not _api().SetFileInformationByHandle(
            ctypes.c_void_p(self.raw),
            FILE_RENAME_INFO,
            legacy_buffer,
            legacy_total,
        ):
            raise _api().winerror()

    def mark_delete(self, *, is_directory: bool) -> None:
        del is_directory  # the kernel validates directory emptiness itself
        extended = _FILE_DISPOSITION_INFO_EX_BUFFER(
            Flags=(
                FILE_DISPOSITION_FLAG_DELETE
                | FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
                | FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
            )
        )
        if _api().SetFileInformationByHandle(
            ctypes.c_void_p(self.raw),
            FILE_DISPOSITION_INFO_EX,
            ctypes.byref(extended),
            ctypes.sizeof(extended),
        ):
            return
        error = ctypes.get_last_error()
        if error != ERROR_INVALID_PARAMETER:
            raise _api().winerror(error)
        legacy = _FILE_DISPOSITION_INFO_BUFFER(DeleteFile=1)
        if not _api().SetFileInformationByHandle(
            ctypes.c_void_p(self.raw),
            FILE_DISPOSITION_INFO,
            ctypes.byref(legacy),
            ctypes.sizeof(legacy),
        ):
            raise _api().winerror()


def open_directory(path: Path, *, writable: bool = False) -> NtHandle:
    """Open an absolute directory without traversing any reparse point."""
    return _open_native(
        _nt_absolute_name(path),
        root=None,
        directory=True,
        create=False,
        writable=writable,
        allow_final_reparse=False,
    )


def open_relative_directory(
    root: NtHandle,
    parts: Sequence[str],
    *,
    writable: bool = False,
    create: bool = False,
) -> NtHandle:
    """Resolve a directory component-by-component below a held root."""
    current: Optional[NtHandle] = None
    parent = root
    try:
        for part in parts:
            try:
                child = parent.open_dir(
                    part,
                    create=False,
                    writable=writable,
                )
            except FileNotFoundError:
                if not create:
                    raise
                child = parent.open_dir(
                    part,
                    create=True,
                    writable=writable,
                )
            if current is not None:
                current.close()
            current = child
            parent = child
        if current is None:
            raise ValueError("relative directory path must not be empty")
        return current
    except BaseException:
        if current is not None:
            current.close()
        raise


def read_regular_file(parent: NtHandle, name: str) -> tuple[bytes, NtStat]:
    with parent.open_file(name, writable=False) as file_handle:
        before = file_handle.stat()
        if not before.is_file:
            raise OSError(f"{name} is not a regular file")
        payload = file_handle.read_all()
        after = file_handle.stat()
        if before.signature != after.signature:
            raise RuntimeError(f"{name} changed while being read")
        return payload, after


def replace_regular_file(
    parent: NtHandle,
    name: str,
    payload: bytes,
    *,
    require_existing: bool,
    temp_name: str,
) -> None:
    """Durably write a temporary file and atomically rename it in one handle."""
    if require_existing:
        with parent.open_file(name, writable=False) as target:
            if not target.stat().is_file:
                raise OSError(f"{name} is not a regular file")
    with parent.open_file(temp_name, create=True, writable=True) as temporary:
        try:
            temporary.write_all(payload)
            temporary.flush()
            temporary.rename_to(parent, name, replace=True)
        except BaseException:
            try:
                temporary.mark_delete(is_directory=False)
            except OSError:
                pass
            raise
    try:
        parent.flush()
    except OSError:
        # Directory flush support varies by Windows/filesystem.  The namespace
        # operation has already committed; callers preserve that truthful state.
        pass


def delete_tree(directory: NtHandle) -> None:
    """Delete descendants without ever following a directory entry."""
    for entry in directory.list_entries():
        if entry.is_reparse:
            # Open the reparse object itself through OBJ_DONT_REPARSE and delete
            # the entry.  Never interpret its target.
            is_directory = entry.is_dir
            child = directory.open_reparse_entry(
                entry.name,
                directory=is_directory,
                writable=True,
            )
            with child:
                if not _entry_matches_metadata(
                    entry,
                    child.stat(),
                    expected_reparse=True,
                ):
                    raise RuntimeError(
                        f"{entry.name} changed before recursive delete"
                    )
                child.mark_delete(is_directory=is_directory)
            continue
        if entry.is_dir:
            with directory.open_dir(entry.name, writable=True) as child_dir:
                if not _entry_matches_metadata(entry, child_dir.stat()):
                    raise RuntimeError(
                        f"{entry.name} changed before recursive delete"
                    )
                delete_tree(child_dir)
                child_dir.mark_delete(is_directory=True)
        else:
            with directory.open_file(entry.name, writable=True) as child_file:
                if not _entry_matches_metadata(entry, child_file.stat()):
                    raise RuntimeError(
                        f"{entry.name} changed before recursive delete"
                    )
                child_file.mark_delete(is_directory=False)


_SNAPSHOT_MAX_DEPTH = 32
_SNAPSHOT_MAX_ENTRIES = 4096
_SNAPSHOT_MAX_FILE_BYTES = 16 * 1024 * 1024
_SNAPSHOT_MAX_TOTAL_BYTES = 64 * 1024 * 1024


def _entry_matches_metadata(
    entry: NtDirectoryEntry,
    metadata: NtStat,
    *,
    expected_reparse: bool = False,
) -> bool:
    return (
        metadata.st_ino == entry.file_id
        and metadata.st_size == entry.size
        and metadata.st_mtime_ns == entry.st_mtime_ns
        and metadata.st_ctime_ns == entry.st_ctime_ns
        and metadata.is_dir == entry.is_dir
        and bool(metadata.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)
        == expected_reparse
        and entry.is_reparse == expected_reparse
    )


def copy_tree_no_reparse(
    source: NtHandle,
    destination: Path,
    *,
    _state: Optional[dict[str, int]] = None,
    _depth: int = 0,
) -> None:
    """Copy a held tree only after identity-binding every enumerated child."""
    if _depth > _SNAPSHOT_MAX_DEPTH:
        raise OSError("skill snapshot exceeds the safe nesting limit")
    state = _state or {"entries": 0, "bytes": 0}
    source_before = source.stat().signature
    remaining_entries = _SNAPSHOT_MAX_ENTRIES - state["entries"]
    entries = source.list_entries(max_entries=remaining_entries)
    destination.mkdir(parents=True, exist_ok=False)
    seen_names: set[str] = set()
    for entry in entries:
        _validate_snapshot_component(entry.name)
        collision_key = entry.name.casefold()
        if collision_key in seen_names:
            raise OSError(
                f"Win32 snapshot name collision: {entry.name!r}"
            )
        seen_names.add(collision_key)
        state["entries"] += 1
        if state["entries"] > _SNAPSHOT_MAX_ENTRIES:
            raise OSError("skill snapshot exceeds the safe entry limit")
        if entry.is_reparse:
            raise OSError(
                f"refusing reparse point in skill snapshot: {entry.name}"
            )

        target = destination / entry.name
        if entry.is_dir:
            with source.open_dir(entry.name, writable=False) as child:
                child_before = child.stat()
                if not _entry_matches_metadata(entry, child_before):
                    raise RuntimeError(
                        f"{entry.name} changed before snapshot copy"
                    )
                copy_tree_no_reparse(
                    child,
                    target,
                    _state=state,
                    _depth=_depth + 1,
                )
                if child.stat().signature != child_before.signature:
                    raise RuntimeError(
                        f"{entry.name} changed during snapshot copy"
                    )
            continue

        if entry.size > _SNAPSHOT_MAX_FILE_BYTES:
            raise OSError(
                f"skill snapshot file is too large: {entry.name}"
            )
        with source.open_file(
            entry.name,
            writable=False,
            stable_read=True,
        ) as child:
            child_before = child.stat()
            if (
                not child_before.is_file
                or not _entry_matches_metadata(entry, child_before)
            ):
                raise RuntimeError(
                    f"{entry.name} changed before snapshot copy"
                )
            remaining_bytes = _SNAPSHOT_MAX_TOTAL_BYTES - state["bytes"]
            payload = child.read_all(
                max_bytes=min(
                    _SNAPSHOT_MAX_FILE_BYTES,
                    remaining_bytes,
                )
            )
            child_after = child.stat()
            if child_after.signature != child_before.signature:
                raise RuntimeError(
                    f"{entry.name} changed during snapshot copy"
                )
        state["bytes"] += len(payload)
        if (
            len(payload) > _SNAPSHOT_MAX_FILE_BYTES
            or state["bytes"] > _SNAPSHOT_MAX_TOTAL_BYTES
        ):
            raise OSError("skill snapshot exceeds the safe byte limit")
        target.write_bytes(payload)
    if source.stat().signature != source_before:
        raise RuntimeError("skill directory changed during snapshot copy")


def walk_directories(
    root: NtHandle,
    *,
    max_directories: Optional[int] = None,
    max_depth: Optional[int] = None,
) -> Iterator[tuple[tuple[str, ...], NtHandle]]:
    """Yield owned handles for real directories below *root*.

    Callers must close every yielded handle.  Reparse entries are skipped.
    """
    seen = 0

    def recurse(parent: NtHandle, relative: tuple[str, ...], depth: int):
        nonlocal seen
        for entry in sorted(parent.list_entries(), key=lambda item: item.name.casefold()):
            if not entry.is_dir or entry.is_reparse:
                continue
            if max_depth is not None and depth >= max_depth:
                raise OSError("directory walk exceeds the safe nesting limit")
            seen += 1
            if max_directories is not None and seen > max_directories:
                raise OSError("directory walk exceeds the safe directory limit")
            child = parent.open_dir(entry.name, writable=False)
            child_relative = relative + (entry.name,)
            yield child_relative, child
            yield from recurse(child, child_relative, depth + 1)

    yield from recurse(root, (), 0)
