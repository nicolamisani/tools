"""Encryption at rest for the stored Google OAuth refresh token.

The key lives next to the database in DATA_DIR and is generated on first use
with 0600 permissions. It is not a defence against someone who already has
read access to DATA_DIR -- it keeps the token out of database backups and
casual `sqlite3 sync.db` dumps.
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

_cached: Fernet | None = None


def _load_key(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    os.chmod(path, 0o600)
    return key


def _fernet(path: Path) -> Fernet:
    global _cached
    if _cached is None:
        _cached = Fernet(_load_key(path))
    return _cached


def encrypt(plaintext: str, key_path: Path) -> str:
    return _fernet(key_path).encrypt(plaintext.encode()).decode()


def decrypt(token: str, key_path: Path) -> str:
    return _fernet(key_path).decrypt(token.encode()).decode()
