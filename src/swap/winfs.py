"""Filesystem utilities: NTFS sparse files and durability (fsync) helpers.

On Windows this talks to kernel32 via ctypes. On other platforms the same
API works through POSIX behavior (truncate-created holes are sparse by
default), so the rest of the codebase never branches on the OS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FSCTL_SET_SPARSE = 0x000900C4
FILE_SUPPORTS_SPARSE_FILES = 0x00000040

_IS_WINDOWS = sys.platform == "win32"


def volume_supports_sparse(path: Path) -> bool:
    """True if the volume holding `path` can create sparse files."""
    if not _IS_WINDOWS:
        return True  # POSIX filesystems create holes on truncate-past-EOF
    import ctypes

    root = path.resolve(strict=False).anchor
    if not root:
        return False
    flags = ctypes.c_uint32(0)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root),
        None, 0,        # volume name buffer
        None,           # serial number
        None,           # max component length
        ctypes.byref(flags),
        None, 0,        # filesystem name buffer
    )
    return bool(ok) and bool(flags.value & FILE_SUPPORTS_SPARSE_FILES)


def create_sparse_file(path: Path, size: int) -> None:
    """Create `path` with logical size `size` but ~0 bytes allocated on disk.

    Overwrites any existing file. The file is fsynced before returning, so
    once this returns the (empty) sparse file is durable.

    Note: on Windows the extension MUST go through Win32 SetEndOfFile.
    Python's truncate() uses the CRT (_chsize_s), which zero-FILLS when
    extending and would allocate the full size despite the sparse flag.
    """
    with open(path, "wb") as handle:
        if _IS_WINDOWS:
            _mark_sparse(handle.fileno())
            _set_end_of_file(handle.fileno(), size)
        else:
            handle.truncate(size)  # POSIX: extension past EOF is a hole
        handle.flush()
        os.fsync(handle.fileno())


def truncate_file(path: Path, new_size: int) -> None:
    """Shrink `path` to `new_size` bytes and fsync (calls SetEndOfFile on Windows)."""
    with open(path, "r+b") as handle:
        handle.truncate(new_size)
        handle.flush()
        os.fsync(handle.fileno())


def fsync_file(path: Path) -> None:
    """Force `path`'s data to disk."""
    with open(path, "rb+") as handle:
        os.fsync(handle.fileno())


def allocated_size(path: Path) -> int:
    """Bytes actually allocated on disk (may be far less than logical size)."""
    if _IS_WINDOWS:
        import ctypes

        high = ctypes.c_uint32(0)
        low = ctypes.windll.kernel32.GetCompressedFileSizeW(
            ctypes.c_wchar_p(str(path)), ctypes.byref(high)
        )
        if low == 0xFFFFFFFF and ctypes.get_last_error() != 0:
            raise OSError(f"GetCompressedFileSizeW failed for {path}")
        return (high.value << 32) + low
    return path.stat().st_blocks * 512


def _set_end_of_file(fd: int, size: int) -> None:
    """Extend the file to `size` without allocating (Win32 SetEndOfFile)."""
    import ctypes
    import msvcrt

    handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
    kernel32 = ctypes.windll.kernel32
    FILE_BEGIN = 0
    if not kernel32.SetFilePointerEx(handle, ctypes.c_longlong(size), None, FILE_BEGIN):
        raise OSError(f"SetFilePointerEx failed (error {ctypes.GetLastError()})")
    if not kernel32.SetEndOfFile(handle):
        raise OSError(f"SetEndOfFile failed (error {ctypes.GetLastError()})")


def _mark_sparse(fd: int) -> None:
    import ctypes
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    returned = ctypes.c_uint32(0)
    ok = ctypes.windll.kernel32.DeviceIoControl(
        ctypes.c_void_p(handle),
        FSCTL_SET_SPARSE,
        None, 0,
        None, 0,
        ctypes.byref(returned),
        None,
    )
    if not ok:
        raise OSError(f"FSCTL_SET_SPARSE failed (error {ctypes.GetLastError()})")
