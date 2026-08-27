#!/usr/bin/env python3
"""Copy one Kolo-generated PNG into a claimed rendering work path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

import inbox_claim
import inbox_monitor


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def default_media_root() -> Path:
    configured = os.environ.get("OPENCLAW_MEDIA_ROOT")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".openclaw" / "media").resolve()
    )


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def materialize(
    monitor_root: Path,
    claim_root: Path,
    message_id: str,
    source: Path,
    slot: int,
    media_root: Path | None = None,
) -> dict[str, str | int]:
    if slot not in {1, 2}:
        raise ValueError("rendering slot must be 1 or 2")
    media_root_input = media_root or default_media_root()
    if media_root_input.is_symlink():
        raise ValueError("Kolo media root is unavailable")
    media_root = media_root_input.resolve()
    if not media_root.is_dir():
        raise ValueError("Kolo media root is unavailable")
    if source.is_symlink():
        raise ValueError("rendering source must not be a symlink")
    resolved_source = source.resolve(strict=True)
    if not is_within(resolved_source, media_root) or not resolved_source.is_file():
        raise ValueError("rendering source must be a regular Kolo media file")
    data = resolved_source.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("Kolo rendering source must be a PNG image")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Kolo rendering source exceeds 20 MiB")

    claim = inbox_claim.read_state(inbox_claim.claim_path(claim_root, message_id))
    if claim.get("status") != "processing":
        raise ValueError("rendering materialization requires a processing claim")
    work_paths = inbox_monitor.prepare_claim_work(monitor_root, claim_root, message_id)
    destination = Path(work_paths[f"rendering_image_{slot}"])
    digest = hashlib.sha256(data).hexdigest()
    result = {"path": str(destination), "sha256": f"sha256:{digest}", "slot": slot}
    if destination.exists():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("rendering destination is not a regular file")
        if hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            return result
        raise ValueError("rendering destination already contains different data")

    temporary = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_bytes(data)
        os.chmod(temporary, 0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor-root", type=Path, required=True)
    parser.add_argument("--claim-root", type=Path, required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--slot", type=int, choices=(1, 2), required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(materialize(
            args.monitor_root, args.claim_root, args.message_id, args.source, args.slot
        ), sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
