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
import time
from typing import Any, Callable

import cost_components
import estimate_record

import image_provider  # noqa: E402 - sibling module

DEFAULT_MODEL = "litellm-fireworks/qwen-3-7-plus"
CALL_TIMEOUT_SECONDS = 90
PROMPT_LIMIT = 60_000
Runner = Callable[..., subprocess.CompletedProcess[str]]

SPEC_KEYS = (
    "piece_type", "quantity", "metal", "metal_karat", "metal_color", "stone_type",
    "stone_origin", "stone_shape", "stone_carat", "stone_color", "stone_clarity",
    "stone_cut", "stone_count", "stone_carat_basis", "stone_dimensions", "earring_style", "center_stone", "accent_stones", "accent_stone_type", "accent_stone_origin",
    "accent_stone_color", "accent_stone_clarity", "finger_size", "dimensions",
    "setting_style", "finish", "engraving", "event_date", "budget",
    "customer_supplied_materials", "certificate", "reference_images",
    "scheduling_intent", "notes", "pieces",
)
TRIAGE_KINDS = {
    "estimate_request", "not_a_quote_request", "vendor_or_marketing",
    "personal_or_internal", "unrelated", "not_an_estimate_request", "escalation", "inventory_request",
}
ASSESSMENTS = {"unchanged", "changed", "uncertain"}
INTENTS = {"estimate_acceptance", "rendering_request", "appointment_request", "cancellation"}


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


CALL_LOG: list[dict[str, Any]] = []  # one entry per completion this process made: seconds, prompt size, ok


def reset_stats() -> None:
    CALL_LOG.clear()


def stats() -> dict[str, Any]:
    """Calls made so far and the time they took; the tick puts this in its summary."""
    return {
        "model_calls": len(CALL_LOG),
        "model_direct": sum(1 for c in CALL_LOG if c.get("transport") == "direct"),
        "model_seconds": round(sum(c["seconds"] for c in CALL_LOG), 2),
        "prompt_chars": sum(c["prompt_chars"] for c in CALL_LOG),
    }


MODEL_PROVIDER_MODE = "auto"  # profile model.provider: auto (direct when the proxy is reachable) | direct | cli
DRAFT_TEMPERATURE = 0.3  # drafts (customer emails) may vary a little; judgements run at 0


def complete(
    prompt: str,
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
    timeout: int = CALL_TIMEOUT_SECONDS,
    temperature: float = 0.0,
) -> str:
    """One model call: the proxy directly when it is reachable, the platform CLI otherwise.

    Every judgement the desk makes passes through here (RELEASE-PLAN-4.14.md
    2.1): the transport is one decision, the prompts and checks are the same
    either way, and the key is read from the environment at call time.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty text")
    if len(prompt) > PROMPT_LIMIT:
        raise ValueError("prompt exceeds the size limit")
    if image_provider.available(MODEL_PROVIDER_MODE):
        started = time.monotonic()
        try:
            text = image_provider.chat(prompt, model=model or DEFAULT_MODEL, timeout=min(timeout, image_provider.CHAT_TIMEOUT_SECONDS),
                                       temperature=temperature)
        except OSError as exc:
            CALL_LOG.append({"seconds": round(time.monotonic() - started, 3), "prompt_chars": len(prompt), "ok": False, "transport": "direct"})
            refused = "answered 4" in str(exc)  # a 4xx is the request, not the weather
            raise JudgmentError(f"completion call failed: {exc}", transient=not refused) from exc
        CALL_LOG.append({"seconds": round(time.monotonic() - started, 3), "prompt_chars": len(prompt), "ok": True, "transport": "direct"})
        return text
    argv = infer_argv(prompt, model or DEFAULT_MODEL, openclaw or default_openclaw())
    started = time.monotonic()
    try:
        completed = runner(argv, check=False, capture_output=True, text=True, shell=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        CALL_LOG.append({"seconds": round(time.monotonic() - started, 3), "prompt_chars": len(prompt), "ok": False, "transport": "cli"})
        raise JudgmentError(f"completion call failed: {exc}", transient=True) from exc
    CALL_LOG.append({"seconds": round(time.monotonic() - started, 3), "prompt_chars": len(prompt), "ok": completed.returncode == 0, "transport": "cli"})
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
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Ask once, validate, and ask once more with the error if the shape was wrong."""
    last = "no attempt made"
    current = prompt
    for attempt in range(attempts):
        text = complete(current, model, runner, openclaw, temperature=temperature)
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
        "not_an_estimate_request (an appraisal or insurance valuation, or a job-status question); "
        "inventory_request (they ask whether the shop has, stocks, or sells a ready-made piece, something in stock, "
        "available now, or ready to ship, whatever details or budget they give: the shop invites them in to see what "
        "is ready rather than quoting a custom piece); "
        "escalation (anger, a legal threat, a chargeback or insurance dispute, a lost or damaged claim, press, fraud, or price pushback on a quote already sent).\n"
        "When in doubt between estimate_request and anything else, choose estimate_request.\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_triage, model, runner, openclaw)


