"""Read and seed aswap's state documents from tests, by their old file paths.

The roster, credential backups, configs, settings and stores live in
``state.db`` (``claude_swap.statedb``), not in the files their paths name.
These helpers go through the same path layer the code does, so a test can
keep saying ``switcher.sequence_file`` and get the document; a path that is
not a state document (``~/.claude.json``, a session profile) is still the
plain file.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
from pathlib import Path

import pytest

from claude_swap import statedb


def read_text(path: Path) -> str:
    return statedb.read_text(path)


def read_json(path: Path):
    return json.loads(statedb.read_text(path))


def write_text(path: Path, text: str) -> None:
    if not statedb.try_write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def write_bytes(path: Path, body: bytes) -> None:
    if not statedb.try_write(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def write_json(path: Path, data, **dumps_kwargs) -> None:
    write_text(path, json.dumps(data, **dumps_kwargs))


def exists(path: Path) -> bool:
    return statedb.exists(path)


def unlink(path: Path, missing_ok: bool = False) -> None:
    statedb.unlink(path, missing_ok=missing_ok)



@contextlib.contextmanager
def unreadable(path: Path, *, unsearchable: bool = False):
    """Make state documents present but unreadable, as ``chmod 000`` did.

    ``path`` is a document or a directory; every document at or under it is
    affected. By default the documents still exist (``updated_at`` answers,
    so ``mtime`` and ``exists`` see them) and only reading a body raises
    PermissionError, like a mode-000 file. With ``unsearchable`` they cannot
    even be found, like the documents of a mode-000 directory.
    """
    target = os.path.normcase(os.path.abspath(path))

    def hit(db, rel):
        doc = os.path.normcase(os.path.abspath(os.path.join(db.root, rel)))
        return doc == target or doc.startswith(target + os.sep)

    def deny(real):
        def method(self, rel, *args, **kwargs):
            if hit(self, rel):
                raise PermissionError(errno.EACCES, "Permission denied (injected)", rel)
            return real(self, rel, *args, **kwargs)
        return method

    names = ("read", "updated_at", "exists") if unsearchable else ("read",)
    with pytest.MonkeyPatch.context() as mp:
        for name in names:
            mp.setattr(statedb.StateDB, name, deny(getattr(statedb.StateDB, name)))
        yield
