#!/usr/bin/env python3
"""Send one claimed Gmail reply with durable write-ahead and receipt evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

import inbox_claim


SEND_URL = "https://gateway.maton.ai/google-mail/gmail/v1/users/me/messages/send"


def read_payload(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"threadId", "raw"}:
        raise ValueError("Gmail reply payload must contain only threadId and raw")
    for field in ("threadId", "raw"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"Gmail reply payload {field} must be non-empty text")
    return value


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_command(payload_path: Path, token: str) -> list[str]:
    if not token or any(character in token for character in "\r\n"):
        raise ValueError("MATON_API_KEY is missing or invalid")
    return [
        "curl",
        "-sS",
        "--fail-with-body",
        "-X",
        "POST",
        SEND_URL,
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Content-Type: application/json",
        "--data-binary",
        f"@{payload_path}",
    ]


def write_private_json(path: Path, value: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def receipt_from_action(action: dict[str, Any]) -> dict[str, str]:
    message_id = action.get("provider_message_id")
    thread_id = action.get("provider_thread_id")
    if not isinstance(message_id, str) or not isinstance(thread_id, str):
        raise ValueError("sent customer delivery lacks durable provider receipt")
    return {"id": message_id, "threadId": thread_id}


def run_command(
    argv: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(list(argv), check=True, capture_output=True, text=True, shell=False)


def send_reply_claimed(
    claim_root: Path,
    message_id: str,
    claim_token: str,
    delivery_key: str,
    payload_path: Path,
    provider_response_path: Path,
    token: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    payload = read_payload(payload_path)
    binding = canonical_sha256(payload)
    command = build_command(payload_path, token)
    acquired, state = inbox_claim.acquire_external_action(
        claim_root,
        message_id,
        claim_token,
        delivery_key,
        "customer_delivery",
        binding,
    )
    if not acquired:
        action = state["external_actions"][delivery_key]
        if action["status"] == "sent":
            receipt = receipt_from_action(action)
            write_private_json(provider_response_path, receipt)
            return receipt
        raise ValueError(f"customer delivery is already {action['status']}; refusing retry")
    try:
        result = run_command(command, runner=runner)
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError("Gmail provider response must be a JSON object")
        provider_message_id = response.get("id")
        provider_thread_id = response.get("threadId")
        if not isinstance(provider_message_id, str) or not provider_message_id:
            raise ValueError("Gmail provider response lacks id")
        if provider_thread_id != payload["threadId"]:
            raise ValueError("Gmail provider response threadId does not match reply payload")
    except Exception:
        # Once curl is invoked, any failure is delivery-ambiguous and must not
        # be retried automatically.
        inbox_claim.finish_external_action(
            claim_root, message_id, claim_token, delivery_key, "uncertain"
        )
        raise
    inbox_claim.finish_external_action(
        claim_root,
        message_id,
        claim_token,
        delivery_key,
        "sent",
        provider_message_id,
        provider_thread_id,
    )
    receipt = {"id": provider_message_id, "threadId": provider_thread_id}
    write_private_json(provider_response_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send-reply-claimed")
    send.add_argument("--claim-root", type=Path, required=True)
    send.add_argument("--message-id", required=True)
    send.add_argument("--claim-token", required=True)
    send.add_argument("--delivery-key", required=True)
    send.add_argument("--payload", type=Path, required=True)
    send.add_argument("--provider-response", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = send_reply_claimed(
            args.claim_root,
            args.message_id,
            args.claim_token,
            args.delivery_key,
            args.payload,
            args.provider_response,
            os.environ.get("MATON_API_KEY", ""),
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