def _clean_fields(spec: dict[str, Any], allow_pieces: bool) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    placeholders = {"", "n/a", "not specified", "unspecified", "unknown", "tbd", "none", "null"}
    for key, raw in spec.items():
        if key not in SPEC_KEYS or key == "pieces":
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
    if allow_pieces and isinstance(spec.get("pieces"), list):
        pieces = [_clean_fields(p, False) for p in spec["pieces"] if isinstance(p, dict)]
        pieces = [p for p in pieces if p][:4]
        if len(pieces) >= 2:
            # Two or more objects asked for: each is its own piece. One
            # entry is not a list; its facts belong at the top level.
            clean["pieces"] = pieces
        elif len(pieces) == 1:
            for key, value in pieces[0].items():
                clean.setdefault(key, value)
    return clean


def check_specification(value: dict[str, Any]) -> dict[str, Any]:
    spec = value.get("specification")
    if not isinstance(spec, dict):
        raise ValueError("specification must be an object")
    clean = _clean_fields(spec, True)
    if not clean:
        raise ValueError("specification has no usable fields")
    return {"specification": clean}


EXAMPLE_PHOTO_PROMPT = (
    "You are helping a jeweler read a customer's example photo of jewelry. In two or three plain sentences, describe "
    "the jewelry in the photo for intake: the piece type, the metal color, the stones (type and color, shape, roughly "
    "how many, whether one center stone or small accents), the setting style, and any notable design detail. Say only "
    "what is visible; never guess a carat weight, a karat, a ring size, or a length; no prices. Answer in plain text."
)


def photo_clause(photos: list[str] | None) -> str:
    """What the desk read from the customer's example photos, handed to the reading (8 September 2026)."""
    lines = [str(t).strip() for t in (photos or []) if str(t).strip()]
    if not lines:
        return ""
    listed = "\n".join(f"- {line[:600]}" for line in lines[:3])
    return (
        "\nEXAMPLE PHOTOS the customer attached, as read by the desk's vision check (one line per photo):\n"
        f"{listed}\n"
        "The customer's words always come first: the photo is an example of the look, and a fact visible in it (piece "
        "type, metal color, stone type and color, stone shape, setting style, stone count, design details) fills a key "
        "only when the words say nothing about it; a stated fact is never replaced or contradicted by the photo "
        "(\"look like these with 1.5 ct emeralds\" means emeralds, whatever the photo shows). reference_images says "
        "what came from the photo alone (\"from the photo: cushion halos, stud backs\"). Never take a carat weight, a "
        "karat, a ring size, or a length from a photo.\n"
    )


def known_clause(known: dict[str, Any] | None) -> str:
    """The specification the desk already holds for this customer, handed to the reading.

    A customer who writes from a new thread (WORKFLOW.md 6.1, "same") or
    changes a quoted piece (6.8) gives only the new words; the record holds
    the rest. Live, 8 September 2026: "add my initials inside the yellow
    band" on a new thread was read on its own and the gate asked for both
    bands' sizes and karats again.
    """
    if not isinstance(known, dict) or not known:
        return ""
    return (
        "\nKNOWN SPECIFICATION, read from this customer's earlier messages and possibly already quoted: "
        f"{json.dumps(known, sort_keys=True)}\n"
        "The thread below may be a new conversation from the same customer. Merge their newest words into the "
        "known specification: keep every known fact (every piece) unless they change it, apply what they now "
        "say, and return the complete specification, all pieces included.\n"
    )


