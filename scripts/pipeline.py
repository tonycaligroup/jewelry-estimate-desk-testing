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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import estimate_record
import gmail_text
import inbox_monitor
import judge
import kolo_safe
import owner_questions
import rendering_materialize
import slots
import reading_check
import spec_gate
import workflow_safe

Runner = Callable[..., subprocess.CompletedProcess[str]]
SWITCH_FILE = "pipeline.json"
TEMPLATE_FILE = "spec-gate-email.md"
NOT_AN_INQUIRY = {"not_a_quote_request", "vendor_or_marketing", "personal_or_internal", "unrelated"}


def settings(desk: Path) -> dict[str, Any]:
    """The inline switch: on unless pipeline.json says {"inline": false}.

    Inline judgment (a few stateless model calls inside the tick) is the
    normal path since 3 September 2026; worker jobs are the fallback. A pod
    can still opt out with the file.
    """
    path = desk / SWITCH_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"inline": True, "model": None}
    if not isinstance(value, dict):
        return {"inline": True, "model": None}
    return {"inline": value.get("inline", True) is not False, "model": value.get("model") or None}


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


FIELD_QUESTIONS = {
    "stone_origin": "would you like natural or lab-grown stones?",
    "stone_type": "which stone would you like?",
    "stone_carat": "what carat weight or stone size do you have in mind?",
    "stone_color": "any preference on stone color?",
    "stone_clarity": "any preference on clarity?",
    "stone_cut": "which cut or shape?",
    "metal": "which metal would you like?",
    "metal_karat": "which karat, 14K or 18K?",
    "metal_color": "yellow, white, or rose?",
    "finger_size": "what ring size?",
    "dimensions": "what length or size should it be?",
    "setting_style": "what setting style do you have in mind?",
    "piece_type": "what kind of piece is this for?",
}


FIELD_PRIORITY = (
    "stone_origin", "stone_type", "stone_carat", "finger_size", "dimensions", "metal", "metal_karat", "metal_color",
    "stone_shape", "stone_cut", "setting_style", "stone_color", "stone_clarity",
)


def prioritized(missing: list[str]) -> list[str]:
    """The fields that move the price first: origin, stone, size; the follow-up asks for all of them, in this order.

    A reading check to confirm comes before everything (it decides what the
    piece even is); multi-piece names sort by piece first, then by the same
    field order.
    """
    rank = {name: index for index, name in enumerate(FIELD_PRIORITY)}

    def key(name: str):
        if reading_check.is_confirm(name):
            return (-2, -1, name)
        index, field = estimate_record.split_field_name(name)
        return (index if index is not None else -1, rank.get(field, len(rank)), field)

    return sorted(missing, key=key)


def describe_missing(specification: dict[str, Any], missing: list[str]) -> list[str]:
    """Missing names as the customer would read them: 'wedding band: finger size'; a check to confirm is its question."""
    labels = []
    for name in missing:
        if reading_check.is_confirm(name):
            labels.append("to confirm: " + (reading_check.question_for(name) or name))
            continue
        index, field = estimate_record.split_field_name(name)
        words = field.replace("_", " ")
        labels.append(f"{estimate_record.piece_label(specification, index)}: {words}" if index is not None else words)
    return labels


def plain_followup(missing: list[str], shop_name: str, specification: dict[str, Any] | None = None) -> str:
    asks = []
    for name in missing[:8]:
        if reading_check.is_confirm(name):
            asks.append(reading_check.question_for(name) or name)
            continue
        index, field = estimate_record.split_field_name(name)
        question = FIELD_QUESTIONS.get(field, f"could you tell us the {field.replace('_', ' ')}?")
        if index is not None:
            question = f"for the {estimate_record.piece_label(specification or {}, index)}, {question}"
        asks.append(question)
    lines = "\n".join(f"- {q[0].upper() + q[1:]}" for q in asks) or "- Is there anything else we should know?"
    return (
        "Hello,\n\nThank you for reaching out. To put together an accurate estimate, could you share:\n\n"
        f"{lines}\n\nIf you are not sure about any of these, tell us the look you are after and we will recommend.\n\n{shop_name}\n"
    )


