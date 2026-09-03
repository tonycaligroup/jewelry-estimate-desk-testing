#!/usr/bin/env python3
"""Finish one claim inside the watcher tick, with no worker job.

The judgment steps are one-shot completions (judge.py); everything with a
side effect is the same deterministic command a worker would have run
(workflow_safe.py). A claim that needs judgment now costs two or three model
calls and finishes in the tick that discovered it. Rendering and appointment
work still needs the agent's tools, so those post-estimate next actions are
handed to a worker job exactly as before.

The switch lives in `<desk>/pipeline.json` (`{"inline": true, "model": ...}`)
so turning it on needs no rebind and the first live claim through it is a
deliberate choice.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

import estimate_record
import gmail_text
import inbox_monitor
import judge
import kolo_safe
import owner_questions
import rendering_materialize
import spec_gate
import workflow_safe

Runner = Callable[..., subprocess.CompletedProcess[str]]
SWITCH_FILE = "pipeline.json"
TEMPLATE_FILE = "spec-gate-email.md"
NOT_AN_INQUIRY = {"not_a_quote_request", "vendor_or_marketing", "personal_or_internal", "unrelated"}


def settings(desk: Path) -> dict[str, Any]:
    """The inline switch; absent or unreadable means off."""
    path = desk / SWITCH_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"inline": False}
    if not isinstance(value, dict):
        return {"inline": False}
    return {"inline": bool(value.get("inline")), "model": value.get("model") or None}


def _template_text(base_dir: Path) -> str:
    path = base_dir / "templates" / TEMPLATE_FILE
    text = path.read_text(encoding="utf-8")
    marker = "**Email body:**"
    return text[text.index(marker) + len(marker):] if marker in text else text


def _namespace(p: dict[str, Path], message_id: str, estimate_id: str, **extra: Any) -> argparse.Namespace:
    return argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        shop_profile=p["shop_profile"], message_id=message_id, estimate_id=estimate_id, **extra,
    )


def _manual_review(p: dict[str, Path], message_id: str, reason: str, runner: Runner) -> dict[str, Any]:
    kolo_safe.manual_review_claimed(p["monitor_root"], p["claim_root"], message_id, None, reason, runner=runner)
    return {"outcome": "manual_review", "reason_code": reason, "next": "done"}


def _send_followup(
    p: dict[str, Path], base_dir: Path, message_id: str, estimate_id: str,
    digest: dict[str, Any], missing: list[str], initiating: bool, paths: dict[str, str],
    profile: dict[str, Any], model: str | None, judge_runner: Runner, openclaw: str | None,
) -> dict[str, Any]:
    shop_name = (profile.get("shop") or {}).get("name") or "the shop"
    drafted = judge.draft_followup(digest, missing, _template_text(base_dir), shop_name, model, judge_runner, openclaw)
    body_path = Path(paths["customer_reply"])
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(drafted["body"] + "\n", encoding="utf-8")
    workflow_safe.send_spec_followup(_namespace(
        p, message_id, estimate_id,
        route=Path(paths["route"]), body=body_path,
        gmail_payload=Path(paths["gmail_payload"]),
        provider_response=Path(paths["gmail_provider_response"]),
        record_output=Path(paths["current_record"]), initiating=initiating,
    ))
    return {"outcome": "followup_sent", "missing_required_fields": missing, "next": "done"}


RENDERING_NOTE = (
    "Attached are visual illustrations of the design direction we discussed. The "
    "written specification and the final design you approve control the finished "
    "piece.\n\nIf you would like an adjustment to the look, reply here and tell me "
    "what you would like changed.\n"
)
RENDERING_VIEWS = (
    "front three-quarter view on a plain white background",
    "side profile view on a plain white background",
)


def rendering_prompts(specification: dict[str, Any]) -> list[str]:
    """Two complementary views of the same approved design, from the spec alone."""
    piece = owner_questions.summary_of_piece(specification) if specification else "a piece of custom jewelry"
    details = ", ".join(
        f"{key.replace('_', ' ')}: {value}"
        for key, value in sorted(specification.items())
        if isinstance(value, (str, int, float)) and not isinstance(value, bool)
    )[:900]
    base = (
        f"Photorealistic product rendering of {piece}, exactly as specified, no alternate designs, "
        f"no text, no people. Specification: {details}. "
    )
    return [base + view for view in RENDERING_VIEWS]


def _image_generate_argv(prompt: str, openclaw: str) -> list[str]:
    return [openclaw, "infer", "image", "generate", "--prompt", prompt, "--json"]


def render_and_send(
    p: dict[str, Path], message_id: str, estimate_id: str, record: dict[str, Any],
    paths: dict[str, str], openclaw: str, command_runner: Runner,
) -> dict[str, Any]:
    """Generate two views from the shell, materialize them, and send the note."""
    images: list[Path] = []
    for slot, prompt in enumerate(rendering_prompts(record.get("specification") or {}), start=1):
        completed = command_runner(
            _image_generate_argv(prompt, openclaw), check=True, capture_output=True, text=True, shell=False,
        )
        raw = completed.stdout or ""
        try:
            envelope = json.loads(raw[raw.find("{"):])
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("image generation returned no JSON") from exc
        outputs = envelope.get("outputs") or []
        source = outputs[0].get("path") if outputs and isinstance(outputs[0], dict) else None
        if not isinstance(source, str) or not source:
            raise ValueError("image generation returned no file path")
        materialized = rendering_materialize.materialize(
            p["monitor_root"], p["claim_root"], message_id, Path(source), slot
        )
        images.append(Path(str(materialized["path"])))
    body_path = Path(paths["customer_reply"])
    body_path.write_text(RENDERING_NOTE, encoding="utf-8")
    workflow_safe.send_rendering(argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        message_id=message_id, estimate_id=estimate_id, body=body_path, images=images,
        gmail_payload=Path(paths["gmail_payload"]), provider_response=Path(paths["gmail_provider_response"]),
        record_output=Path(paths["current_record"]),
    ))
    return {"outcome": "rendering_sent", "images": len(images), "next": "done"}


def post_estimate_actions(
    p: dict[str, Path], message_id: str, estimate_id: str, record: dict[str, Any], next_action: str,
    paths: dict[str, str], openclaw: str, command_runner: Runner,
) -> dict[str, Any]:
    """Appointment approvals and renderings from the tick; a worker only on failure."""
    wants_appointment = next_action in ("request_appointment_approval", "request_appointment_approval_then_send_rendering")
    wants_rendering = next_action in ("send_rendering", "request_appointment_approval_then_send_rendering")
    if wants_appointment:
        intent_path = Path(paths["appointment_intent"])
        workflow_safe.write_private(intent_path, {"requested_times": [], "calendar_availability": []})
        workflow_safe.request_appointment_approval(argparse.Namespace(
            monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
            message_id=message_id, estimate_id=estimate_id, appointment_intent=intent_path,
            appointment_approval=Path(paths["appointment_approval"]), record_output=Path(paths["current_record"]),
            defer_finalize_for_rendering=wants_rendering,
        ))
        if not wants_rendering:
            return {"outcome": "appointment_approval_requested", "next": "done"}
    try:
        return render_and_send(p, message_id, estimate_id, record, paths, openclaw, command_runner)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        # The worker still has the agent's image tool; let it take this one.
        return {"outcome": "needs_worker", "branch": "post_estimate", "next_action": next_action,
                "error": str(exc)[:160]}


def process_claim(
    workspace: Path,
    base_dir: Path,
    message_id: str,
    intake: dict[str, Any],
    model: str | None = None,
    judge_runner: Runner = subprocess.run,
    command_runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """Take a claim from the intake result to its finished state, or hand off.

    Returns a summary with `outcome`; `outcome: needs_worker` means the
    claim is still processing and the caller should spawn a worker job for
    the returned `branch` (rendering or appointment work).
    """
    desk = workspace / "estimate-desk"
    p = {
        "monitor_root": desk / "inbox-monitor",
        "claim_root": desk / "inbox-claims",
        "record_root": desk / "records",
        "shop_profile": desk / "shop-profile.json",
    }
    estimate_id = intake["estimate_id"]
    paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
    profile = workflow_safe.read_object(p["shop_profile"])
    mailbox = (profile.get("shop") or {}).get("outbound_mailbox")
    thread = workflow_safe.read_object(Path(paths["gmail_thread"]))
    digest = gmail_text.thread_digest(thread, message_id, mailbox)
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))

    # Dead-spot guard: a review already said "ask", nothing was sent yet.
    pending = estimate_record.pending_followup(record, message_id)
    if pending is not None:
        return _send_followup(
            p, base_dir, message_id, estimate_id, digest, pending["missing_required_fields"],
            pending["initiating"], paths, profile, model, judge_runner, openclaw,
        )

    review_path = Path(paths["work_dir"]) / "review.json"
    post_estimate = record.get("status") in workflow_safe.SENT_STATUSES
    if post_estimate:
        artifact = judge.classify_reply(digest, record.get("specification") or {}, model, judge_runner, openclaw)
        workflow_safe.write_private(review_path, artifact)
        reviewed = workflow_safe.review_thread(_namespace(p, message_id, estimate_id, review=review_path, runner=command_runner))
        nxt = reviewed.get("next")
        if nxt in ("finalize", "manual_review", "done"):
            return {"outcome": "post_estimate_finished", "next_action": nxt, "next": "done"}
        return post_estimate_actions(
            p, message_id, estimate_id, record, nxt, paths, openclaw or judge.default_openclaw(), command_runner,
        )

    triage = judge.triage(digest, model, judge_runner, openclaw)
    if triage["kind"] in NOT_AN_INQUIRY:
        workflow_safe.not_an_inquiry(_namespace(
            p, message_id, estimate_id, reason=triage["kind"], record_output=Path(paths["current_record"]),
        ))
        return {"outcome": "not_an_inquiry", "reason": triage["kind"], "next": "done"}
    if triage["kind"] == "not_an_estimate_request":
        return _manual_review(p, message_id, "not_an_estimate_request", command_runner)
    if triage["kind"] == "escalation":
        return _manual_review(p, message_id, "customer_escalation", command_runner)

    extracted = judge.extract_specification(digest, model, judge_runner, openclaw)
    specification = extracted["specification"]
    missing = spec_gate.missing_required_fields(specification, profile)
    workflow_safe.write_private(review_path, {"specification": specification, "missing_required_fields": missing})
    reviewed = workflow_safe.review_thread(_namespace(p, message_id, estimate_id, review=review_path, runner=command_runner))
    nxt = reviewed.get("next")
    if nxt == "done":
        return {"outcome": reviewed.get("outcome", "done"), "next": "done"}
    if nxt == "send_spec_followup":
        return _send_followup(
            p, base_dir, message_id, estimate_id, digest, reviewed["missing_required_fields"],
            reviewed["initiating"], paths, profile, model, judge_runner, openclaw,
        )
    if nxt == "price":
        chosen = judge.choose_quantities(
            specification, reviewed["fill"], reviewed["fee_catalog"], reviewed["stone_catalog"],
            reviewed.get("typical_finished_weights") or {}, model, judge_runner, openclaw,
        )
        priced = workflow_safe.price(_namespace(
            p, message_id, estimate_id,
            finished_grams=chosen["finished_grams"], bench_hours=chosen["bench_hours"],
            center_carat=chosen.get("center_carat"), fees=chosen["fees"],
            accents=[f"{a['key']}:{a['carats']}" for a in chosen["accents"]],
        ))
        return {"outcome": "approval_requested", "proposed_price": priced.get("proposed_price"), "next": "done"}
    raise ValueError(f"review-thread returned an unknown next step {nxt!r}")