def extract_specification(
    digest: dict[str, Any],
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
    known: dict[str, Any] | None = None,
    photos: list[str] | None = None,
) -> dict[str, Any]:
    """Every fact the customer gave, merged across the thread, nothing invented."""
    prompt = (
        "You are the intake desk of a retail custom-jewelry shop. From the CUSTOMER messages in "
        "the thread, merge every fact the customer actually stated about the piece into one "
        "specification. Answer with one JSON object only: {\"specification\": {...}}.\n"
        f"Allowed keys (use only those the customer answered): {', '.join(SPEC_KEYS)}.\n"
        "Rules: stone_origin is \"natural\" or \"lab-grown\" only when the customer said so. "
        "center_stone is \"yes\" when the piece has one main feature stone and \"no\" when every stone is a small "
        "accent, pave, melee, or cluster stone (for example 1mm stones filling a logo or a band); an eternity or channel-set "
        "band, or stones all the way around or in the middle of a band, has no center stone and its carat is the total of "
        "the small stones (write it in stone_carat and describe them in accent_stones); omit center_stone when unclear. "
        "metal_karat is a number like 14 or 18 when stated. finger_size is the ring size. "
        "dimensions is length or size for a chain, bracelet, or pendant. "
        "setting_style is the customer's own design wording (classic band, solitaire, bezel, channel-set, halo) or "
        "\"jeweler's choice\" when they explicitly leave it to you; never invent one. "
        "For a pair (earrings, cufflinks, studs, hoops) stone_carat_basis is \"each\" when the carat weight is per stone or "
        "per earring (\"1.5 ct each\", \"per earring\") and \"total\" when it is the pair's total (\"2 ct total\", \"tcw\"); "
        "omit it when the customer did not say. "
        "earring_style is \"stud\", \"hoop\", or \"drop\" when the customer's words say which kind of earrings "
        "(studs, hoops, huggies, drops, dangles); omit it otherwise. "
        "stone_dimensions is the stone size the customer wants in millimetres (\"15mm x 12mm oval\"), the size they "
        "want, not one they are comparing against; omit it otherwise. "
        "stone_color and stone_clarity describe the center or main stone only; a grade the customer gives for the halo, "
        "pave, or accent stones (\"D color VS1 on the halo\") goes in accent_stone_color and accent_stone_clarity, "
        "their kind in accent_stone_type and their origin in accent_stone_origin, never in the center stone's keys. "
        "When the customer explicitly leaves color, clarity, cut, finish, or the carat weight or stone size to the jeweler "
        "(\"whatever you think\", \"work it out from the logo\", \"your call\"), write \"jeweler's choice\" for that key. "
        "When the customer asks for more than one object (an engagement ring and a wedding band, two bands, "
        "earrings and a pendant), put the facts they share at the top level (usually the metal) and list each "
        "object under pieces with its own piece_type, finger_size or dimensions, stones, and setting_style, "
        "even when they call it a matching set; a halo, accent stones, or an engraving on one piece are not a "
        "second piece. Leave pieces out for a single object. "
        "Never write placeholders such as unknown, n/a, or not specified; omit the key instead. "
        "Never include prices, costs, or anything the SHOP messages said. "
        "scheduling_intent is the customer's own words when the message being handled asks to meet, come in, "
        "visit, or bring something to the shop (\"can we meet next week\", \"I can come by Friday\"), or asks to move or "
        "reschedule a meeting, or proposes a day and time (\"something came up, any chance we can do Friday at 4pm?\"); "
        "omit it when they do not ask to meet. "
        "customer_supplied_materials names anything the customer already owns and wants used (\"my mother's "
        "diamond\", \"reset my stone\", \"my own gold\"); when the stone is theirs, still fill stone_type and any "
        "shape or size they gave (stone_carat holds its carat weight or millimetre size), and never ask or invent its grade. "
        "A photo mention can go in reference_images but never fills another key.\n\n"
        f"{photo_clause(photos)}{known_clause(known)}THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_specification, model, runner, openclaw)


def check_triage_and_specification(value: dict[str, Any]) -> dict[str, Any]:
    triaged = check_triage(value)
    if triaged["kind"] != "estimate_request":
        return {**triaged, "specification": {}}
    return {**triaged, **check_specification(value)}


def triage_and_extract(
    digest: dict[str, Any], model: str | None = None, runner: Runner = subprocess.run, openclaw: str | None = None,
    known: dict[str, Any] | None = None,
    photos: list[str] | None = None,
) -> dict[str, Any]:
    """One call for a new inquiry: what the thread is, and every fact the customer gave.

    Two calls became one (RELIABILITY-PLAN.md 7.2). The shape is the union of
    the two single calls, checked by the same rules; a thread that is not an
    estimate request carries an empty specification.
    """
    prompt = (
        "You are the intake desk of a retail custom-jewelry shop. Read the email thread and answer with one "
        "JSON object only: {\"kind\": <one of the kinds below>, \"note\": <one short sentence>, "
        "\"specification\": {...}}.\n"
        "Kinds: estimate_request (they want a custom piece, replica, redesign, remount, or repair quoted); "
        "not_a_quote_request (a person writing about something other than getting a piece made); "
        "vendor_or_marketing (a supplier, sales pitch, or marketing); "
        "personal_or_internal (a personal note or internal shop matter); "
        "unrelated (none of the above); "
        "not_an_estimate_request (an appraisal or insurance valuation, or a job-status question); "
        "inventory_request (they ask whether the shop has, stocks, or sells a ready-made piece, something in stock, "
        "available now, or ready to ship, whatever details or budget they give: the shop invites them in to see what "
        "is ready rather than quoting a custom piece); "
        "escalation (anger, a legal threat, a chargeback or insurance dispute, a lost or damaged claim, press, fraud, or price pushback on a quote already sent).\n"
        "When in doubt between estimate_request and anything else, choose estimate_request.\n"
        "When the kind is estimate_request, merge every fact the CUSTOMER actually stated about the piece into "
        "specification; otherwise leave specification as {}.\n"
        f"Allowed specification keys (use only those the customer answered): {', '.join(SPEC_KEYS)}.\n"
        "Rules: stone_origin is \"natural\" or \"lab-grown\" only when the customer said so. "
        "center_stone is \"yes\" when the piece has one main feature stone and \"no\" when every stone is a small "
        "accent, pave, melee, or cluster stone (for example 1mm stones filling a logo or a band); an eternity or channel-set "
        "band, or stones all the way around or in the middle of a band, has no center stone and its carat is the total of "
        "the small stones (write it in stone_carat and describe them in accent_stones); omit center_stone when unclear. "
        "metal_karat is a number like 14 or 18 when stated. finger_size is the ring size. "
        "dimensions is length or size for a chain, bracelet, or pendant. "
        "setting_style is the customer's own design wording (classic band, solitaire, bezel, channel-set, halo) or "
        "\"jeweler's choice\" when they explicitly leave it to you; never invent one. "
        "For a pair (earrings, cufflinks, studs, hoops) stone_carat_basis is \"each\" when the carat weight is per stone or "
        "per earring (\"1.5 ct each\", \"per earring\") and \"total\" when it is the pair's total (\"2 ct total\", \"tcw\"); "
        "omit it when the customer did not say. "
        "earring_style is \"stud\", \"hoop\", or \"drop\" when the customer's words say which kind of earrings "
        "(studs, hoops, huggies, drops, dangles); omit it otherwise. "
        "stone_dimensions is the stone size the customer wants in millimetres (\"15mm x 12mm oval\"), the size they "
        "want, not one they are comparing against; omit it otherwise. "
        "stone_color and stone_clarity describe the center or main stone only; a grade the customer gives for the halo, "
        "pave, or accent stones (\"D color VS1 on the halo\") goes in accent_stone_color and accent_stone_clarity, "
        "their kind in accent_stone_type and their origin in accent_stone_origin, never in the center stone's keys. "
        "When the customer explicitly leaves color, clarity, cut, finish, or the carat weight or stone size to the jeweler "
        "(\"whatever you think\", \"work it out from the logo\", \"your call\"), write \"jeweler's choice\" for that key. "
        "scheduling_intent is the customer's own words when the message being handled asks to meet, come in, "
        "visit, or bring something to the shop, or asks to move or reschedule a meeting, or proposes a day and time "
        "(\"something came up, any chance we can do Friday at 4pm?\"); omit it when they do not ask to meet. "
        "customer_supplied_materials names anything the customer already owns and wants used (\"my mother's "
        "diamond\", \"reset my stone\"); when the stone is theirs, still fill stone_type and any shape or size they "
        "gave (stone_carat holds its carat weight or millimetre size), and never ask or invent its grade. "
        "When the customer asks for more than one object (an engagement ring and a wedding band, two bands, "
        "earrings and a pendant), put the facts they share at the top level (usually the metal) and list each "
        "object under pieces with its own piece_type, finger_size or dimensions, stones, and setting_style, "
        "even when they call it a matching set; a halo, accent stones, or an engraving on one piece are not a "
        "second piece. Leave pieces out for a single object. "
        "Never write placeholders such as unknown, n/a, or not specified; omit the key instead. "
        "Never include prices, costs, or anything the SHOP messages said.\n\n"
        f"{photo_clause(photos)}{known_clause(known)}THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_triage_and_specification, model, runner, openclaw)


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
        "rendering_request (they ask to see a picture, drawing, or rendering), cancellation (they cancel a meeting they had, or say they no longer want the piece), appointment_request (they ask to meet, or to move or reschedule a meeting, or propose a day and time, "
        "call, or come in). Clear rendering or appointment wording is not uncertain merely because both appear. "
        "changed_fields is an empty list unless the assessment is changed.\n"
        "If the message is price pushback, a discount request, anger, or any escalation, answer "
        '{"post_estimate_artifact": {"design_change_assessment": "uncertain", "intents": [], "changed_fields": []}}.\n\n'
        f"APPROVED SPECIFICATION:\n{json.dumps(approved_specification, sort_keys=True)}\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_artifact, model, runner, openclaw)


QUESTION_STARTS = {"what", "which", "how", "when", "where", "who", "do", "does", "is", "are", "would", "could",
                   "can", "will", "any", "natural", "lab", "yellow", "white", "rose", "round", "size"}


def check_body(value: dict[str, Any]) -> dict[str, Any]:
    body = value.get("body")
    if not isinstance(body, str) or len(body.strip()) < 40:
        raise ValueError("body must be the email text")
    import customer_content_guard  # local import: keeps judge free of the guard's other imports at load

    body = customer_content_guard.plain_text(body.strip())
    if len(body) > 4000:
        raise ValueError("body is too long")
    # Headings and sign-offs dressed as questions ("The setting?", "Warmly,
    # Lomelino Jewelry?") are refused; a short question in a bullet, or one
    # that starts like a question ("Which metal?"), is fine.
    stubs = []
    for line in body.splitlines():
        if line.strip().startswith(("- ", "* ", "• ")):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
            words = sentence.rstrip("?").split()
            if sentence.endswith("?") and 0 < len(words) <= 3 and words[0].lower() not in QUESTION_STARTS:
                stubs.append(sentence)
    if stubs:
        raise ValueError("no headings or sign-offs ending in a question mark (" + "; ".join(stubs[:3]) + "); "
                         "write each question as a full sentence")
    if re.search(r"[$€£]\s*\d|\b\d[\d,]*\s*(?:dollars|usd)\b|\bper carat\b|\bper gram\b", body, re.IGNORECASE):
        raise ValueError("body must not contain a price, rate, or amount")
    if "{{" in body or "}}" in body:
        raise ValueError("body must not contain template placeholders")
    if "?" not in body:
        raise ValueError("a follow-up must ask the customer at least one question")
    recap = [line for line in body.splitlines() if re.match(r"\s*[-*\u2022]\s", line) and "?" not in line
             and re.search(r"\b(I've noted|I have noted|I have you down|noted your|you mentioned)\b", line, re.IGNORECASE)]
    if recap:
        raise ValueError("remove bullets that only restate what the customer said; every bullet must ask something")
    return {"body": body}


CONFIRM_WORDS = ("confirm", "make sure", "double-check", "double check", "just checking", "to be sure", "clarify")

FIELD_WORDS = {
    "finger_size": ("size",), "dimensions": ("length", "size", "long", "inch", "mm"),
    "metal": ("metal", "gold", "platinum", "silver"), "metal_karat": ("karat", "14k", "18k", "10k", "carat gold"),
    "metal_color": ("yellow", "white", "rose", "color", "colour"), "stone_type": ("stone", "diamond", "sapphire", "gem"),
    "stone_origin": ("natural", "lab"), "stone_carat": ("carat", "size", "mm", "big"), "stone_color": ("color", "colour", "grade"),
    "stone_clarity": ("clarity", "grade"), "stone_cut": ("cut", "shape"), "stone_shape": ("shape", "cut"),
    "setting_style": ("set", "style", "solitaire", "halo", "bezel", "prong"), "piece_type": ("piece", "kind", "type"),
    "earring_style": ("stud", "hoop", "drop", "style"),
}


def uncovered_fields(body: str, missing_fields: list[str]) -> list[str]:
    """Missing details the follow-up never mentions. Labels may be 'wedding band: finger size'."""
    text = body.lower()
    out = []
    for label in missing_fields:
        if label.startswith("to confirm: "):
            # A reading check: covered when the body raises its subject at all.
            words = CONFIRM_WORDS
        else:
            field = label.split(": ", 1)[1] if ": " in label else label
            key = field.strip().replace(" ", "_")
            words = FIELD_WORDS.get(key, (field.strip().replace("_", " "),))
        if not any(w in text for w in words):
            out.append(label)
    return out


# Questions a customer cannot answer: the jeweler works these out from the stone and the design (8 September 2026).
BENCH_MEASUREMENT_RE = re.compile(
    r"(?i)\b(?:millimet\w*|\bmm\b|diameters?|circumference|drop length|(?:length|size|width) (?:in|of) (?:mm|millimet)|"
    r"gram(?:s|mage)?\b|gauge|ear ?wires?|post (?:length|type)|exact (?:dimensions?|measurements?|size)|"
    r"precise (?:dimensions?|measurements?)|prong (?:count|number|style)|how many prongs|shank|gallery|melee|alloy|"
    r"band (?:width|thickness)|(?:setting|bezel) height|stone count|how many (?:stones|diamonds) (?:in|for|around)|"
    r"tolerance|purity|fineness|(?:depth|table) percentage)\b"
)


def bench_measurement_questions(body: str) -> list[str]:
    """Lines of an email that ask the customer for a measurement only the bench can decide."""
    return [line.strip() for line in str(body or "").splitlines()
            if "?" in line and BENCH_MEASUREMENT_RE.search(line)]


_COVER_STOP = {"with", "and", "the", "for", "each", "total", "pair", "in", "at", "a", "an"}


def words_covered(body: str, text: str) -> bool:
    """At least half of `text`'s distinctive words appear in the body (a confirmed vision, a promised question)."""
    haystack = re.sub(r"[^a-z0-9 ]+", " ", str(body or "").lower())
    words = [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if w not in _COVER_STOP and len(w) > 2]
    if not words:
        return True
    hits = sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"s?\b", haystack))
    return hits * 2 >= len(words)