def _send_followup(
    p: dict[str, Path], base_dir: Path, message_id: str, estimate_id: str,
    digest: dict[str, Any], missing: list[str], initiating: bool, paths: dict[str, str],
    profile: dict[str, Any], model: str | None, judge_runner: Runner, openclaw: str | None,
    command_runner: Runner = subprocess.run,
) -> dict[str, Any]:
    shop_name = (profile.get("shop") or {}).get("name") or "the shop"
    missing = prioritized(missing)
    try:
        record_now = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
        specification = record_now.get("specification") or {}
    except (OSError, ValueError):
        specification = {}
    try:
        drafted = judge.draft_followup(digest, describe_missing(specification, missing), _template_text(base_dir),
                                       shop_name, model, judge_runner, openclaw)
    except judge.JudgmentError as exc:
        if exc.transient:
            raise
        # The model could not write a proper question twice; a plain one
        # still moves the inquiry, and the owner sees nothing odd.
        drafted = {"body": plain_followup(missing, shop_name, specification)}
    body_path = Path(paths["customer_reply"])
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(drafted["body"] + "\n", encoding="utf-8")
    workflow_safe.send_spec_followup(_namespace(
        p, message_id, estimate_id,
        route=Path(paths["route"]), body=body_path,
        gmail_payload=Path(paths["gmail_payload"]),
        provider_response=Path(paths["gmail_provider_response"]),
        record_output=Path(paths["current_record"]), initiating=initiating,
        runner=command_runner,
    ))
    return {"outcome": "followup_sent", "missing_required_fields": missing, "next": "done"}


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
    model: str | None = None, judge_runner: Runner | None = None,
) -> dict[str, Any]:
    """Plan, render two checked views, materialize them, and ask the owner."""
    import artwork as artwork_module
    import rendering

    work_dir = Path(paths["work_dir"])
    thread = workflow_safe.read_object(Path(paths["gmail_thread"])) if Path(paths["gmail_thread"]).exists() else {}
    art = None
    try:
        import gateway_token  # local import; only needed when the thread carries images

        found = artwork_module.collect(thread, work_dir / "artwork", gateway_token.load_token())
        art = found[-1] if found else None
    except Exception:  # noqa: BLE001 - artwork is a bonus; a render without it still goes to the owner
        art = None
    # A revision: the owner passed on the last views and said what should
    # change. Only the pieces their words name are rendered again.
    change_path = work_dir / "rendering-change.json"
    change = workflow_safe.read_object(change_path) if change_path.exists() else {}
    note = str(change.get("note") or "").strip()
    previous = None
    only: list[str] = []
    if note:
        report_path = work_dir / "rendering-report.json"
        previous = workflow_safe.read_object(report_path) if report_path.exists() else None
        labels = [str(pc.get("label") or "") for pc in (previous or {}).get("pieces") or []]
        only = list(change.get("pieces") or []) or rendering.pieces_named(note, labels)
    # The vision model that grades the views: the profile may pin one
    # (rendering.vision_model); otherwise the desk's default, which is the
    # model the pod's own image tool reports, never the job environment's
    # guess (a one-shot job resolved a model the instance had no right to
    # use, 6 September 2026).
    try:
        profile_now = workflow_safe.read_object(p["shop_profile"]) if p.get("shop_profile") else {}
    except (OSError, ValueError):
        profile_now = {}
    vision_model = str(((profile_now.get("rendering") or {}).get("vision_model") or "")).strip() or rendering.DEFAULT_VISION_MODEL
    try:
        report = rendering.run_pieces(
            record.get("specification") or {}, work_dir / "renders", openclaw, artwork=art,
            context=judge.thread_text(gmail_text.thread_digest(thread, message_id)) if thread else "",
            model=model, runner=command_runner, change=note, only=only, previous=previous, vision_model=vision_model,
        )
    except judge.JudgmentError as exc:
        raise ValueError(f"rendering plan failed: {exc}") from exc
    images: list[Path] = []
    # The slot files are write-once (a different image in a slot is refused).
    # A revision replaces the images the owner passed on, and a run after a
    # crash replaces what the dead run left; either way the previous file is
    # kept beside the slot as history, never sent, never lost.
    label = f"r{int(change.get('round') or 2) - 1}" if note else "prev"
    for slot in range(1, 5):
        slot_path = Path(paths.get(f"rendering_image_{slot}") or "")
        if slot_path.is_file():
            archived = slot_path.with_name(f"{slot_path.stem}-{label}{slot_path.suffix}")
            index = 1
            while archived.exists():
                index += 1
                archived = slot_path.with_name(f"{slot_path.stem}-{label}-{index}{slot_path.suffix}")
            slot_path.rename(archived)
    for view in report["views"][:4]:
        materialized = rendering_materialize.materialize(
            p["monitor_root"], p["claim_root"], message_id, Path(view["image"]), view["slot"]
        )
        images.append(Path(str(materialized["path"])))
    multi = len(report.get("pieces") or []) > 1
    checker = "; ".join(
        f"view {v['slot']}" + (f" ({v.get('piece')})" if multi else "") + " "
        + ("not machine-checked (the vision check was unavailable)" if v.get("unchecked")
           else ("passed" if v["passed"] else "failed " + ", ".join(v["failed"])))
        + f" ({v['attempts']} attempt{'s' if v['attempts'] != 1 else ''})"
        for v in report["views"]
    )
    workflow_safe.write_private(work_dir / "rendering-report.json", report)
    # WORKFLOW.md 6.6: renderings are approval-gated at every stage. The owner
    # sees the views in chat and gets a card; nothing reaches the customer
    # until they approve.
    return workflow_safe.request_rendering_approval(argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        shop_profile=p.get("shop_profile"), message_id=message_id, estimate_id=estimate_id,
        runner=command_runner, checker=checker,
        archetype=", ".join(dict.fromkeys(str((pc.get("plan") or {}).get("archetype") or "")
                                          for pc in report.get("pieces") or [{"plan": report["plan"]}])).strip(", "),
        revised=note or None, revision=int(change.get("round") or 1) if note else 1,
    ))


