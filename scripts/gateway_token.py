#!/usr/bin/env python3
"""Resolve the Maton gateway token and say where it came from.

Precedence, explicit since 14 September 2026: the platform's `MATON_API_KEY`
in the process environment, then a file named by `MATON_API_KEY_FILE`, then
the desk's private fallback file at `~/.openclaw/secrets/maton-api-key`.

The fallback file used to win. That made a copy installed once at activation
authoritative forever, while the platform rotated the key underneath it (the
tester's pod on 14 September held a file from 11 September and a newer key in
the environment; rotated keys overlap for a while, so both still worked that
day). The environment is what the platform keeps current, so it is read
first. The file is kept only for installations with no environment key, and
`refresh` remains an explicit operator command for those; nothing here, and
nothing in the watcher, replaces a credential on its own.

Every file read must be a regular file owned by the caller and readable by
nobody else. No function prints a token.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


DEFAULT_TOKEN_FILE = Path.home() / ".openclaw" / "secrets" / "maton-api-key"

SOURCE_ENVIRONMENT = "environment"
SOURCE_CONFIGURED_FILE = "configured_file"
SOURCE_FALLBACK_FILE = "fallback_file"


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


def load_token_with_source(environ: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The token and its source: environment, configured_file, or fallback_file."""
    env = os.environ if environ is None else environ
    token = env.get("MATON_API_KEY", "")
    if _valid(token):
        return token, SOURCE_ENVIRONMENT
    configured = env.get("MATON_API_KEY_FILE")
    if configured:
        return read_token_file(Path(configured).expanduser()), SOURCE_CONFIGURED_FILE
    if DEFAULT_TOKEN_FILE.exists():
        return read_token_file(DEFAULT_TOKEN_FILE), SOURCE_FALLBACK_FILE
    if token:
        raise ValueError("MATON_API_KEY is present but not a usable token")
    raise ValueError(
        "MATON_API_KEY is missing: the platform environment carries no gateway token, "
        "MATON_API_KEY_FILE is unset, and no fallback file is installed"
    )


def load_token(environ: Mapping[str, str] | None = None) -> str:
    """The token alone, for callers that only send it."""
    return load_token_with_source(environ)[0]


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


def candidate_token(environ: Mapping[str, str] | None = None) -> str:
    """The platform's current token, deliberately ignoring the fallback file."""
    env = os.environ if environ is None else environ
    token = env.get("MATON_API_KEY", "")
    if _valid(token):
        return token
    configured = env.get("MATON_API_KEY_FILE")
    if configured:
        return read_token_file(Path(configured).expanduser())
    raise ValueError("no current platform gateway token is available for a verified refresh")


def refresh_token_file(
    workspace: Path,
    environ: Mapping[str, str] | None = None,
    verifier: Callable[[str, str], Any] | None = None,
) -> str:
    """Operator command: replace the fallback file only after the candidate proves the configured mailbox.

    Relevant only to an installation with no environment key; with one present the
    fallback file is never read. Not the normal 401 repair (that is `kolo gateway
    restart`, then readiness in cron context).
    """
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


def install_token_file(environ: Mapping[str, str] | None = None) -> Path:
    """Create the fallback file once from the current platform token, without exposing its value."""
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
                "token before installing the fallback copy"
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
        if args == ["source"]:
            print(load_token_with_source()[1])
            return 0
        if len(args) == 3 and args[:2] == ["refresh", "--workspace"]:
            refresh_token_file(Path(args[2]).resolve())
            print(f"gateway token refreshed and verified at {DEFAULT_TOKEN_FILE}")
            return 0
        print(
            "usage: gateway_token.py install | source | refresh --workspace <absolute-workspace>",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