def check_body_covers(missing_fields: list[str], understanding: str | None = None, sender: str = "", questions: list[str] | None = None,
                      previous: str = ""):
    def check(value: dict[str, Any]) -> dict[str, Any]:
        result = check_body(value)
        if previous:
            import customer_mail  # local import: customer_mail imports this module

            if customer_mail._opening(result["body"]) and customer_mail._opening(result["body"]) == customer_mail._opening(previous):
                raise ValueError("do not open with the same sentence as the shop's last email on this thread; react to what they just said")
        import gmail_text  # local import: gmail_text does not depend on this module

        other = gmail_text.greets_someone_else(result["body"], sender)
        if other:
            raise ValueError(f"the email is to {sender}, who wrote it; do not greet {other}")
        left = uncovered_fields(result["body"], missing_fields)
        if left:
            raise ValueError("the email must ask about every missing detail; it never mentions: " + "; ".join(left)
                             + ". Ask for each of them, one bullet each")
        if understanding and not words_covered(result["body"], understanding):
            raise ValueError("before the questions, confirm their vision in one sentence the way a jeweler would, naming it: "
                             + understanding[:200])
        if questions is not None:
            bullets = [line for line in result["body"].splitlines() if line.strip().startswith("- ")]
            if len(bullets) > len(questions):
                raise ValueError(f"ask {len(questions)} question{'s' if len(questions) != 1 else ''}, one bullet each, in the desk's words; "
                                 "the metal (which metal, karat, color) is one question")
        bench = bench_measurement_questions(result["body"])
        if bench:
            raise ValueError("never ask the customer a technical question the jeweler works out (millimetres, diameters, drop "
                             "lengths, weights, prong or stone counts, band widths, exact dimensions); ask for a rough preference "
                             "or leave it to the jeweler. Remove: " + " | ".join(bench)[:300])
        return result
    return check


