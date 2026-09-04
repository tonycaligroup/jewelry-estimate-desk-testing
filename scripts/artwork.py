#!/usr/bin/env python3
"""The customer's artwork: image attachments from their thread, saved privately.

A logo, a sketch, or a photo of a piece they like changes what a rendering
should show. Gmail lists attachments as parts with an attachmentId; the
bytes come from a second call. Only image parts under a size cap are kept,
newest message first, and the files live beside the claim's other work.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import gmail_fetch

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp"}
MAX_BYTES = 8_000_000
MAX_FILES = 3


def _parts(payload: Any):
    if not isinstance(payload, dict):
        return
    yield payload
    for child in payload.get("parts") or []:
        yield from _parts(child)


def image_parts(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """(message id, attachment id, mime, filename) for every image, newest message first."""
    found: list[dict[str, Any]] = []
    messages = list(thread.get("messages") or [])
    messages.sort(key=lambda m: int(m.get("internalDate") or 0), reverse=True)
    for message in messages:
        for part in _parts(message.get("payload")):
            mime = str(part.get("mimeType") or "").lower()
            body = part.get("body") if isinstance(part.get("body"), dict) else {}
            if mime in IMAGE_TYPES and body.get("attachmentId"):
                size = int(body.get("size") or 0)
                if 0 < size <= MAX_BYTES:
                    found.append({"message_id": str(message.get("id")), "attachment_id": str(body["attachmentId"]),
                                  "mime": mime, "filename": str(part.get("filename") or "")})
    return found


def collect(thread: dict[str, Any], out_dir: Path, token: str, opener: Callable[..., Any] | None = None,
            limit: int = MAX_FILES) -> list[Path]:
    """Fetch the newest image attachments into out_dir; returns their paths, newest first."""
    parts = image_parts(thread)[:limit]
    if not parts:
        return []
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(out_dir, 0o700)
    saved: list[Path] = []
    kwargs = {"opener": opener} if opener else {}
    for index, part in enumerate(parts, start=1):
        data = gmail_fetch.fetch_json(
            f"messages/{quote(part['message_id'], safe='')}/attachments/{quote(part['attachment_id'], safe='')}",
            None, token, **kwargs,
        )
        raw = data.get("data")
        if not isinstance(raw, str) or not raw:
            continue
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if not payload or len(payload) > MAX_BYTES:
            continue
        path = out_dir / f"artwork-{index}{IMAGE_TYPES[part['mime']]}"
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        saved.append(path)
    return saved
