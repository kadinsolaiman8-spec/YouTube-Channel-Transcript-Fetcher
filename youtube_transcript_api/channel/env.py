"""Load optional .env files for channel export (web UI / CLI)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _parse_env_line(line: str) -> Optional[tuple[str, str]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].strip()
    if "=" not in stripped:
        return None
    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    if not key:
        return None
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return key, value


def _load_env_file(path: Path, protected_keys: set[str]) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if key in protected_keys:
            continue
        os.environ[key] = value


def load_local_env(base_dir: Optional[Path] = None) -> None:
    """Load `.env` then `.env.local` from base_dir (default: cwd).

    Shell environment variables always win. `.env.local` overrides `.env`.
    """
    root = base_dir if base_dir is not None else Path.cwd()
    protected = set(os.environ)
    _load_env_file(root / ".env", protected)
    _load_env_file(root / ".env.local", protected)