def _last_offered_times(p: dict[str, Path], estimate_id: str | None) -> list[dict[str, Any]]:
    """The options in the shop's most recent offer to this customer, if any."""
    if not estimate_id:
        return []
    try:
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    except (OSError, ValueError):
        return []
    offers = record.get("times_offered") or []
    if not isinstance(offers, list) or not offers:
        return []
    last = offers[-1]
    return list(last.get("options") or []) if isinstance(last, dict) else []


def appointment_intent(
    p: dict[str, Path], digest: dict[str, Any], paths: dict[str, str],
    model: str | None, judge_runner: Runner, openclaw: str | None, token: str | None = None,
    estimate_id: str | None = None,
) -> dict[str, Any]:
    """What the customer asked for, plus live-checked times from the windows."""
    profile = workflow_safe.read_object(p["shop_profile"])
    scheduling = profile.get("scheduling") or {}
    zone_name = scheduling.get("timezone") or "UTC"
    try:
        from zoneinfo import ZoneInfo

        now_local = datetime.now(ZoneInfo(zone_name)).strftime("%A %Y-%m-%d %H:%M")
    except (KeyError, ValueError, OSError):
        now_local = None
    offered_before = _last_offered_times(p, estimate_id)
    try:
        judged = judge.extract_requested_times(
            digest, model, judge_runner, openclaw, now_local=now_local, timezone_name=zone_name,
            offered=offered_before,
        )
        asked, resolved = judged["requested_times"], judged.get("resolved_times", [])
    except judge.JudgmentError:
        asked, resolved = [], []
    intent: dict[str, Any] = {"requested_times": asked, "resolved_times": resolved, "calendar_availability": []}
    if not scheduling.get("calendar") or not slots.parse_windows(scheduling):
        intent["availability_note"] = "no calendar or declared windows configured"
        return intent
    try:
        import gateway_token  # local import; only needed when a calendar is configured

        offered = slots.offer_times(
            profile, token or gateway_token.load_token(), Path(paths["work_dir"]), requested=resolved,
        )
        intent["calendar_availability"] = [
            {"start": o["start"], "end": o["end"], "label": o["label"]} for o in offered["options"]
        ]
        intent["mode"] = offered.get("mode", "offer")
        if offered.get("reason"):
            intent["availability_note"] = offered["reason"]
        elif offered.get("mode") == "offer" and offered.get("requested_slot"):
            intent["availability_note"] = "the time they asked for is taken; these are free"
    except (OSError, ValueError, KeyError) as exc:
        intent["availability_note"] = f"calendar check failed: {str(exc)[:100]}"
    return intent