def draft_followup(
    digest: dict[str, Any],
    missing_fields: list[str],
    template: str,
    shop_name: str,
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
    photos: list[str] | None = None,
    understanding: str | None = None,
    questions: list[str] | None = None,
    customer_name: str = "",
    meeting_booked: bool = False,
    welcome_back: bool = False,
) -> dict[str, Any]:
    """One friendly, price-free email asking only for what is still missing; a photo's vision is confirmed first."""
    import gmail_text  # local import: gmail_text does not depend on this module

    sender = (str(customer_name or "").strip().split() or [""])[0].strip(",.") or gmail_text.sender_first_name(digest)
    welcome = ("They refer to a piece from before (one the shop made for them, or one you discussed) and nothing about it is on "
               "file: open by welcoming them back warmly (never say you cannot find it or have no record), and make the first "
               "question the one asking them to remind you a little about the piece, or to send a photo if they have one. "
               if welcome_back else "")
    closing = welcome + ("A meeting with them is already booked: close by saying anything they are unsure of can be settled when you "
               "meet, and do not invite them to come by or to set up a time. " if meeting_booked else
               "Close by inviting them to come by the shop if they would rather talk it through in person, without naming times. ")
    prompt = (
        "You are the jeweler at a small retail custom-jewelry shop writing back to a customer. Write the reply "
        "body (no subject line, no headers) in the tone of the template: warm, personal, unhurried. Open with "
        "one sentence that reacts to what they shared (the occasion, who it is for, a family stone, the idea "
        "they described); never open with a summary of their request. Then ask for every one of the missing "
        "details listed below, in the order given (the first ones matter most to the price), as a short dash "
        "list with one bullet per detail, each bullet a plain question in the customer's words (\"- What ring "
        "size?\", \"- Is the diamond natural or lab-grown?\"); ask only what a customer can answer about what they "
        "want: never a technical question (a millimetre measurement, a stone diameter, a drop length, a weight, a prong "
        "or stone count, a band width) which the jeweler works out from the reference and the description; a size is "
        "asked as a rough preference (\"about how long would you like them?\", \"a delicate or a bold look?\"). Ask "
        "for all of them in this one email so the "
        "customer is not asked twice, and tell them it is fine not to know and you will suggest what usually "
        "looks best. Never write a line that merely restates what they said (no \"I've noted\", no \"I have you "
        "down for\"); never add a timing or budget section unless it asks a question. " + closing + "Keep it "
        "under 180 words. Use the customer's name if they gave one. Never mention prices, costs, rates, or "
        "budgets as requirements. Do not use template placeholders; write real text. No headings or labels, "
        "and the sign-off is a plain line with no question mark. Do not wrap lines: each paragraph is one line, "
        "with a blank line between paragraphs. "
        + (f"The customer writing is {sender} (from their address line): greet {sender}, never someone else they mention, "
           "such as the person the piece is for. " if sender else "")
        + f"Sign off as {shop_name}. Answer with one JSON object only: {{\"body\": \"...\"}}.\n\n"
        f"MISSING DETAILS TO ASK FOR: {', '.join(missing_fields)}\n\n"
        + ((("THE QUESTIONS, in the desk's words, one bullet each and no more (the metal is one question, not three):\n"
             + "\n".join(f"- {q}" for q in questions) + "\n\n")) if questions else "")
        + ((f"THEIR VISION, from their photo and their words: {understanding}\nBefore the questions, confirm it in one "
            "sentence the way a jeweler speaks to a client (for example \"Just so I have your vision right: you are after "
            f"{understanding}.\"), and ask them to say if anything is off. Never call it a summary.\n\n") if understanding else "")
        + (("PHOTO READING (what the desk saw in the photo they attached): " + " | ".join(str(t)[:400] for t in photos[:3])
            + ("\n\n" if understanding else "\nBefore the questions, say in one sentence what you took from their photo (the piece, "
               "the metal color, the stones) and ask them to say if anything is off.\n\n")) if photos else "")
        + f"TEMPLATE (tone and structure only):\n{template}\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    previous = next((str(m.get("body") or "") for m in reversed(digest.get("messages") or []) if m.get("sent_by") == "shop"), "")
    return ask_json(prompt, check_body_covers(list(missing_fields), understanding, sender, questions, previous), model, runner, openclaw,
                    temperature=DRAFT_TEMPERATURE)


LOCAL_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def check_requested_times(value: dict[str, Any]) -> dict[str, Any]:
    raw = value.get("requested_times", [])
    if not isinstance(raw, list):
        raise ValueError("requested_times must be a list")
    times = []
    for item in raw:
        text = _string(item, 80)
        if text and text not in times:
            times.append(text)
    resolved: list[str] = []
    raw_resolved = value.get("resolved_times", [])
    if not isinstance(raw_resolved, list):
        raise ValueError("resolved_times must be a list")
    for item in raw_resolved:
        text = _string(item, 40)
        if not text:
            continue
        if not LOCAL_DATETIME_RE.fullmatch(text):
            raise ValueError("resolved_times entries must look like YYYY-MM-DDTHH:MM")
        if text not in resolved:
            resolved.append(text)
    return {"requested_times": times[:3], "resolved_times": resolved[:3]}


def extract_requested_times(
    digest: dict[str, Any],
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
    now_local: str | None = None,
    timezone_name: str | None = None,
    offered: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The customer's own words about when they want to meet, nothing invented.

    When the words name a specific day and clock time ("tomorrow at 1pm",
    "Tuesday at 10:30"), the model also resolves them to local date-times so
    the calendar can offer that exact slot first. Vague words ("next week",
    "afternoons") resolve to nothing. When the shop has just offered times,
    a customer picking or accepting one ("that's good", "the second one",
    "Monday works") resolves to that offered slot.
    """
    today = (
        f"Today is {now_local} in the shop's timezone ({timezone_name}). "
        if now_local and timezone_name else ""
    )
    offered_text = ""
    if offered:
        lines = "; ".join(f"{o.get('label') or o.get('start')} = {str(o.get('start'))[:16]}" for o in offered[:3])
        offered_text = (
            f"The shop's last email offered these times: {lines}. If the newest customer message accepts or "
            "picks one of them (\"that works\", \"the second one\", \"Monday is fine\"), resolve it to that "
            "offered time exactly as given after the equals sign; if they accept without naming one and only "
            "one was offered, use that one. "
        )
    prompt = (
        "A customer of a jewelry shop asked to meet. From ONLY the newest customer message (marked as the one "
        "being handled), copy the customer's own words about timing, for example \"early next week\", "
        "\"Tuesday afternoon\", \"noon on the 9th\". " + today + offered_text +
        "When a quote names a specific day AND a clock time, also resolve it to a local date-time in the "
        "form YYYY-MM-DDTHH:MM; leave out anything vague. Answer with one JSON object only: "
        '{"requested_times": [<up to three short quotes>], "resolved_times": [<zero to three YYYY-MM-DDTHH:MM>]}. '
        "Use empty lists when they gave no timing. Never invent a time.\n\n"
        f"THREAD:\n{thread_text(digest)}"
    )
    return ask_json(prompt, check_requested_times, model, runner, openclaw)


def resolve_owner_times(
    text: str,
    now_local: str,
    timezone_name: str,
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """Times the owner typed ("Tuesday 2pm or Wednesday at 11") as local date-times."""
    prompt = (
        f"Today is {now_local} in the shop's timezone ({timezone_name}). A jewelry shop owner wrote when they "
        "could meet a customer. Copy each time they named and resolve it to a local date-time in the form "
        "YYYY-MM-DDTHH:MM; a day without a clock time resolves to nothing. Answer with one JSON object only: "
        '{"requested_times": [<their words, up to three>], "resolved_times": [<up to three YYYY-MM-DDTHH:MM>]}. '
        f"Never invent a time.\n\nOWNER WROTE:\n{text[:600]}"
    )
    return ask_json(prompt, check_requested_times, model, runner, openclaw)


ORIGIN_WORDS = {"natural": "natural", "lab": "lab-grown", "labgrown": "lab-grown", "moissanite": "lab-grown"}


def key_origin(key: str) -> str | None:
    text = key.lower().replace("_", " ").replace("-", " ")
    for word, origin in ORIGIN_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            return origin
    return None


def check_quantities(value: dict[str, Any], fee_catalog: list[str], stone_catalog: list[str], needs_carat: bool,
                     stone_origin: str | None = None) -> dict[str, Any]:
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
        origin = key_origin(key)
        if origin is not None:
            stated = (stone_origin or "").lower().replace("_", "-").replace(" ", "-")
            stated = "lab-grown" if stated.startswith("lab") else ("natural" if stated == "natural" else "")
            if not stated:
                raise ValueError(f"accent '{key}' names a stone origin but the customer never said natural or lab-grown; the origin must be asked, not assumed")
            if stated != origin:
                raise ValueError(f"accent '{key}' is {origin} but the customer said {stated}")
        if isinstance(carats, bool) or not isinstance(carats, (int, float)) or carats <= 0:
            raise ValueError("accent carats must be a positive number")
        accents.append({"key": key, "carats": float(carats)})
    result: dict[str, Any] = {"finished_grams": grams, "bench_hours": hours, "fees": fees, "accents": accents}
    if carat is not None:
        result["center_carat"] = carat
    return result


def check_piece_quantities(value: dict[str, Any], pieces: list[dict[str, Any]], fee_catalog: list[str],
                           stone_catalog: list[str]) -> dict[str, Any]:
    raw = value.get("pieces")
    if not isinstance(raw, list) or len(raw) != len(pieces):
        raise ValueError(f"pieces must be a list of exactly {len(pieces)} objects, one per piece in order")
    checked = []
    for info, item in zip(pieces, raw):
        if not isinstance(item, dict):
            raise ValueError("each piece is an object")
        one = check_quantities(item, fee_catalog, stone_catalog, bool(info.get("needs_carat")), str(info.get("stone_origin") or ""))
        one["label"] = str(info.get("label"))
        checked.append(one)
    return {"pieces": checked}


def choose_quantities_per_piece(
    specification: dict[str, Any], pieces: list[dict[str, Any]], fee_catalog: list[str], stone_catalog: list[str],
    typical_weights: dict[str, Any], model: str | None, runner: Runner, openclaw: str | None,
) -> dict[str, Any]:
    """One call, one set of numbers per piece (MULTI-PIECE-PLAN.md batch 2)."""
    merged = estimate_record.pieces_of(specification)
    menu = [{"label": info["label"],
             "specification": merged[info["index"]] if isinstance(info.get("index"), int) and info["index"] < len(merged) else {},
             "center_carat_needed": bool(info.get("needs_carat"))} for info in pieces]
    prompt = (
        "You are an experienced bench jeweler estimating quantities for a price quote, deliberately on the "
        "high side so the shop is never underpaid. This order has more than one piece; give one set of "
        "numbers per piece, in the order listed. Answer with one JSON object only: "
        '{"pieces": [{"finished_grams": <number>, "bench_hours": <number>, "center_carat": <number, only where '
        'center_carat_needed is true>, "fees": [<catalog keys that apply to this piece>], '
        '"accents": [{"key": <stone catalog key>, "carats": <total carats>}]}, ...]}.\n'
        "finished_grams: finished metal weight of that piece"
        + (f"; the shop's typical finished weights by piece type are {json.dumps(typical_weights)}" if typical_weights else "")
        + ".\nbench_hours: bench labor hours for that piece.\n"
        f"fees: choose only from these catalog keys, per piece, including every one that piece needs: {fee_catalog}.\n"
        f"accents: accent or melee stones only if that piece has them, using only these catalog keys: {stone_catalog}; otherwise an empty list.\n\n"
        f"PIECES TO QUANTIFY: {json.dumps(menu, sort_keys=True)}"
    )
    return ask_json(prompt, lambda value: check_piece_quantities(value, pieces, fee_catalog, stone_catalog), model, runner, openclaw)


def choose_quantities(
    specification: dict[str, Any],
    fill: dict[str, str],
    fee_catalog: list[str],
    stone_catalog: list[str],
    typical_weights: dict[str, Any],
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
    pieces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The few numbers a bench jeweler would estimate before pricing, on the high side."""
    if pieces and len(pieces) > 1:
        # A piece already quoted (a "second piece" reopen) keeps the numbers
        # it was quoted on; the model is asked only about the others.
        open_pieces = [info for info in pieces if not info.get("prior_quantities")]
        answered = iter(
            choose_quantities_per_piece(specification, open_pieces, fee_catalog, stone_catalog, typical_weights, model, runner, openclaw)["pieces"]
            if open_pieces else []
        )
        return {"pieces": [
            {**{k: v for k, v in info["prior_quantities"].items() if k != "twin_of"}, "label": str(info.get("label"))}
            if info.get("prior_quantities") else next(answered)
            for info in pieces
        ]}
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
        lambda value: check_quantities(value, fee_catalog, stone_catalog, needs_carat, str(specification.get("stone_origin") or "")),
        model, runner, openclaw,
    )
