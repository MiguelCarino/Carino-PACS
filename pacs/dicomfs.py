"""Small filesystem helpers shared by the watcher and the history/browse code."""

from __future__ import annotations

import os


def is_dicom(path: str) -> bool:
    """True if the file has the DICM magic at offset 128 (extension-agnostic)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(128)
            return fh.read(4) == b"DICM"
    except OSError:
        return False


def safe_within(root: str, path: str) -> bool:
    """True if *path* is *root* itself or lives inside it (realpath-based, so it
    also defeats ``..`` traversal and symlink escapes). Used to gate destructive
    actions to the configured storage folders only."""
    if not root or not path:
        return False
    try:
        root = os.path.realpath(root)
        path = os.path.realpath(path)
    except OSError:
        return False
    return path == root or path.startswith(root + os.sep)


def prune_empty_dirs(start_dir: str, stop_at: str) -> None:
    """Remove *start_dir* and its now-empty parents, walking up until (but not
    including) *stop_at*. Never removes a non-empty directory. This is what keeps
    the outgoing tree from filling up with empty Patient/Study/Series folders
    after their files are archived or deleted."""
    if not start_dir or not stop_at:
        return
    d = os.path.abspath(start_dir)
    stop = os.path.abspath(stop_at)
    while d != stop and d.startswith(stop + os.sep):
        try:
            os.rmdir(d)            # only succeeds if the directory is empty
        except OSError:
            break                  # not empty (or already gone) → stop climbing
        d = os.path.dirname(d)