def post_estimate_actions(
    p: dict[str, Path], message_id: str, estimate_id: str, record: dict[str, Any], next_action: str,
    paths: dict[str, str], openclaw: str, command_runner: Runner,
    digest: dict[str, Any] | None = None, model: str | None = None, judge_runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Appointment approvals and rendering approvals from the tick; a worker only on failure."""
    wants_appointment = next_action in ("request_appointment_approval", "request_appointment_approval_then_send_rendering")
    wants_rendering = next_action in ("send_rendering", "request_appointment_approval_then_send_rendering")
    if wants_appointment:
        intent_path = Path(paths["appointment_intent"])
        workflow_safe.write_private(
            intent_path, appointment_intent(p, digest or {"messages": []}, paths, model, judge_runner, openclaw, estimate_id=estimate_id)
        )
        workflow_safe.request_appointment_approval(argparse.Namespace(
            monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
            shop_profile=p.get("shop_profile"),
            message_id=message_id, estimate_id=estimate_id, appointment_intent=intent_path,
            appointment_approval=Path(paths["appointment_approval"]), record_output=Path(paths["current_record"]),
            defer_finalize_for_rendering=wants_rendering, runner=command_runner, judge_runner=judge_runner,
        ))
        if not wants_rendering:
            return {"outcome": "appointment_approval_requested", "next": "done"}
    if settings(p["monitor_root"].resolve().parent).get("render_job", True):
        # The rendering runs in its own job with its own clock; the tick
        # spawns it and moves on (ARCHITECTURE-OPTIONS.md C').
        return {"outcome": "render_job_requested", "next": "spawn_render", "estimate_id": estimate_id}
    return render_and_send(p, message_id, estimate_id, record, paths, openclaw, command_runner, model=model, judge_runner=judge_runner)


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
            pending["initiating"], paths, profile, model, judge_runner, openclaw, command_runner,
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
            digest=digest, model=model, judge_runner=judge_runner,
        )

    initiating = (record.get("route") or {}).get("gmail_message_id") == message_id
    # A reply on a thread that already has a record is the same conversation
    # continuing; only the message that opened the record is triaged. A
    # customer asking "what does this have to do with it?" is not junk mail.
    judged = judge.triage_and_extract(digest, model, judge_runner, openclaw) if initiating else None
    triage = {"kind": judged["kind"], "note": judged["note"]} if judged else {"kind": "estimate_request", "note": "reply on an open estimate"}
    if triage["kind"] in NOT_AN_INQUIRY:
        workflow_safe.not_an_inquiry(_namespace(
            p, message_id, estimate_id, reason=triage["kind"], record_output=Path(paths["current_record"]),
        ))
        return {"outcome": "not_an_inquiry", "reason": triage["kind"], "next": "done"}
    if triage["kind"] == "not_an_estimate_request":
        return _manual_review(p, message_id, "not_an_estimate_request", command_runner)
    if triage["kind"] == "escalation":
        return _manual_review(p, message_id, "customer_escalation", command_runner)

    specification = judged["specification"] if judged else judge.extract_specification(digest, model, judge_runner, openclaw)["specification"]
    missing = spec_gate.missing_required_fields(specification, profile)
    # ARCHITECTURE-OPTIONS.md E': the reading is checked against the
    # customer's own words in code. A disagreement is never priced; it is
    # one more line the follow-up asks, alongside everything else missing.
    disagreements = reading_check.compare(digest, specification)
    if disagreements:
        workflow_safe.write_private(Path(paths["work_dir"]) / "reading-check.json", {"disagreements": disagreements})
        missing = missing + [d["name"] for d in disagreements if d["name"] not in missing]
    workflow_safe.write_private(review_path, {"specification": specification, "missing_required_fields": missing})
    reviewed = workflow_safe.review_thread(_namespace(p, message_id, estimate_id, review=review_path, runner=command_runner))
    nxt = reviewed.get("next")
    if nxt == "done":
        return {"outcome": reviewed.get("outcome", "done"), "next": "done"}
    if nxt == "send_spec_followup":
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
        if specification.get("scheduling_intent") and not record.get("appointment_booked"):
            # Meeting first: a customer who asks to come in gets the meeting,
            # not a questionnaire. The details are settled at the meeting or
            # in a later email, and pricing picks up from there.
            intent_path = Path(paths["appointment_intent"])
            workflow_safe.write_private(intent_path, appointment_intent(
                p, digest, paths, model, judge_runner, openclaw, estimate_id=estimate_id,
            ))
            workflow_safe.request_appointment_approval(argparse.Namespace(
                monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
                shop_profile=p.get("shop_profile"), message_id=message_id, estimate_id=estimate_id,
                appointment_intent=intent_path, appointment_approval=Path(paths["appointment_approval"]),
                record_output=Path(paths["current_record"]), defer_finalize_for_rendering=False,
                runner=command_runner, judge_runner=judge_runner,
            ))
            return {"outcome": "appointment_approval_requested", "before_estimate": True, "next": "done"}
        repeated = estimate_record.followup_stalled(record, message_id, reviewed["missing_required_fields"])
        if repeated and not reviewed["initiating"]:
            # The customer was already asked for exactly this and did not
            # answer it. Asking twice reads as a broken record; the owner
            # decides what happens next.
            asked = workflow_safe.ask_followup_stalled(
                _namespace(p, message_id, estimate_id, runner=command_runner), record, repeated,
            )
            return {"outcome": "awaiting_owner", "question_id": asked.get("question_id"), "next": "done"}
        return _send_followup(
            p, base_dir, message_id, estimate_id, digest, reviewed["missing_required_fields"],
            reviewed["initiating"], paths, profile, model, judge_runner, openclaw, command_runner,
        )
    if nxt == "price":
        return _price_after_review(p, message_id, estimate_id, specification, reviewed, model, judge_runner, openclaw, command_runner)
    raise ValueError(f"review-thread returned an unknown next step {nxt!r}")


def resend_followup(
    workspace: Path, base_dir: Path, message_id: str, estimate_id: str,
    model: str | None = None, judge_runner: Runner = subprocess.run, command_runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """The owner said ask again: send the follow-up the stall check held back."""
    desk = workspace / "estimate-desk"
    p = {"monitor_root": desk / "inbox-monitor", "claim_root": desk / "inbox-claims", "record_root": desk / "records",
         "shop_profile": desk / "shop-profile.json"}
    paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
    if not Path(paths["gmail_thread"]).exists():
        import gateway_token  # local import; only needed on a replay
        import gmail_fetch

        gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token())
    profile = workflow_safe.read_object(p["shop_profile"])
    thread = workflow_safe.read_object(Path(paths["gmail_thread"]))
    digest = gmail_text.thread_digest(thread, message_id, (profile.get("shop") or {}).get("outbound_mailbox"))
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    missing = list(record.get("missing_required_fields") or [])
    if not missing:
        raise ValueError("nothing is missing any more; the estimate can be priced")
    return _send_followup(p, base_dir, message_id, estimate_id, digest, missing, False, paths, profile,
                          model, judge_runner, openclaw, command_runner)


def _price_after_review(
    p: dict[str, Path], message_id: str, estimate_id: str, specification: dict[str, Any], reviewed: dict[str, Any],
    model: str | None, judge_runner: Runner, openclaw: str | None, command_runner: Runner = subprocess.run,
) -> dict[str, Any]:
    chosen = judge.choose_quantities(
        specification, reviewed["fill"], reviewed["fee_catalog"], reviewed["stone_catalog"],
        reviewed.get("typical_finished_weights") or {}, model, judge_runner, openclaw,
        pieces=reviewed.get("pieces") or None,
    )
    if "pieces" in chosen:
        priced = workflow_safe.price(_namespace(
            p, message_id, estimate_id, finished_grams=None, bench_hours=None, center_carat=None, fees=[], accents=[],
            pieces=chosen["pieces"], runner=command_runner, judge_runner=judge_runner,
        ))
    else:
        priced = workflow_safe.price(_namespace(
            p, message_id, estimate_id,
            finished_grams=chosen["finished_grams"], bench_hours=chosen["bench_hours"],
            center_carat=chosen.get("center_carat"), fees=chosen["fees"],
            accents=[f"{a['key']}:{a['carats']}" for a in chosen["accents"]],
            runner=command_runner, judge_runner=judge_runner,
        ))
    return {"outcome": "approval_requested", "proposed_price": priced.get("proposed_price"), "next": "done"}


def price_from_record(
    workspace: Path, message_id: str, estimate_id: str,
    model: str | None = None, judge_runner: Runner = subprocess.run, command_runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """After the owner supplied a missing rate: price from the review already on
    the record. The specification is not judged again, so the review recorded
    before the question stands and nothing conflicts."""
    desk = workspace / "estimate-desk"
    p = {"monitor_root": desk / "inbox-monitor", "claim_root": desk / "inbox-claims", "record_root": desk / "records",
         "shop_profile": desk / "shop-profile.json"}
    paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
    if not Path(paths["gmail_thread"]).exists():
        # A review that parked this claim cleaned its work folder; fetch again.
        import gateway_token  # local import; only needed on a replay
        import gmail_fetch

        gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token())
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    specification = record.get("specification") or {}
    if not specification:
        raise ValueError("the record has no reviewed specification to price")
    review_path = Path(paths["work_dir"]) / "review.json"
    workflow_safe.write_private(review_path, {
        "specification": specification, "missing_required_fields": list(record.get("missing_required_fields") or []),
    })
    reviewed = workflow_safe.review_thread(_namespace(p, message_id, estimate_id, review=review_path, runner=command_runner))
    nxt = reviewed.get("next")
    if nxt == "price":
        return _price_after_review(p, message_id, estimate_id, specification, reviewed, model, judge_runner, openclaw, command_runner)
    if nxt == "done":
        return {"outcome": reviewed.get("outcome", "done"), "next": "done"}
    raise ValueError(f"the record is not ready to price (review said {nxt!r})")
