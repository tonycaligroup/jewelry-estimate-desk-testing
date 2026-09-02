#!/usr/bin/env python3
"""Resolve the Maton gateway token without leaving it in the agent's environment.

The token used to be read from `MATON_API_KEY` in the process environment and
passed to curl in argv, so any process listing or any improvised shell command
could lift it. On 2 Sep 2026 the main session did exactly that. The bundled
scripts now prefer a private file: `MATON_API_KEY_FILE` names it, or the
default `~/.openclaw/secrets/maton-api-key` is used when present. The file
must be owned by the caller and readable by nobody else. The environment
variable remains a fallback so an existing installation keeps working until
the operator moves the key.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Mapping


DEFAULT_TOKEN_FILE = Path.home() / ".openclaw" / "secrets" / "maton-api-key"


def _valid(token: str) -> bool:
    return bool(token) and not any(character in token for character in "\r\n\t ")


def read_token_file(path: Path) -> str:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"gateway token file {path} does not exist") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"gateway token file {path} must be a regular file")
    if mode & 0o077:
        raise ValueError(
            f"gateway token file {path} must not be readable by group or others"
        )
    token = path.read_text(encoding="utf-8").strip()
    if not _valid(token):
        raise ValueError(f"gateway token file {path} does not contain a usable token")
    return token


def load_token(environ: Mapping[str, str] | None = None) -> str:
    """Return the gateway token from the file, else the legacy variable."""
    env = os.environ if environ is None else environ
    configured = env.get("MATON_API_KEY_FILE")
    if configured:
        return read_token_file(Path(configured).expanduser())
    if DEFAULT_TOKEN_FILE.exists():
        return read_token_file(DEFAULT_TOKEN_FILE)
    token = env.get("MATON_API_KEY", "")
    if not _valid(token):
        raise ValueError(
            "MATON_API_KEY is missing or invalid: set MATON_API_KEY_FILE to a "
            "0600 file holding the gateway token"
        )
    return token
