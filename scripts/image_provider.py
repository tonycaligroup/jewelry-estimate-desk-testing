#!/usr/bin/env python3
"""The image provider, called directly: generation, edits, and the vision check over HTTP.

Kolo's probe (8 September 2026): the platform CLI runs one command at a
time (an SQLite lock), does not honour its own timeout (one generation ran
351 s), and the same generation asked of the LiteLLM proxy directly took
11 s. The proxy's address and key are in the environment of every process
the agent spawns, the watcher's ticks included, so the desk reads them at
run time and writes nothing down. When they are absent (the lab, the
tests) the CLI path in `rendering` stays in use.

Nothing here logs, prints, or raises the key.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL_VAR = "LITELLM_BASE_URL"
API_KEY_VAR = "LITELLM_API_KEY"
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_VISION_MODEL = "kolo-best-available"  # the CLI's alias
DIRECT_VISION_MODEL = "qwen-3-7-plus"  # what the proxy knows; vision-capable (platform facts, 4 Sep 2026)
CLI_ALIASES = ("kolo-best-available",)


def vision_model_name(model: str | None) -> str:
    """The proxy's id for the vision model: the CLI's alias becomes a real model."""
    name = model_name(model, DEFAULT_VISION_MODEL)
    return DIRECT_VISION_MODEL if name in CLI_ALIASES else name
GENERATE_TIMEOUT_SECONDS = 180
DESCRIBE_TIMEOUT_SECONDS = 90
MODES = ("auto", "direct", "cli")

Opener = Callable[..., Any]


def credentials(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """The proxy's base URL and key from the environment, or None when either is missing."""
    source = os.environ if env is None else env
    base = str(source.get(BASE_URL_VAR) or "").strip().rstrip("/")
    key = str(source.get(API_KEY_VAR) or "").strip()
    return (base, key) if base and key else None


def available(mode: str = "auto", env: dict[str, str] | None = None) -> bool:
    """Whether image calls go to the provider directly: the profile's choice, then the environment."""
    if mode not in MODES:
        raise ValueError(f"rendering.provider must be one of {', '.join(MODES)}")
    if mode == "cli":
        return False
    found = credentials(env) is not None
    if mode == "direct" and not found:
        raise ValueError(f"rendering.provider is direct but {BASE_URL_VAR} or {API_KEY_VAR} is not set")
    return found


def model_name(model: str | None, default: str) -> str:
    """'litellm/gpt-image-2' or 'litellm-fireworks/qwen-3-7-plus' (the CLI's forms) is the part after the last '/' at the proxy."""
    value = str(model or "").strip() or default
    return value.rsplit("/", 1)[1] if "/" in value else value


def _post(url: str, key: str, body: bytes, content_type: str, timeout: float, what: str, opener: Opener = urlopen) -> dict[str, Any]:
    request = Request(url, data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": content_type}, method="POST")
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001 - the status is the message
            pass
        raise OSError(f"{what}: the image provider answered {exc.code}" + (f": {detail}" if detail else "")) from None
    except URLError as exc:
        raise OSError(f"{what}: the image provider could not be reached ({str(exc.reason)[:120]})") from None
    except TimeoutError:
        raise OSError(f"{what}: the image provider did not answer within {int(timeout)} s") from None
    except OSError as exc:  # socket timeouts arrive as OSError subclasses
        raise OSError(f"{what}: {str(exc)[:160]}") from None
    try:
        value = json.loads(raw)
    except ValueError:
        raise OSError(f"{what}: the image provider returned no JSON") from None
    if not isinstance(value, dict):
        raise OSError(f"{what}: the image provider returned {type(value).__name__}, not an object")
    return value


def _first_image(value: dict[str, Any], what: str) -> bytes:
    data = value.get("data")
    entry = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else {}
    b64 = entry.get("b64_json")
    if isinstance(b64, str) and b64:
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError):
            raise OSError(f"{what}: the image provider returned unreadable image data") from None
    url = entry.get("url")
    if isinstance(url, str) and url.startswith("http"):
        try:
            with urlopen(url, timeout=60) as response:  # noqa: S310 - the provider named the file
                return response.read()
        except (OSError, ValueError) as exc:
            raise OSError(f"{what}: the image provider's file could not be fetched ({str(exc)[:120]})") from None
    raise OSError(f"{what}: the image provider returned no image")


