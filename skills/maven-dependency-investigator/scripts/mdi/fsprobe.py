"""Filesystem-type probe shared by every writer of spool or scratch bytes.

Archive spools, scratch trees and Maven control homes can grow to the
configured gigabyte caps. On a memory-backed filesystem (tmpfs, ramfs) every
one of those bytes is RAM, so the CLI and the Maven executor refuse such
locations before any byte is written. Linux and glibc only; the struct
layout is the glibc ``struct statfs`` with ``f_type`` first.
"""

import ctypes
import os

MEMORY_FS_MAGICS = frozenset({0x01021994, 0x858458F6})  # tmpfs, ramfs


class _StatFsBuf(ctypes.Structure):
    # f_type leads struct statfs on glibc; the pad covers the rest of the
    # struct so the kernel write never overruns the buffer.
    _fields_ = [("f_type", ctypes.c_long), ("_rest", ctypes.c_byte * 248)]


_LIBC = ctypes.CDLL(None, use_errno=True)


def fs_magic(path):
    """Return the statfs magic of the filesystem holding an existing path."""
    buffer = _StatFsBuf()
    if _LIBC.statfs(os.fsencode(path), ctypes.byref(buffer)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno), path)
    return buffer.f_type


def is_memory_backed(path):
    return fs_magic(path) in MEMORY_FS_MAGICS


def memory_backed_message(what, path):
    return (f"{what} {path!r} is on a memory-backed filesystem (tmpfs or ramfs); "
            f"archive spools, scratch and run files written there consume RAM "
            f"directly. Use a disk-backed location such as the default "
            f"~/.local/state/maven-dependency-investigator/runs or a path under "
            f"/var/tmp.")
