#!/usr/bin/env python3
"""One-shot judgment calls: prompt in, validated JSON out, no agent turn.

The desk's judgment steps are small and well shaped: pull a specification
out of a thread, classify a reply, write a short price-free email, choose a
few quantities. Each is one call to the platform's stateless completion
command (`openclaw infer model run`), parsed strictly and validated against
the shape the caller needs, with one retry when the model returns something
malformed. No tools, no session, no shell for the model: it returns data,
and the deterministic commands do everything with side effects.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable

import cost_components

DEFAULT_MODEL = "litellm-fireworks/qwen-3-7-plus"
CALL_TIMEOUT_SECONDS = 120
PROMPT_LIMIT = 60_000
Runner = Callable[..., subprocess.CompletedProcess[str]]

SPEC_KEYS = (
    "piece_type", "quantity", "metal", "metal_karat", "metal_color", "stone_type",
    "stone_origin", "stone_shape", "stone_carat", "stone_color", "stone_clarity",
    "stone_cut", "stone_count", "accent_stones", "finger_size", "dimensions",
    "setting_style", "finish", "engraving", "event_date", "budget",
    "customer_supplied_materials", "certificate", "reference_images",
    "scheduling_intent", "notes",
)
TRIAGE_KINDS = {
    "estimate_request", "not_a_quote_request", "vendor_or_marketing",
    "personal_or_internal", "unrelated", "not_an_estimate_request", "escalation",
}
ASSESSMENTS = {"unchanged", "changed", "uncertain"}
INTENTS = {"estimate_acceptance", "rendering_request", "appointment_request"}


class JudgmentError(RuntimeError):
    """The model could not be called or would not return a usable answer."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


def default_openclaw() -> str:
    return shutil.which("openclaw") or "/usr/local/bin/openclaw"


def infer_argv(prompt: str, model: str, openclaw: str) -> list[str]:
    """Argument array for one stateless completion; never a shell string.

    `model run` is a lean provider completion: no agent turn, no tools, no
    concurrency slot. There is no system-prompt or JSON-mode flag, so the
    contract lives in the prompt and the answer is validated here. Thinking
    is off explicitly; the models that honor it are slower with it on.
    """
    return [
        openclaw, "infer", "model", "run",
        "--model", model, "--thinking", "off", "--json", "--prompt", prompt,
    ]


def _unwrap(stdout: str) -> str:
    """The text inside the CLI's JSON envelope.

    The documented envelope is `{"ok": true, "outputs": [{"text": ...}]}`;
    older or other shapes are walked generically as a fallback.
    """
    raw = stdout.strip()
    if not raw:
        return ""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(value, dict):
        if value.get("ok") is False:
            raise JudgmentError(
                f"completion reported failure: {str(value.get('error') or value)[:200]}", transient=True
            )
        outputs = value.get("outputs")
        if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict):
            text = outputs[0].get("text")
            if isinstance(text, str):
                return text
    for _ in range(4):
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("text", "output", "content", "response", "result", "message", "choices", "data", "completion"):
                if key in value:
                    value = value[key]
                    break
            else:
                return json.dumps(value)
        elif isinstance(value, list) and value:
            value = value[0]
        else:
            return raw
    return value if isinstance(value, str) else json.dumps(value)


