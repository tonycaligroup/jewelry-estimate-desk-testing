#!/usr/bin/env python3
"""Resolve the Maton gateway token without leaving it in the agent's environment.

The token used to be read from `MATON_API_KEY` in the process environment and
passed to curl in argv, so any process listing or any improvised shell command
could lift it. On 2 Sep 2026 the main session did exactly that. The bundled
scripts now prefer the desk-owned private file at
`~/.openclaw/secrets/maton-api-key` whenever it exists. `MATON_API_KEY_FILE`
can name a migration source until that file is installed. The file must be
owned by the caller and readable by nobody else. The environment variable
remains a final fallback so an existing installation keeps working until the
operator installs the desk-owned copy.
"""

from __future__ import annotations

import os
import json
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


DEFAULT_TOKEN_FILE = Path.home() / ".openclaw" / "secrets" / "maton-api-key"


class GatewayAuthenticationError(ValueError):
    """The active private credential failed and no verified replacement was available."""


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
    """Return the desk-owned token, else a configured file or legacy variable."""
    env = os.environ if environ is None else environ
    if DEFAULT_TOKEN_FILE.exists():
        return read_token_file(DEFAULT_TOKEN_FILE)
    configured = env.get("MATON_API_KEY_FILE")
    if configured:
        return read_token_file(Path(configured).expanduser())
    token = env.get("MATON_API_KEY", "")
    if not _valid(token):
        raise ValueError(
            "MATON_API_KEY is missing or invalid: set MATON_API_KEY_FILE to a "
            "0600 file holding the gateway token"
        )
    return token


def candidate_token(environ: Mapping[str, str] | None = None) -> str:
    """Read the platform's current candidate, deliberately ignoring the installed copy."""
    env = os.environ if environ is None else environ
    configured = env.get("MATON_API_KEY_FILE")
    if configured:
        return read_token_file(Path(configured).expanduser())
    token = env.get("MATON_API_KEY", "")
    if not _valid(token):
        raise ValueError("no current platform gateway token is available for verified refresh")
    return token


def _replace_token_file(token: str) -> Path:
    if not _valid(token):
        raise ValueError("replacement gateway token is invalid")
    DEFAULT_TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = DEFAULT_TOKEN_FILE.with_name(
        f".{DEFAULT_TOKEN_FILE.name}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, DEFAULT_TOKEN_FILE)
        DEFAULT_TOKEN_FILE.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return DEFAULT_TOKEN_FILE


def refresh_token_file(
    workspace: Path,
    environ: Mapping[str, str] | None = None,
    verifier: Callable[[str, str], Any] | None = None,
) -> str:
    """Replace the private token only after it proves access to the configured mailbox."""
    profile_path = Path(workspace) / "estimate-desk" / "shop-profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot verify a gateway refresh without the shop profile") from exc
    shop = profile.get("shop") if isinstance(profile, dict) else None
    mailbox = str((shop or {}).get("outbound_mailbox") or "").strip().lower()
    if not mailbox or "@" not in mailbox:
        raise ValueError("shop profile is missing a valid outbound mailbox")
    replacement = candidate_token(environ)
    if verifier is None:
        import gmail_identity

        verifier = lambda token, address: gmail_identity.retrieve(address, token)
    verifier(replacement, mailbox)
    _replace_token_file(replacement)
    return replacement


def is_auth_failure(error: BaseException) -> bool:
    text = str(error).lower()
    return "http 401" in text or "invalid authentication credentials" in text


def install_token_file(environ: Mapping[str, str] | None = None) -> Path:
    """Create the desk-owned token file once, without exposing its value."""
    env = os.environ if environ is None else environ
    if DEFAULT_TOKEN_FILE.exists():
        read_token_file(DEFAULT_TOKEN_FILE)
        return DEFAULT_TOKEN_FILE

    configured = env.get("MATON_API_KEY_FILE")
    if configured:
        token = read_token_file(Path(configured).expanduser())
    else:
        token = env.get("MATON_API_KEY", "")
        if not _valid(token):
            raise ValueError(
                "MATON_API_KEY is missing or invalid: provide the current gateway "
                "token before installing the desk-owned copy"
            )

    DEFAULT_TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            DEFAULT_TOKEN_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        read_token_file(DEFAULT_TOKEN_FILE)
        return DEFAULT_TOKEN_FILE
    try:
        os.write(descriptor, (token + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
    return DEFAULT_TOKEN_FILE


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args == ["install"]:
            path = install_token_file()
            print(f"gateway token ready at {path}")
            return 0
        if len(args) == 3 and args[:2] == ["refresh", "--workspace"]:
            refresh_token_file(Path(args[2]).resolve())
            print(f"gateway token refreshed and verified at {DEFAULT_TOKEN_FILE}")
            return 0
        print("usage: gateway_token.py install | refresh --workspace <absolute-workspace>", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