QUALITIES = ("auto", "low", "medium", "high")


def generate(prompt: str, output: Path, model: str | None = None, refs: list[Path] | None = None,
             timeout: float | None = None, size: str = "1024x1024", quality: str = "auto",
             env: dict[str, str] | None = None, opener: Opener = urlopen) -> Path:
    """One image, written to `output`. With reference images it is an edit; without, a generation."""
    found = credentials(env)
    if found is None:
        raise OSError("the image provider is not configured in this environment")
    base, key = found
    seconds = float(timeout or GENERATE_TIMEOUT_SECONDS)
    name = model_name(model, DEFAULT_IMAGE_MODEL)
    if refs:
        boundary = "jed" + secrets.token_hex(12)
        parts: list[bytes] = []
        for field, value in (("model", name), ("prompt", prompt), ("size", size), ("quality", quality), ("n", "1")):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"\r\n\r\n{value}\r\n".encode())
        for ref in refs:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image[]\"; filename=\"{Path(ref).name}\"\r\n"
                         "Content-Type: image/png\r\n\r\n".encode() + Path(ref).read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        value = _post(f"{base}/v1/images/edits", key, b"".join(parts), f"multipart/form-data; boundary={boundary}",
                      seconds, "image edit", opener)
    else:
        body = json.dumps({"model": name, "prompt": prompt, "size": size, "quality": quality, "n": 1}).encode()
        value = _post(f"{base}/v1/images/generations", key, body, "application/json", seconds, "image generation", opener)
    image = _first_image(value, "image edit" if refs else "image generation")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(image)
    return output


def describe(image: Path, prompt: str, model: str | None = None, timeout: float | None = None,
             env: dict[str, str] | None = None, opener: Opener = urlopen) -> str:
    """The vision model's answer about one image, as text."""
    found = credentials(env)
    if found is None:
        raise OSError("the image provider is not configured in this environment")
    base, key = found
    seconds = float(timeout or DESCRIBE_TIMEOUT_SECONDS)
    encoded = base64.b64encode(Path(image).read_bytes()).decode("ascii")
    body = json.dumps({
        "model": vision_model_name(model),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        ]}],
        "max_tokens": 1500,
        "temperature": 0,
        "reasoning_effort": "none",  # live, 8 September 2026: every vision check came back empty with thinking on
    }).encode()
    value = _post(f"{base}/v1/chat/completions", key, body, "application/json", seconds, "vision check", opener)
    choices = value.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise OSError("vision check: the model returned no text")
    return content


CHAT_TIMEOUT_SECONDS = 60
DEFAULT_CHAT_MODEL = "qwen-3-7-plus"


def chat(prompt: str, model: str | None = None, timeout: float | None = None, temperature: float = 0.0,
         max_tokens: int = 1500, env: dict[str, str] | None = None, opener: Opener = urlopen) -> str:
    """One judgement: the prompt as a single user message, thinking off, the answer's text.

    Probed on the desk's pod, 8 September 2026: with thinking on, Qwen spends
    the whole token budget on reasoning and returns an empty answer;
    `reasoning_effort: "none"` gives the clean JSON the desk needs in about a
    second (13 s through the CLI), ~180 completion tokens.
    """
    found = credentials(env)
    if found is None:
        raise OSError("the model provider is not configured in this environment")
    base, key = found
    body = json.dumps({
        "model": model_name(model, DEFAULT_CHAT_MODEL),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "reasoning_effort": "none",
    }).encode()
    value = _post(f"{base}/v1/chat/completions", key, body, "application/json",
                  float(timeout or CHAT_TIMEOUT_SECONDS), "judgement", opener)
    choices = value.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        content = "".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise OSError("judgement: the model returned no text")
    return content


def timed(seconds_left: float | None, ceiling: float, floor: float = 20.0, margin: float = 20.0) -> float:
    """A call's timeout: the ceiling, or what remains before a deadline minus a margin, never under the floor."""
    if seconds_left is None:
        return ceiling
    return max(floor, min(ceiling, seconds_left - margin))


if __name__ == "__main__":
    print(json.dumps({"available": available(), "time": time.time()}))