def complete(
    prompt: str,
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
    timeout: int = CALL_TIMEOUT_SECONDS,
) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty text")
    if len(prompt) > PROMPT_LIMIT:
        raise ValueError("prompt exceeds the size limit")
    argv = infer_argv(prompt, model or DEFAULT_MODEL, openclaw or default_openclaw())
    try:
        completed = runner(argv, check=False, capture_output=True, text=True, shell=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JudgmentError(f"completion call failed: {exc}", transient=True) from exc
    if completed.returncode != 0:
        raise JudgmentError(
            f"completion exited {completed.returncode}: {(completed.stderr or completed.stdout or '')[:200]}",
            transient=True,
        )
    text = _unwrap(completed.stdout or "")
    if not text.strip():
        raise JudgmentError("completion returned no text", transient=True)
    return text


def extract_json(text: str) -> dict[str, Any]:
    """The first JSON object in a completion, tolerant of fences and chatter."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the completion")
    value = json.loads(candidate[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("completion JSON is not an object")
    return value


def ask_json(
    prompt: str,
    check: Callable[[dict[str, Any]], dict[str, Any]],
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
    attempts: int = 2,
) -> dict[str, Any]:
    """Ask once, validate, and ask once more with the error if the shape was wrong."""
    last = "no attempt made"
    current = prompt
    for attempt in range(attempts):
        text = complete(current, model, runner, openclaw)
        try:
            return check(extract_json(text))
        except (ValueError, json.JSONDecodeError) as exc:
            last = str(exc)
            current = (
                f"{prompt}\n\nYour previous answer was rejected: {last}. "
                "Return only the JSON object described above, nothing else."
            )
    raise JudgmentError(f"completion malformed after {attempts} attempts: {last}")


def thread_text(digest: dict[str, Any]) -> str:
    """The digest as the model sees it: sender, date, body, oldest first."""
    lines: list[str] = []
    for message in digest.get("messages") or []:
        who = "SHOP" if message.get("sent_by") == "shop" else "CUSTOMER"
        flag = " (the newest message, the one being handled)" if message.get("claimed") else ""
        lines.append(f"--- {who}{flag} | {message.get('date') or ''} | subject: {message.get('subject') or ''}")
        lines.append(message.get("body") or "")
    return "\n".join(lines)


def _string(value: Any, limit: int = 200) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = str(value).strip()
    return text[:limit]


# --------------------------------------------------------------------------
# Judgments
# --------------------------------------------------------------------------

def check_triage(value: dict[str, Any]) -> dict[str, Any]:
    kind = _string(value.get("kind"), 40).lower()
    if kind not in TRIAGE_KINDS:
        raise ValueError(f"kind must be one of {sorted(TRIAGE_KINDS)}")
    return {"kind": kind, "note": _string(value.get("note"), 300)}


def triage(digest: dict[str, Any], model: str | None = None, runner: Runner = subprocess.run, openclaw: str | None = None) -> dict[str, Any]:
    """Is this thread a request for a custom piece at all?"""
    prompt = (
        "You are the intake desk of a retail custom-jewelry shop. Read the email thread and "
        "decide what the CUSTOMER messages are. Answer with one JSON object only: "
        '{"kind": <one of the kinds below>, "note": <one short sentence>}.\n'
        "Kinds: estimate_request (they want a custom piece, replica, redesign, remount, or repair quoted); "
        "not_a_quote_request (a person writing about something other than getting a piece made); "
        "vendor_or_marketing (a supplier, sales pitch, or marketing); "
        "personal_or_internal (a personal note or internal shop matter); "
        "unrelated (none of the above); "
        "not_an_estimate_request (an appraisal or insurance valuation, the price of existing inventory, or a job-status question); "
        "escalation (anger, a legal threat, a chargeback or insurance dispute, a lost or damaged claim, press, fraud, or price pushback on a quote already sent).\n"
        "When in doubt between estimate_request and anything else, choose estimate_request.\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_triage, model, runner, openclaw)


def check_specification(value: dict[str, Any]) -> dict[str, Any]:
    spec = value.get("specification")
    if not isinstance(spec, dict):
        raise ValueError("specification must be an object")
    clean: dict[str, Any] = {}
    placeholders = {"", "n/a", "not specified", "unspecified", "unknown", "tbd", "none", "null"}
    for key, raw in spec.items():
        if key not in SPEC_KEYS:
            continue
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            clean[key] = raw
            continue
        if isinstance(raw, list):
            items = [_string(item, 120) for item in raw if _string(item, 120)]
            if items:
                clean[key] = items[:12]
            continue
        text = _string(raw, 200)
        if text and text.lower() not in placeholders:
            clean[key] = text
    if not clean:
        raise ValueError("specification has no usable fields")
    return {"specification": clean}


def extract_specification(
    digest: dict[str, Any],
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """Every fact the customer gave, merged across the thread, nothing invented."""
    prompt = (
        "You are the intake desk of a retail custom-jewelry shop. From the CUSTOMER messages in "
        "the thread, merge every fact the customer actually stated about the piece into one "
        "specification. Answer with one JSON object only: {\"specification\": {...}}.\n"
        f"Allowed keys (use only those the customer answered): {', '.join(SPEC_KEYS)}.\n"
        "Rules: stone_origin is \"natural\" or \"lab-grown\" only when the customer said so. "
        "metal_karat is a number like 14 or 18 when stated. finger_size is the ring size. "
        "dimensions is length or size for a chain, bracelet, or pendant. "
        "setting_style is the customer's own design wording (classic band, solitaire, bezel, channel-set, halo) or "
        "\"jeweler's choice\" when they explicitly leave it to you; never invent one. "
        "When the customer explicitly leaves color, clarity, cut, or finish to the jeweler, write \"jeweler's choice\" for that key. "
        "Never write placeholders such as unknown, n/a, or not specified; omit the key instead. "
        "Never include prices, costs, or anything the SHOP messages said. "
        "A photo mention can go in reference_images but never fills another key.\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_specification, model, runner, openclaw)


def check_artifact(value: dict[str, Any]) -> dict[str, Any]:
    artifact = value.get("post_estimate_artifact", value)
    if not isinstance(artifact, dict):
        raise ValueError("post_estimate_artifact must be an object")
    assessment = _string(artifact.get("design_change_assessment"), 20).lower()
    if assessment not in ASSESSMENTS:
        raise ValueError("design_change_assessment must be unchanged, changed, or uncertain")
    intents_raw = artifact.get("intents")
    if not isinstance(intents_raw, list):
        raise ValueError("intents must be a list")
    intents = []
    for item in intents_raw:
        name = _string(item, 40).lower()
        if name not in INTENTS:
            raise ValueError(f"intents must be chosen from {sorted(INTENTS)}")
        if name not in intents:
            intents.append(name)
    changed_raw = artifact.get("changed_fields", [])
    if not isinstance(changed_raw, list):
        raise ValueError("changed_fields must be a list")
    changed = [_string(item, 60).lower() for item in changed_raw if _string(item, 60)]
    if assessment != "changed" and changed:
        raise ValueError("changed_fields must be empty unless the assessment is changed")
    if assessment == "changed" and not changed:
        raise ValueError("a changed assessment needs changed_fields")
    return {"post_estimate_artifact": {
        "design_change_assessment": assessment, "intents": intents, "changed_fields": changed,
    }}


def classify_reply(
    digest: dict[str, Any],
    approved_specification: dict[str, Any],
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """What the newest customer message means against the approved design."""
    prompt = (
        "You are the desk of a retail custom-jewelry shop. An estimate for the approved design below "
        "was already sent to this customer. Classify ONLY the newest customer message (marked as the "
        "one being handled). Answer with one JSON object only: "
        '{"post_estimate_artifact": {"design_change_assessment": ..., "intents": [...], "changed_fields": [...]}}.\n'
        "design_change_assessment: \"unchanged\" if the message keeps the approved design; \"changed\" if it "
        "clearly alters a field of it (then list those field keys in changed_fields); \"uncertain\" if it might "
        "alter the design, asks for a second or different piece, or cannot be mapped confidently.\n"
        "intents: every explicit intent, chosen only from estimate_acceptance (they accept or say go ahead), "
        "rendering_request (they ask to see a picture, drawing, or rendering), appointment_request (they ask to meet, "
        "call, or come in). Clear rendering or appointment wording is not uncertain merely because both appear. "
        "changed_fields is an empty list unless the assessment is changed.\n"
        "If the message is price pushback, a discount request, anger, or any escalation, answer "
        '{"post_estimate_artifact": {"design_change_assessment": "uncertain", "intents": [], "changed_fields": []}}.\n\n'
        f"APPROVED SPECIFICATION:\n{json.dumps(approved_specification, sort_keys=True)}\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_artifact, model, runner, openclaw)


def check_body(value: dict[str, Any]) -> dict[str, Any]:
    body = value.get("body")
    if not isinstance(body, str) or len(body.strip()) < 40:
        raise ValueError("body must be the email text")
    body = body.strip()
    if len(body) > 4000:
        raise ValueError("body is too long")
    if re.search(r"[$€£]\s*\d|\b\d[\d,]*\s*(?:dollars|usd)\b|\bper carat\b|\bper gram\b", body, re.IGNORECASE):
        raise ValueError("body must not contain a price, rate, or amount")
    if "{{" in body or "}}" in body:
        raise ValueError("body must not contain template placeholders")
    return {"body": body}


def draft_followup(
    digest: dict[str, Any],
    missing_fields: list[str],
    template: str,
    shop_name: str,
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """One friendly, price-free email asking only for what is still missing."""
    prompt = (
        "You write customer emails for a retail custom-jewelry shop. Write the reply body (no subject line, "
        "no headers) asking the customer only for the missing details listed below, following the tone and "
        "structure of the template. Confirm what they already told you in a half-sentence instead of asking "
        "again. Never mention prices, costs, rates, or budgets as requirements; budget and dates may be "
        "invited but are optional. Do not offer meeting times. Do not use template placeholders; write real "
        f"text. Sign off as {shop_name}. Answer with one JSON object only: {{\"body\": \"...\"}}.\n\n"
        f"MISSING DETAILS TO ASK FOR: {', '.join(missing_fields)}\n\n"
        f"TEMPLATE (tone and structure only):\n{template}\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_body, model, runner, openclaw)


def check_quantities(value: dict[str, Any], fee_catalog: list[str], stone_catalog: list[str], needs_carat: bool) -> dict[str, Any]:
    def positive(name: str, required: bool) -> float | None:
        raw = value.get(name)
        if raw is None:
            if required:
                raise ValueError(f"{name} is required")
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
            raise ValueError(f"{name} must be a positive number")
        return float(raw)

    grams = positive("finished_grams", True)
    hours = positive("bench_hours", True)
    carat = positive("center_carat", needs_carat)
    fees_raw = value.get("fees", [])
    if not isinstance(fees_raw, list):
        raise ValueError("fees must be a list of catalog keys")
    fees = []
    for item in fees_raw:
        key = _string(item, 60)
        if key not in fee_catalog:
            raise ValueError(f"fee '{key}' is not in the catalog {fee_catalog}")
        if key not in fees:
            fees.append(key)
    accents_raw = value.get("accents", [])
    if not isinstance(accents_raw, list):
        raise ValueError("accents must be a list")
    accents = []
    for item in accents_raw:
        if not isinstance(item, dict):
            raise ValueError("each accent is {\"key\": ..., \"carats\": ...}")
        key = _string(item.get("key"), 60)
        carats = item.get("carats")
        if key not in stone_catalog:
            raise ValueError(f"accent '{key}' is not in the catalog {stone_catalog}")
        if isinstance(carats, bool) or not isinstance(carats, (int, float)) or carats <= 0:
            raise ValueError("accent carats must be a positive number")
        accents.append({"key": key, "carats": float(carats)})
    result: dict[str, Any] = {"finished_grams": grams, "bench_hours": hours, "fees": fees, "accents": accents}
    if carat is not None:
        result["center_carat"] = carat
    return result


def choose_quantities(
    specification: dict[str, Any],
    fill: dict[str, str],
    fee_catalog: list[str],
    stone_catalog: list[str],
    typical_weights: dict[str, Any],
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """The few numbers a bench jeweler would estimate before pricing, on the high side."""
    needs_carat = any(key.startswith("stone_lines[0].quantity") for key in fill)
    metal = cost_components.extract_metal(specification)
    stone = cost_components.extract_center_stone(specification)
    prompt = (
        "You are an experienced bench jeweler estimating quantities for a price quote, deliberately on the "
        "high side so the shop is never underpaid. Answer with one JSON object only: "
        '{"finished_grams": <number>, "bench_hours": <number>, "center_carat": <number, only if asked below>, '
        '"fees": [<catalog keys that apply>], "accents": [{"key": <stone catalog key>, "carats": <total carats>}]}.\n'
        f"finished_grams: finished metal weight in grams of {metal.get('description') or 'the metal'} for this piece"
        + (f"; the shop's typical finished weights by piece type are {json.dumps(typical_weights)}" if typical_weights else "")
        + ".\nbench_hours: bench labor hours for the whole job.\n"
        + ("center_carat: the center stone carat weight is not stated; estimate it from the description.\n" if needs_carat else "")
        + f"fees: choose only from these catalog keys, including every one this job needs: {fee_catalog}.\n"
        f"accents: accent or melee stones only if the design has them, using only these catalog keys: {stone_catalog}; otherwise an empty list.\n\n"
        f"SPECIFICATION:\n{json.dumps(specification, sort_keys=True)}\n"
        f"CENTER STONE READ BY THE SHOP: {json.dumps(stone)}\n"
        f"QUANTITIES THE SHOP NEEDS: {json.dumps(fill)}"
    )
    return ask_json(
        prompt,
        lambda value: check_quantities(value, fee_catalog, stone_catalog, needs_carat),
        model, runner, openclaw,
    )
