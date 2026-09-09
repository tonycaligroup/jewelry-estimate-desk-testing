#!/usr/bin/env python3
"""High-level fail-closed workflow actions that never copy claim tokens by hand."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import approval_guard
import brief_registry
import activation_binding
import calendar_query
import customer_content_guard
import customer_mail
import estimate_record
import gmail_reply
import gmail_safe
import gmail_text
import spot_price
import inbox_claim
import gateway_token
import gmail_classify
import gmail_route
import inbox_monitor
import judge
import kolo_safe
import owner_questions
import rehearsal
import run_lease
import cost_components
import route_ownership
import slots


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_private(path: Path, value: Any) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def mirror_record(record: dict[str, Any], path: Path) -> None:
    write_private(path, record)
    subprocess.run(
        kolo_safe.build_record_upsert(
            "skill.jewelry_estimate", record["estimate_id"], path, record["status"]
        ),
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def workspace_of(monitor_root: Path) -> Path:
    return monitor_root.resolve().parent.parent


def execute_line(monitor_root: Path, subcommand: str, **flags: str) -> str:
    """The one command the main session runs when a brief is approved."""
    parts = [f"python3 {skill_dir()}/scripts/workflow_safe.py {subcommand}", f"--workspace {workspace_of(monitor_root)}"]
    for key, value in flags.items():
        parts.append(f"--{key.replace('_', '-')} {value}")
    return " ".join(parts)


def approval_store_path(monitor_root: Path, estimate_id: str, message_id: str) -> Path:
    """Durable private copy of one appointment approval, outside claim work."""
    return workspace_of(monitor_root) / "estimate-desk" / "approvals" / f"{estimate_id}-{inbox_claim.claim_key(message_id)[:16]}.json"


def _register_brief(monitor_root: Path, kind: str, title: str, estimate_id: str, message_id: str, runner: Any) -> None:
    """Best effort: the card is already filed; a missing id only disables the rejection poll for it."""
    try:
        brief_registry.register(monitor_root, kind, title, estimate_id, message_id, runner=runner or subprocess.run)
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass


def handle_rejected_briefs(workspace: Path, runner: Any = subprocess.run) -> list[dict[str, Any]]:
    """Every tick: cards the owner rejected since the last poll, acted on.

    Appointment: wake the dormant question so the owner is asked what to do.
    Rendering: close the parked claim, nothing sent, one notice. Price: one
    notice; the estimate stays pending until the owner says more in chat.
    """
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(workspace)
    handled: list[dict[str, Any]] = []
    for entry in brief_registry.rejected_since_last_poll(p["monitor_root"], runner=runner):
        kind, estimate_id, message_id = entry["kind"], entry["estimate_id"], entry["message_id"]
        channel = kolo_safe.owner_channel_args(p["monitor_root"])
        try:
            if kind == "appointment":
                root = owner_questions.questions_root(p["monitor_root"])
                qid = owner_questions.question_id(estimate_id, "appointment_next", message_id)
                question = owner_questions.load(root, qid)
                if question["status"] == "open":
                    question["dormant"] = False
                    owner_questions.save(root, question)
                    question = _attach_answer_command(root, p["monitor_root"], question)
                    owner_questions.deliver(root, question, runner=runner, extra_args=channel)
            elif kind == "rendering":
                _ask_rendering_next(p, estimate_id, message_id, runner)
            elif kind == "price":
                _ask_price_next(p, estimate_id, message_id, runner)
            brief_registry.mark(p["monitor_root"], entry["brief_id"], "rejected", entry.get("note"))
            handled.append({"brief_id": entry["brief_id"], "kind": kind, "estimate_id": estimate_id})
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            handled.append({"brief_id": entry["brief_id"], "kind": kind, "error": str(exc)[:160]})
    return handled


def handle_approved_briefs(workspace: Path, runner: Any = subprocess.run) -> list[dict[str, Any]]:
    """Every tick: every card the owner approved, executed here.

    The same executors the session would run, with the same lease, journal,
    and failure question; if the session runs the line too, the second run
    finds the first one's journal and does nothing more. Cards are binary
    (WORKFLOW 6.4, 6 September 2026), so an approval means the card as filed,
    the price card included.
    """
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(workspace)
    handled: list[dict[str, Any]] = []
    approved = brief_registry.approved_since_last_poll(p["monitor_root"], runner=runner)
    approved_ids = {e["brief_id"] for e in approved}
    for entry in approved:
        handled.append(_run_approved_brief(workspace, p, entry, approved_ids))
    handled.extend(_release_held_briefs(workspace, p))
    return handled


def _brief_argv(workspace: Path, p: dict[str, Path], entry: dict[str, Any]) -> list[str] | None:
    kind, estimate_id, message_id, brief_id = entry["kind"], entry["estimate_id"], entry["message_id"], entry["brief_id"]
    if kind == "price":
        return ["send-approved-estimate-brief", "--workspace", str(workspace), "--estimate-id", estimate_id, "--brief-id", brief_id]
    if kind == "rendering":
        return ["send-approved-rendering", "--workspace", str(workspace), "--estimate-id", estimate_id,
                "--message-id", message_id, "--brief-id", brief_id]
    if kind == "appointment":
        approval = read_object(approval_store_path(p["monitor_root"], estimate_id, message_id))
        command = "book-approved-appointment" if approval.get("action_type") == "appointment_booking" else "send-approved-times"
        return [command, "--workspace", str(workspace), "--estimate-id", estimate_id, "--message-id", message_id, "--brief-id", brief_id]
    return None


def _bundle_mode(p: dict[str, Path], entry: dict[str, Any], argv: list[str], approved_ids: set[str]) -> tuple[str, dict[str, Any] | None]:
    """hold, bundle (with the held partner), or send: cards born from one customer email travel together.

    The owner, 9 September 2026: a booking and a rendering approved from
    one reply went out as two emails. The first approved of a pair holds
    its email (a booking still lands on the calendar); the second sends one
    email carrying both. An offer of times is never part of a pair.
    """
    if argv[0] == "send-approved-times":
        return "send", None
    partners = [s for s in brief_registry.siblings(p["monitor_root"], entry) if not _is_offer_card(p, s)]
    held = next((s for s in partners if s.get("outcome") == "held"), None)
    if held is not None:
        return "bundle", held
    open_partner = any(
        s.get("outcome") == "pending" or (s.get("brief_id") in approved_ids and s.get("outcome") != "executed")
        for s in partners
    )
    return ("hold", None) if open_partner else ("send", None)


def _is_offer_card(p: dict[str, Path], entry: dict[str, Any]) -> bool:
    """An offer of times is never part of a pair: its email carries the questions and goes out on its own."""
    if entry.get("kind") != "appointment":
        return False
    try:
        approval = read_object(approval_store_path(p["monitor_root"], entry["estimate_id"], entry["message_id"]))
    except (OSError, ValueError):
        return False
    return approval.get("action_type") != "appointment_booking"


def _run_approved_brief(workspace: Path, p: dict[str, Path], entry: dict[str, Any], approved_ids: set[str]) -> dict[str, Any]:
    kind, estimate_id, message_id, brief_id = entry["kind"], entry["estimate_id"], entry["message_id"], entry["brief_id"]
    try:
        argv = _brief_argv(workspace, p, entry)
        if argv is None:
            return {"brief_id": brief_id, "kind": kind, "outcome": "skipped"}
        mode, partner = _bundle_mode(p, entry, argv, approved_ids)
        if mode == "hold":
            argv = argv + (["--hold-email"] if argv[0] == "book-approved-appointment" else ["--hold"])
        elif mode == "bundle" and partner is not None:
            argv = argv + ["--with-held", partner["brief_id"]]
        buffer, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
            code = main(argv)
        printed = buffer.getvalue().strip()
        if code == LEASE_HELD_EXIT:
            # The session (or a retry) is running this very line now. Its
            # own run reports the brief or asks the owner; this approval
            # stays pending and is tried again next tick.
            brief_registry.mark(p["monitor_root"], brief_id, "pending", "another run in progress",
                                approved_at=entry.get("approved_at") or datetime.now(timezone.utc).isoformat())
            return {"brief_id": brief_id, "kind": kind, "estimate_id": estimate_id, "command": argv[0], "outcome": "in_progress"}
        if code == 0 and mode == "hold":
            brief_registry.mark(p["monitor_root"], brief_id, "held", "waiting for its partner card from the same email",
                                held_at=datetime.now(timezone.utc).isoformat())
            outcome = "held"
        else:
            brief_registry.mark(p["monitor_root"], brief_id, "executed" if code == 0 else "failed",
                                None if code == 0 else errors.getvalue().strip()[:300])
            outcome = "executed" if code == 0 else "failed"
        return {"brief_id": brief_id, "kind": kind, "estimate_id": estimate_id, "command": argv[0], "outcome": outcome,
                **({"bundled_with": partner["brief_id"]} if partner else {}),
                **({"result": json.loads(printed)} if code == 0 and printed.startswith("{") else {}),
                **({"error": errors.getvalue().strip()[-300:]} if code != 0 else {})}
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        return {"brief_id": brief_id, "kind": kind, "error": str(exc)[:160]}


def _release_held_briefs(workspace: Path, p: dict[str, Path]) -> list[dict[str, Any]]:
    """A held send goes alone when its partner was rejected, or when the partner is still undecided after HOLD_MINUTES."""
    handled: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for entry in brief_registry.load_all(p["monitor_root"]):
        if entry.get("outcome") != "held":
            continue
        partners = [s for s in brief_registry.siblings(p["monitor_root"], entry) if not _is_offer_card(p, s)]
        rejected = any(s.get("outcome") == "rejected" for s in partners)
        undecided = any(s.get("outcome") in ("pending", "failed") for s in partners)  # a failed partner is retried or asked about
        try:
            held_at = datetime.fromisoformat(str(entry.get("held_at") or entry.get("decided_at") or now.isoformat()))
        except ValueError:
            held_at = now
        if held_at.tzinfo is None:
            held_at = held_at.replace(tzinfo=timezone.utc)
        stale = (now - held_at) >= timedelta(minutes=brief_registry.HOLD_MINUTES)
        if not (rejected or (undecided and stale) or not partners):
            continue
        why = "its partner card was rejected" if rejected else "its partner card stayed undecided" if undecided else "no partner card left"
        try:
            argv = _brief_argv(workspace, p, entry)
            if argv is None:
                continue
            argv = argv + ["--released"]
            buffer, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(errors):
                code = main(argv)
            printed = buffer.getvalue().strip()
            if code == LEASE_HELD_EXIT:
                continue
            brief_registry.mark(p["monitor_root"], entry["brief_id"], "executed" if code == 0 else "failed",
                                f"sent alone: {why}" if code == 0 else errors.getvalue().strip()[:300])
            handled.append({"brief_id": entry["brief_id"], "kind": entry["kind"], "estimate_id": entry["estimate_id"], "command": argv[0],
                            "outcome": "executed" if code == 0 else "failed", "released": why,
                            **({"result": json.loads(printed)} if code == 0 and printed.startswith("{") else {}),
                            **({"error": errors.getvalue().strip()[-300:]} if code != 0 else {})})
        except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            handled.append({"brief_id": entry["brief_id"], "kind": entry["kind"], "error": str(exc)[:160]})
    return handled


def _ask_price_next(p: dict[str, Path], estimate_id: str, message_id: str, runner: Any) -> dict[str, Any]:
    """The owner rejected a price card: ask, in words, what price to file (WORKFLOW.md 6.4).

    A card is approve or reject; the price the owner wants comes back as an
    answer and becomes a fresh card. Each rejection asks once more, so the
    question is numbered by round.
    """
    root = owner_questions.questions_root(p["monitor_root"])
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    if record.get("status") != "pending_approval":
        return {"outcome": "nothing_to_ask", "record_status": record.get("status")}
    customer = kolo_safe._sender_display(record["route"]["recipient"])
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their piece"
    price = float(record.get("proposed_price") or 0)
    answered = [q for q in owner_questions.list_questions(root)
                if q["kind"] == "price_next" and q["estimate_id"] == estimate_id and q["status"] != "open"]
    round_no = len(answered) + 1
    qid_message = message_id if round_no == 1 else f"{message_id}#round{round_no}"
    text = (
        f"You passed on the price for {customer} ({piece}): ${price:,.2f}; nothing was sent. "
        "Reply with a price to file, a changed fact to re-price (\"5 ct total\", \"18k rose gold\"), or \"handle myself\". "
        "Reply with the price you want and I will file a fresh card at it (same cost sheet, new margin shown), "
        "or \"handle myself\" and I will leave the thread to you."
    )
    created, question = owner_questions.create_decision(
        root, "price_next", estimate_id, qid_message, text,
        {"rejected_price": price, "source_message_id": message_id, "round": round_no},
    )
    question = _attach_answer_command(root, p["monitor_root"], question)
    if created:
        owner_questions.deliver(root, question, runner=runner, extra_args=kolo_safe.owner_channel_args(p["monitor_root"]))
    return {"outcome": "asked", "question_id": question["question_id"], "created": created}


NEXT_STEP_FILE = "next-step.json"


def _hand_to_tick(p: dict[str, Path], message_id: str, step: str | None = None, estimate_id: str | None = None) -> dict[str, Any]:
    """Leave the heavy work (a re-read, a price) to the next tick.

    `step` names a pipeline step the tick runs instead of a full read
    (`price_from_record`, `resend_followup`); the tick deletes the note once
    the step ran.

    An owner's answer runs inside the chat session's command, which the
    session may kill after a while (6 September 2026: a "change" answer
    died in the re-price and the claim sat leased for fifteen minutes). The
    answer records the decision and reopens the claim; the tick, with its
    own clock, does the reading and pricing within two minutes.
    """
    token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
    if step:
        paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
        write_private(Path(paths["work_dir"]) / NEXT_STEP_FILE, {"action": step, "estimate_id": estimate_id})
    inbox_claim.mark_inline(p["claim_root"], message_id, token, True)
    inbox_claim.release_lease(p["claim_root"], message_id, token)
    return {"pipeline": "queued_for_tick", "note": "the next tick " + ({"price_from_record": "prices it", "resend_followup": "asks the customer again",
                                                                          "price_and_render": "renders it and prices it; one card follows"}.get(step or "", "reads and prices it"))}


def _answer_same_piece(args: argparse.Namespace, workspace: Path, p: dict[str, Path], root: Path,
                       question: dict[str, Any]) -> dict[str, Any]:
    """The owner says a new thread is the same piece (WORKFLOW.md 6.1): the estimate carries on there.

    The record's route moves to the new thread (the old one kept as history)
    and the message is then read as a reply on that estimate: the gate, the
    price, or the post-estimate steps continue in the new thread. A record
    whose price card is pending cannot move (the binding holds the route);
    that one still hands over to the owner.
    """
    import cron_config  # local import keeps module import order unchanged
    import inbox_watcher  # local import: inbox_watcher imports this module
    import pipeline  # local import: pipeline imports this module

    message_id = _question_message_id(question)
    estimate_id = (question.get("context") or {}).get("existing_estimate_id") or question["estimate_id"]
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "same_sender", "decision": "same"}
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    if record.get("status") not in estimate_record.MOVABLE_STATUSES:
        _close_parked_claim(p, message_id, "owner_decided_same")
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, "same")
        result.update({"claim": "owner_decided_same", "note": f"the estimate is {record.get('status')}; its thread cannot move while a card is pending, so the owner takes this thread"})
        return result
    reopened = _resume_parked_claim(p, message_id)
    if not Path(reopened["work_paths"]["gmail_message"]).exists():
        import gmail_fetch  # local import; only needed when the work file was cleaned up

        gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token())
    new_route = read_object(Path(reopened["work_paths"]["route"]))
    estimate_record.move_route(p["record_root"], estimate_id, new_route, message_id)
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, "same")
    work_dir = Path(reopened["work_paths"]["work_dir"])
    (work_dir / "intake-result.json").unlink(missing_ok=True)
    intake_result = intake(argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        message_id=message_id, shop_profile=p["shop_profile"],
    ))
    write_private(work_dir / "intake-result.json", intake_result)
    result["intake"] = {k: intake_result.get(k) for k in ("decision", "estimate_id", "next_action", "outcome")}
    if intake_result.get("next_action") != "review_thread":
        return result
    result.update(_hand_to_tick(p, message_id))
    result["thread_id"] = new_route.get("thread_id")
    return result


def _answer_design_change(args: argparse.Namespace, workspace: Path, p: dict[str, Path], root: Path,
                          question: dict[str, Any], outcome: str = "design_change") -> dict[str, Any]:
    """The owner says the reply changes the design or adds a piece (WORKFLOW.md 6.8): reopen on this thread.

    The sent estimate becomes history on the record; the customer's message
    is read again as part of the inquiry, the gate asks for what the change
    leaves open or prices it, and a fresh card follows. Same thread, same
    record, the old figure never re-sent. A second piece is read into
    `pieces` beside the first (the multi-piece rule: one estimate, one total).
    """
    import cron_config  # local import keeps module import order unchanged
    import inbox_watcher  # local import: inbox_watcher imports this module
    import pipeline  # local import: pipeline imports this module

    message_id = _question_message_id(question)
    estimate_id = question["estimate_id"]
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "unclear_reply",
                              "decision": outcome}
    reopened = _resume_parked_claim(p, message_id)
    if not Path(reopened["work_paths"]["gmail_message"]).exists():
        import gmail_fetch  # local import; only needed when the work file was cleaned up

        gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token())
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    if record.get("status") in SENT_STATUSES:
        estimate_record.reopen_for_change(p["record_root"], estimate_id, message_id, outcome, args.answer)
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    work_dir = Path(reopened["work_paths"]["work_dir"])
    intake_path = work_dir / "intake-result.json"
    intake_result = read_object(intake_path) if intake_path.exists() else None
    if not intake_result or intake_result.get("estimate_id") != estimate_id:
        intake_result = intake(argparse.Namespace(
            monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
            message_id=message_id, shop_profile=p["shop_profile"],
        ))
        write_private(intake_path, intake_result)
    result.update(_hand_to_tick(p, message_id))
    result["revision"] = int(estimate_record.read_object(
        estimate_record.record_path(p["record_root"], estimate_id)).get("revision") or 0)
    return result


def _ask_rendering_next(p: dict[str, Path], estimate_id: str, message_id: str, runner: Any) -> dict[str, Any]:
    """The owner rejected a rendering card: ask, in words, what should change (WORKFLOW.md 6.6, 6.10).

    The claim stays parked behind the card; the answer re-renders the pieces
    the owner names and files a fresh card. Asked once per rejection.
    """
    root = owner_questions.questions_root(p["monitor_root"])
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    customer = kolo_safe._sender_display(record["route"]["recipient"])
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their piece"
    labels = [estimate_record.piece_label(record.get("specification") or {}, i)
              for i in range(len(estimate_record.pieces_of(record.get("specification") or {})))]
    answered = [q for q in owner_questions.list_questions(root)
                if q["kind"] == "rendering_next" and q["gmail_message_id"].split("#")[0] == message_id and q["status"] != "open"]
    round_no = len(answered) + 2
    qid_message = message_id if round_no == 2 else f"{message_id}#round{round_no}"
    which = f" Name the piece if not all of them ({', '.join(labels)})." if len(labels) > 1 else ""
    text = (
        f"You passed on the renderings for {customer} ({piece}); nothing was sent. "
        f"Tell me what should change and I will render new views for a fresh card.{which} "
        "Or say \"handle myself\" and I will hold the renderings and leave the thread to you."
    )
    created, question = owner_questions.create_decision(
        root, "rendering_next", estimate_id, qid_message, text,
        {"source_message_id": message_id, "round": round_no, "pieces": labels},
    )
    question = _attach_answer_command(root, p["monitor_root"], question)
    if created:
        owner_questions.deliver(root, question, runner=runner, extra_args=kolo_safe.owner_channel_args(p["monitor_root"]))
    return {"outcome": "asked", "question_id": question["question_id"], "created": created}


def _answer_rendering_next(args: argparse.Namespace, workspace: Path, p: dict[str, Path], root: Path,
                           question: dict[str, Any], outcome: str) -> dict[str, Any]:
    """Apply the owner's answer after a rejected rendering card: re-render, or the thread is theirs."""
    import cron_config  # local import keeps module import order unchanged
    import inbox_watcher  # local import: inbox_watcher imports this module
    import rendering  # local import: only needed to read the owner's words

    runner = getattr(args, "runner", subprocess.run)
    context = question.get("context") or {}
    message_id = _question_message_id(question)
    estimate_id = question["estimate_id"]
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "rendering_next", "decision": outcome}
    if outcome == "handle_myself":
        if _claim_parked(p, message_id):
            _close_parked_claim(p, message_id, "owner_rejected_rendering")
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result["note"] = "the renderings are held; the desk leaves this thread to the owner"
        return result
    note = " ".join(str(args.answer or "").split())[:400]
    labels = [str(l) for l in context.get("pieces") or []]
    named = rendering.pieces_named(note, labels) if len(labels) > 1 else []
    reopened = _resume_parked_claim(p, message_id)
    work_dir = Path(reopened["work_paths"]["work_dir"])
    write_private(work_dir / "rendering-change.json", {"note": note, "pieces": named, "round": int(context.get("round") or 2)})
    estimate_record.record_rendering_revision(p["record_root"], estimate_id, message_id, note, named or labels)
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    # The rendering runs where every rendering runs: in the watcher, one
    # view per tick. A fresh plan is made for the named pieces on the next
    # tick; the other pieces' views are kept from the last report.
    import pipeline  # local import: pipeline imports this module

    (work_dir / pipeline.PROGRESS_FILE).unlink(missing_ok=True)
    result.update(_hand_to_tick(p, message_id))
    result.update({"outcome": "re_render_started", "pieces": named or labels, "note": note})
    return result


def _refile_price_card(p: dict[str, Path], estimate_id: str, message_id: str, round_no: int,
                       runner: Any, args: argparse.Namespace) -> dict[str, Any]:
    """File a fresh price card from the record's bound state (the owner's price already on it)."""
    profile = read_object(p["shop_profile"])
    state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], message_id))
    for key, action in (state.get("external_actions") or {}).items():
        if key.startswith(f"approved_estimate:{estimate_id}:") and isinstance(action, dict) \
                and action.get("status") in {"sent", "pending", "uncertain"}:
            raise ValueError("an estimate send is journaled for this thread; run the doctor before filing a new price")
    current = estimate_record.prepare_approval_state(p["record_root"], estimate_id, message_id, {"estimate_id": estimate_id}, profile)
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    approval = approval_guard.build_request(current)
    approval["execute"] = execute_line(p["monitor_root"], "send-approved-estimate-brief", estimate_id=estimate_id, brief_id="<Brief ID>")
    approval["owner_price"] = record.get("owner_price")
    estimate_record.validate_approval_request(p["record_root"], estimate_id, message_id, approval)
    work_dir = estimate_work_dir(p["monitor_root"], estimate_id, message_id)
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Everything drafted or journaled at the old price is stale: the executor
    # must never reuse the old email or the old payload.
    for name in ("customer-reply.txt", "approved.json", "gmail-send.json", "current-record.json"):
        (work_dir / name).unlink(missing_ok=True)
    approval_path = work_dir / f"approval-request-r{round_no}.json"
    write_private(approval_path, approval)
    approver = activation_binding.load(activation_binding.binding_path(p["monitor_root"]))
    kolo_safe.request_approval_claimed(
        p["claim_root"], message_id, None, f"approval_request:{estimate_id}:{message_id}:r{round_no}",
        estimate_id, approval_path, approver["session_key"], runner=runner, allow_processed=True,
    )
    record = estimate_record.record_approval_requested(p["record_root"], estimate_id, message_id, approval)
    title = kolo_safe.approval_title(approval, estimate_id)
    _register_brief(p["monitor_root"], "price", title, estimate_id, message_id, runner)
    # No email draft here: this runs inside the chat session's command, and a
    # model call there is what the session kills. The executor drafts the
    # estimate email at send time when none was prepared.
    mirror_record(record, work_dir / "current-record.json")
    return {"outcome": "price_card_filed", "price": record["proposed_price"], "title": title[:120],
            "margin": (record.get("owner_price") or {}).get("margin")}


def _answer_price_next(args: argparse.Namespace, p: dict[str, Path], root: Path,
                       question: dict[str, Any], outcome: str) -> dict[str, Any]:
    """Apply the owner's answer after a rejected price card: a price, or the thread is theirs."""
    runner = getattr(args, "runner", subprocess.run)
    estimate_id = question["estimate_id"]
    context = question.get("context") or {}
    message_id = _question_message_id(question)
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "price_next", "decision": outcome}
    if outcome == "handle_myself":
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
        if record.get("status") == "pending_approval":
            estimate_record.retire(p["record_root"], estimate_id, "owner_handles_thread",
                                   f"owner took the thread after passing on ${float(context.get('rejected_price') or 0):,.2f}")
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result["note"] = "the desk leaves this thread to the owner; the estimate is dormant"
        return result
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    changes = estimate_record.owner_facts_in_words(args.answer, record.get("specification") or {})
    if changes and "$" not in str(args.answer):
        # The owner changed a fact ("5 ct total", "18k rose gold"): the owner's word goes on the record and the
        # ledger, and the tick re-prices from the record; nothing stands down (9 September 2026).
        import ledger  # local import: the ledger never imports this module

        estimate_record.owner_changes_specification(p["record_root"], estimate_id, changes, question["question_id"])
        try:
            ledger.add_facts(workspace_of(p["monitor_root"]) / "estimate-desk", estimate_id, [
                {"field": k, "piece": None, "stone": ledger.stone_of(k), "value": v, "source": "owner", "gmail_message_id": message_id,
                 "span": str(args.answer)[:120]} for k, v in changes.items()])
        except Exception:  # noqa: BLE001 - the record carries the change; the ledger catches up on the re-read
            pass
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, "spec_change")
        # The claim finished with the first card; it is reopened on purpose and the next tick re-prices the record,
        # where the owner's facts now stand. A model call never runs in this session's command.
        inbox_monitor.reopen_item(p["monitor_root"], message_id, p["claim_root"], 1, allow_processed=True)
        result.update({"decision": "spec_change", "changes": changes, **_hand_to_tick(p, message_id, "price_from_record", estimate_id)})
        return result
    price = owner_questions.parse_owner_price(args.answer)
    if price is None:
        raise ValueError("could not read a price from that reply; give a dollar figure, for example 2,300, or a changed fact such as 5 ct total")
    estimate_record.record_owner_price(p["record_root"], estimate_id, price, question["question_id"], read_object(p["shop_profile"]))
    filed = _refile_price_card(p, estimate_id, message_id, int(context.get("round") or 1) + 0, runner, args)
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    result.update(filed)
    return result


def _question_message_id(question: dict[str, Any]) -> str:
    """The Gmail message a question is about; a repeat question carries a round suffix the claim does not."""
    context = question.get("context") or {}
    return str(context.get("source_message_id") or str(question.get("gmail_message_id") or "").split("#")[0])


def _attach_answer_command(root: Path, monitor_root: Path, question: dict[str, Any]) -> dict[str, Any]:
    if not question.get("answer_command"):
        question["answer_command"] = owner_questions.answer_command(
            skill_dir(), workspace_of(monitor_root), question["question_id"]
        )
        owner_questions.save(root, question)
    return question


def finish_processed(
    monitor_root: Path,
    claim_root: Path,
    record_root: Path,
    message_id: str,
) -> None:
    token = inbox_claim.authoritative_claim_token(claim_root, message_id)
    inbox_claim.advance_phase(claim_root, message_id, token, "work_persisted")
    inbox_claim.advance_phase(claim_root, message_id, token, "ready_to_finalize")
    inbox_monitor.finalize_item(
        monitor_root,
        message_id,
        claim_root,
        token,
        "processed",
        record_root=record_root,
    )


def send_spec_followup(args: argparse.Namespace) -> dict[str, Any]:
    route = read_object(args.route)
    body = args.body.read_text(encoding="utf-8")
    # The payload built by an earlier run is reused: same Message-ID, same
    # journal binding, so a retry after a crash verifies instead of refusing.
    _reuse_or_build_payload(args.gmail_payload, lambda: gmail_reply.build_reply(route, body))
    receipt = gmail_safe.send_reply_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"customer_reply:{args.estimate_id}:{args.message_id}",
        args.gmail_payload,
        args.provider_response,
        gateway_token.load_token(),
    )
    if args.initiating:
        record = estimate_record.record_spec_gate_sent(
            args.record_root, args.estimate_id, body, receipt
        )
    else:
        record = estimate_record.record_followup_sent(
            args.record_root, args.estimate_id, args.message_id, body, receipt
        )
    mirror_record(record, args.record_output)
    finish_processed(
        args.monitor_root, args.claim_root, args.record_root, args.message_id
    )
    return record


def _revision_suffix(record_root: Path, estimate_id: str) -> str:
    """A re-priced record (the owner changed a fact, or a design change) journals its card under its revision."""
    try:
        revision = int(estimate_record.read_object(estimate_record.record_path(record_root, estimate_id)).get("revision") or 0)
    except (OSError, ValueError):
        revision = 0
    return f":rev{revision}" if revision else ""


def request_approval(args: argparse.Namespace) -> dict[str, Any]:
    candidate = read_object(args.current_state)
    current = estimate_record.prepare_approval_state(
        args.record_root,
        args.estimate_id,
        args.message_id,
        candidate,
        read_object(args.shop_profile),
    )
    approval_existed = args.approval_request.exists()
    if approval_existed:
        approval = read_object(args.approval_request)
    else:
        approval = approval_guard.build_request(current)
        approval["execute"] = execute_line(
            args.monitor_root, "send-approved-estimate-brief",
            estimate_id=args.estimate_id, brief_id="<Brief ID>",
        )
        renders = getattr(args, "renderings", None) or []
        if renders:
            # Concierge mode: the views the owner just saw go out with the estimate. They are copied beside the
            # estimate's email (the claim's folder is cleaned when the claim closes) and named on the card.
            work_dir = estimate_work_dir(args.monitor_root, args.estimate_id, args.message_id)
            work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            kept = []
            for item in renders:
                source = Path(str(item["path"]))
                target = work_dir / f"rendering-{int(item['slot'])}.png"
                target.write_bytes(source.read_bytes())
                kept.append({"slot": int(item["slot"]), "sha256": _sha256_file(target), "checker": str(item.get("checker") or "")[:120]})
            approval["renderings"] = kept
            estimate_record.mark_concierge(args.record_root, args.estimate_id, renderings=kept)
        estimate_record.validate_approval_request(
            args.record_root, args.estimate_id, args.message_id, approval
        )
        write_private(args.approval_request, approval)
    if approval_existed:
        estimate_record.validate_approval_request(
            args.record_root, args.estimate_id, args.message_id, approval
        )
    approver = activation_binding.load(
        activation_binding.binding_path(args.monitor_root)
    )
    kolo_safe.request_approval_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"approval_request:{args.estimate_id}:{args.message_id}" + _revision_suffix(args.record_root, args.estimate_id),
        args.estimate_id,
        args.approval_request,
        approver["session_key"],
    )
    record = estimate_record.record_approval_requested(
        args.record_root, args.estimate_id, args.message_id, approval
    )
    _register_brief(args.monitor_root, "price", kolo_safe.approval_title(approval, args.estimate_id),
                    args.estimate_id, args.message_id, getattr(args, "runner", None))
    # Draft the estimate email now, while the thread is at hand and the tick
    # has time; the approval executor then only sends.
    try:
        profile = read_object(args.shop_profile)
        paths = inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)
        facts, fixed = estimate_email_facts(record, profile)
        _prepare_email(
            {"monitor_root": args.monitor_root, "shop_profile": args.shop_profile}, record, args.message_id, "estimate",
            facts, fixed, estimate_work_dir(args.monitor_root, args.estimate_id, args.message_id) / "customer-reply.txt",
            _digest_from_work(paths, args.message_id, profile), getattr(args, "judge_runner", subprocess.run),
        )
    except Exception:  # noqa: BLE001 - the executor drafts if this did not happen
        pass
    mirror_record(record, args.record_output)
    finish_processed(
        args.monitor_root, args.claim_root, args.record_root, args.message_id
    )
    return record


def _appointment_approval_details(
    record: dict[str, Any],
    message_id: str,
    intent: dict[str, Any],
    monitor_root: Path | None = None,
) -> dict[str, Any]:
    if not {"requested_times", "calendar_availability"} <= set(intent) or not set(intent) <= {
        "requested_times", "resolved_times", "calendar_availability", "availability_note", "mode", "outside_hours", "hours", "ask_for",
        "ask_intro",
    }:
        raise ValueError("appointment intent contains missing or unsupported fields")
    if intent.get("ask_intro") is not None and (not isinstance(intent["ask_intro"], str) or not intent["ask_intro"].strip()
                                                 or len(intent["ask_intro"]) > 200):
        raise ValueError("ask_intro must be one short sentence")
    ask_for = intent.get("ask_for") or []
    if not isinstance(ask_for, list) or len(ask_for) > 8 or any(
        not isinstance(q, str) or not q.strip() or len(q) > 200 or any(c in q for c in "\r\n") for q in ask_for
    ):
        raise ValueError("ask_for must contain at most eight short questions")
    resolved_times = intent.get("resolved_times", [])
    if not isinstance(resolved_times, list) or len(resolved_times) > 3 or any(
        not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value) for value in resolved_times
    ):
        raise ValueError("resolved_times must contain at most three YYYY-MM-DDTHH:MM strings")
    mode = intent.get("mode") or "offer"
    if mode not in {"book", "offer"}:
        raise ValueError("appointment intent mode must be book or offer")
    requested_times = intent["requested_times"]
    if (
        not isinstance(requested_times, list)
        or len(requested_times) > 5
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 160
            or any(character in value for character in "\r\n")
            for value in requested_times
        )
    ):
        raise ValueError("requested_times must contain at most five short strings")
    availability = intent["calendar_availability"]
    if not isinstance(availability, list) or len(availability) > 5:
        raise ValueError("calendar_availability must contain at most five slots")
    normalized_slots = []
    for index, slot in enumerate(availability):
        if not isinstance(slot, dict) or set(slot) != {"start", "end", "label"}:
            raise ValueError(
                f"calendar_availability[{index}] must contain start, end, and label"
            )
        if any(
            not isinstance(slot[field], str)
            or not slot[field]
            or len(slot[field]) > 160
            or any(character in slot[field] for character in "\r\n")
            for field in ("start", "end", "label")
        ):
            raise ValueError(f"calendar_availability[{index}] contains invalid text")
        normalized_slots.append(dict(slot))
    route_ownership.validate_record(record)
    if record["status"] not in {"estimate_sent", "appointment_booked", "approved", "awaiting_specs"}:
        raise ValueError("appointment approval requires an open estimate")
    route = record["route"]
    if mode == "book" and len(normalized_slots) != 1:
        raise ValueError("a booking card carries exactly one time")
    if mode == "offer" and not normalized_slots:
        mode = "none"  # nothing free to offer: the card only asks the owner
    details = {
        "schema_version": 1,
        "action_type": "appointment_booking" if mode == "book" else "appointment_offer",
        "estimate_id": record["estimate_id"],
        "source_message_id": message_id,
        "customer_email": route["recipient"],
        "thread_id": route["thread_id"],
        "requested_times": [value.strip() for value in requested_times],
        "calendar_availability": normalized_slots,
        "piece": "a visit to see ready-made pieces" if record.get("inventory_inquiry")
                 else owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their estimate",
    }
    if normalized_slots:
        details["proposed_time"] = dict(normalized_slots[0])
    if ask_for:
        details["ask_for"] = [q.strip() for q in ask_for]  # the questions the approved email also asks
        if intent.get("ask_intro"):
            details["ask_intro"] = str(intent["ask_intro"]).strip()  # concierge: budget and timeframe, introduced as such
    if monitor_root is not None:
        common = {"estimate_id": record["estimate_id"], "message_id": message_id, "brief_id": "<Brief ID>"}
        if mode == "book":
            details["execute"] = execute_line(monitor_root, "book-approved-appointment", **common, option="1")
        elif mode == "offer":
            details["execute"] = execute_line(monitor_root, "send-approved-times", **common)
        details["execute_on_reject"] = execute_line(monitor_root, "appointment-rejected", **common)
    outside = intent.get("outside_hours")
    if isinstance(outside, list) and outside and all(isinstance(o, str) and o.strip() for o in outside):
        details["outside_hours"] = [str(o).strip()[:80] for o in outside[:3]]
        details["hours"] = str(intent.get("hours") or "")[:160]
    note = intent.get("availability_note")
    if isinstance(note, str) and note.strip():
        details["availability_note"] = note.strip()[:160]
    return details


def request_appointment_approval(args: argparse.Namespace) -> dict[str, Any]:
    """Create a durable appointment approval and optionally finalize the claim."""
    record = estimate_record.read_object(estimate_record.record_path(args.record_root, args.estimate_id))
    if record.get("status") in SENT_STATUSES:
        record, _decision = estimate_record.post_estimate_decision(
            args.record_root,
            args.estimate_id,
            args.message_id,
            "appointment_request",
        )
    else:
        # Meeting first (WORKFLOW.md): a customer who asks to come in gets
        # the meeting; the design details are settled there or by email later.
        route_ownership.validate_record(record)
    intent = read_object(args.appointment_intent)
    if args.appointment_approval.exists():
        approval = read_object(args.appointment_approval)
        expected = _appointment_approval_details(record, args.message_id, intent, args.monitor_root)
        # The reject code is added after the first write; it is not binding.
        if {k: v for k, v in approval.items() if k != "reject_code"} != expected:
            raise ValueError("existing appointment approval binding changed")
    else:
        approval = _appointment_approval_details(record, args.message_id, intent, args.monitor_root)
        write_private(args.appointment_approval, approval)
    # WORKFLOW.md 6.6: only times inside the owner's declared windows may be
    # offered or booked, whoever wrote the intent (a worker agent once put a
    # Sunday on a card).
    scheduling = (_profile_for(args).get("scheduling") or {})
    outside = slots.outside_windows(scheduling, approval.get("calendar_availability") or [])
    if outside:
        raise ValueError("times outside the declared consultation windows: " + "; ".join(outside)[:200])
    # The claim's work directory is cleaned once the claim closes; the executor
    # needs the options the owner saw, so keep a durable private copy.
    store = approval_store_path(args.monitor_root, args.estimate_id, args.message_id)
    write_private(store, approval)
    try:
        profile = read_object(args.shop_profile) if getattr(args, "shop_profile", None) else {}
        shop = (profile.get("shop") or {}).get("name") or "the shop"
        piece = approval.get("piece") or "your piece"
        options = approval.get("calendar_availability") or []
        paths = inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)
        digest = _digest_from_work(paths, args.message_id, profile)
        before = {"the visit": "the visit is to design your perfect piece together (write to the customer as you); "
                               "never mention an estimate or a quote, and do not ask for design details now"} \
            if record.get("status") == "awaiting_specs" else {}
        before.update(_inventory_fact(record))
        if approval.get("action_type") == "appointment_booking" and options:
            when = options[0]["label"]
            _prepare_email({"monitor_root": args.monitor_root, "shop_profile": args.shop_profile}, record, args.message_id,
                           "confirmation", {"piece": piece, "time_labels": [when], "shop name": shop, **before},
                           CONFIRMATION_NOTE.format(when=when, shop=shop, piece=piece), prepared_email_path(store), digest,
                           getattr(args, "runner", subprocess.run))
        elif options:
            labels = [o.get("label") or o["start"] for o in options]
            facts, fixed = _offer_facts(approval, piece, labels, shop, estimate_record.vision_in_words(
                record.get("specification"), on_file=estimate_record.prior_basis(record)))
            _prepare_email({"monitor_root": args.monitor_root, "shop_profile": args.shop_profile}, record, args.message_id,
                           "offer", {**facts, **before}, fixed, prepared_email_path(store), digest,
                           getattr(args, "runner", subprocess.run))
    except Exception:  # noqa: BLE001 - the executor drafts if this did not happen
        pass
    # The reject row names a code; a reply with that code and a plan reaches
    # this question. Kolo delivers approvals to the session but not rejections.
    qroot = owner_questions.questions_root(args.monitor_root)
    if not options:
        # Nothing to approve: the calendar gave no free time (or could not be
        # read). A card with no times would only be rejected, so ask now.
        reason = str(approval.get("availability_note") or "no free time inside the declared windows")[:160]
        _created, question = owner_questions.create_decision(
            qroot, "appointment_next", args.estimate_id, args.message_id,
            _appointment_ask_text(record, approval, reason),
            {"rejected_action": approval.get("action_type"), "rejected_options": [], "reason": reason},
            dormant=False,
        )
        question = _attach_answer_command(qroot, args.monitor_root, question)
        if _created:
            owner_questions.deliver(qroot, question, runner=getattr(args, "runner", subprocess.run),
                                    extra_args=kolo_safe.owner_channel_args(args.monitor_root))
        # No card was filed, so nothing to record as requested; the review is
        # already on the record. The claim waits for the owner's answer the
        # way a missing rate does, and the answer closes it.
        mirror_record(record, args.record_output)
        if not args.defer_finalize_for_rendering:
            token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
            inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "appointment_next_question")
        return record
    _created, dormant = owner_questions.create_decision(
        qroot, "appointment_next", args.estimate_id, args.message_id,
        _appointment_next_text(record, approval),
        {"rejected_action": approval.get("action_type"), "rejected_options": approval.get("calendar_availability") or []},
        dormant=True,
    )
    _attach_answer_command(qroot, args.monitor_root, dormant)
    if approval.get("reject_code") != owner_questions.reference(dormant["question_id"]):
        approval["reject_code"] = owner_questions.reference(dormant["question_id"])
        write_private(args.appointment_approval, approval)
        write_private(approval_store_path(args.monitor_root, args.estimate_id, args.message_id), approval)
    approver = activation_binding.load(
        activation_binding.binding_path(args.monitor_root)
    )
    kolo_safe.request_appointment_approval_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"appointment_approval:{args.estimate_id}:{args.message_id}",
        args.estimate_id,
        args.appointment_approval,
        approver["session_key"],
    )
    record = estimate_record.record_appointment_approval_requested(
        args.record_root, args.estimate_id, args.message_id, approval
    )
    _rows, _reasoning, title = kolo_safe.appointment_card(approval, args.estimate_id)
    _register_brief(args.monitor_root, "appointment", title, args.estimate_id, args.message_id, getattr(args, "runner", None))
    mirror_record(record, args.record_output)
    if not args.defer_finalize_for_rendering:
        finish_processed(
            args.monitor_root, args.claim_root, args.record_root, args.message_id
        )
    return record


def send_approved_estimate(args: argparse.Namespace) -> dict[str, Any]:
    current = (
        read_object(args.current_state)
        if args.current_state is not None
        else estimate_record.current_approval_state(args.record_root, args.estimate_id)
    )
    approved = read_object(args.approved)
    message_id = args.message_id or estimate_record.approval_source_message_id(
        args.record_root, args.estimate_id
    )
    valid, errors = approval_guard.verify_execution(approved, current)
    if not valid:
        raise ValueError("approval verification failed: " + "; ".join(errors))
    route = current.get("route")
    if not isinstance(route, dict):
        raise ValueError("current state route must be an object")
    body = args.body.read_text(encoding="utf-8")
    customer_content_guard.validate_approved_price(
        body, approved["owner_approved_price"]
    )
    images = [Path(str(i)) for i in (getattr(args, "images", None) or [])]
    payload = _reuse_or_build_payload(args.gmail_payload, lambda: gmail_reply.build_reply(route, body, images or None))
    receipt = gmail_safe.send_reply_claimed(
        args.claim_root,
        message_id,
        inbox_claim.authoritative_claim_token(
            args.claim_root, message_id, allow_processed=True
        ),
        f"approved_estimate:{args.estimate_id}:{message_id}",
        args.gmail_payload,
        args.provider_response,
        gateway_token.load_token(),
        allow_processed_claim=True,
    )
    record = estimate_record.record_estimate_sent(
        args.record_root,
        args.estimate_id,
        message_id,
        approved,
        current,
        receipt,
    )
    mirror_record(record, args.record_output)
    return record


RENDERING_GATE = (
    "renderings are approval-gated at every stage: file the card with request-rendering-approval; "
    "the owner's approval runs send-approved-rendering"
)


def send_rendering(args: argparse.Namespace) -> dict[str, Any]:
    """Deliver approved renderings. Only send_approved_rendering may call this.

    WORKFLOW.md 6.6: nothing customer-facing without a card. The approval the
    owner saw is required here, and the images must be the ones on it. A
    rendering sent by a worker agent without a card (4 September 2026, a
    tennis bracelet) is what this gate prevents.
    """
    approval = getattr(args, "approved_rendering", None)
    if not isinstance(approval, dict):
        raise ValueError(RENDERING_GATE)
    if approval.get("estimate_id") != args.estimate_id or approval.get("gmail_message_id") != args.message_id:
        raise ValueError("rendering approval does not match this estimate and message")
    expected = {item["slot"]: item["sha256"] for item in approval.get("images", [])}
    images = list(args.images)
    if len(images) != len(expected) or any(_sha256_file(img) != expected.get(i) for i, img in enumerate(images, start=1)):
        raise ValueError("rendering images changed since the owner approved them")
    record, _decision = estimate_record.post_estimate_decision(
        args.record_root,
        args.estimate_id,
        args.message_id,
        "rendering_request",
    )
    route_ownership.validate_record(record)
    if record["status"] not in {"estimate_sent", "appointment_booked", "approved"}:
        raise ValueError("rendering delivery requires a sent estimate")
    route = record.get("route")
    if not isinstance(route, dict):
        raise ValueError("estimate record route must be an object")
    body = args.body.read_text(encoding="utf-8")
    customer_content_guard.validate_customer_text(body)
    payload = _reuse_or_build_payload(args.gmail_payload, lambda: gmail_reply.build_reply(route, body, args.images))
    receipt = gmail_safe.send_reply_claimed(
        args.claim_root,
        args.message_id,
        None,
        f"customer_rendering:{args.estimate_id}:{args.message_id}",
        args.gmail_payload,
        args.provider_response,
        gateway_token.load_token(),
    )
    record = estimate_record.record_rendering_sent(
        args.record_root,
        args.estimate_id,
        args.message_id,
        body,
        args.images,
        receipt,
    )
    mirror_record(record, args.record_output)
    finish_processed(
        args.monitor_root, args.claim_root, args.record_root, args.message_id
    )
    return record


NOT_CUSTOMER_MAIL = {
    "auto_reply",
    "calendar_event",
    "automated_notification",
    "bulk_mail",
    "internal_sender",
}
NOT_AN_INQUIRY_REASONS = {
    "not_a_quote_request",
    "vendor_or_marketing",
    "personal_or_internal",
    "unrelated",
}


def not_an_inquiry(args: argparse.Namespace) -> dict[str, Any]:
    """Close a claimed message that turned out not to be a quote request.

    The header classifier cannot read; it only removes machine mail. A human
    can still write to the shop about anything, and intake will have opened a
    record for it before anyone read it. Once the thread review shows the
    message asks for no estimate, this retires that record, mirrors it, and
    finalizes the claim, so the record never shadows later mail from the same
    sender. It refuses anything that has moved past the initial record.
    """
    if args.reason not in NOT_AN_INQUIRY_REASONS:
        raise ValueError("reason must be one of: " + ", ".join(sorted(NOT_AN_INQUIRY_REASONS)))
    record = read_object(estimate_record.record_path(args.record_root, args.estimate_id))
    route = record.get("route") or {}
    if route.get("gmail_message_id") != args.message_id:
        raise ValueError(
            "only the message that opened the record can close it as not an inquiry"
        )
    if record.get("status") != "awaiting_specs":
        raise ValueError(
            f"record is '{record.get('status')}', not a fresh awaiting_specs record"
        )
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    retired = estimate_record.retire(
        args.record_root, args.estimate_id, "not_an_inquiry", f"triage: {args.reason}"
    )
    mirror_record(retired, args.record_output)
    kolo_safe.complete_claimed(args.monitor_root, args.claim_root, args.message_id, token)
    return {
        "message_id": args.message_id,
        "estimate_id": args.estimate_id,
        "status": retired["status"],
        "reason": args.reason,
        "outcome": "not_an_inquiry_completed",
        "next_action": "done",
    }


def resolve_review_approval(args: argparse.Namespace) -> dict[str, Any]:
    """Close a review the owner approved in the approval queue, then report it.

    This is the only thing the chat session may do with an approved
    manual-review brief: one command, no reading of customer mail, and the
    brief is marked executed so the queue reflects reality.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", args.review_key or ""):
        raise ValueError("review_key must be a lowercase SHA-256 value")
    open_keys = {r["review_key"] for r in inbox_monitor.list_manual_reviews(args.monitor_root)}
    item = None
    if args.review_key in open_keys:
        item = inbox_monitor.resolve_manual_review(args.monitor_root, args.review_key)
        outcome = "resolved"
    else:
        outcome = "already_resolved"
    result = {"action_type": "manual_review", "review_key": args.review_key, "outcome": outcome}
    _report_brief(args, result, getattr(args, "runner", subprocess.run), repeat=outcome == "already_resolved")
    return {**result, "brief_id": args.brief_id, "review_status": (item or {}).get("review_status", "resolved")}


def worker_start(args: argparse.Namespace) -> dict[str, Any]:
    """Hand a worker job the intake result for the one claim leased to it.

    A worker is told which message it owns and nothing else. This is the only
    thing it may run first: it proves the claim is still processing and still
    leased, then returns the intake result the watcher wrote, with the exact
    work paths, so the worker never chooses paths or repeats intake.
    """
    state = inbox_claim.read_state(inbox_claim.claim_path(args.claim_root, args.message_id))
    if state.get("status") != "processing":
        raise ValueError(f"claim is {state.get('status')}, not processing; nothing to do")
    if not inbox_claim.recovery_lease_active(state):
        raise ValueError("claim lease has expired; the watcher will recover it")
    paths = inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)
    result = read_object(Path(paths["work_dir"]) / "intake-result.json")
    if result.get("message_id") != args.message_id or result.get("next_action") != "review_thread":
        raise ValueError("intake result does not describe a delegated review for this message")
    # The thread as plain text, so the worker never opens the Gmail JSON.
    try:
        thread = read_object(Path(paths["gmail_thread"]))
        profile = read_object(args.monitor_root.resolve().parent / "shop-profile.json")
        mailbox = (profile.get("shop") or {}).get("outbound_mailbox")
        result["thread"] = gmail_text.thread_digest(thread, args.message_id, mailbox)
    except (OSError, ValueError, json.JSONDecodeError):
        result["thread"] = None
    # Dead-spot guard. A previous worker may have reviewed the thread and then
    # died before, or just after, sending the specification follow-up. Tell
    # this worker exactly where to resume, or finish the claim when the send
    # already happened, so the customer is neither left unasked nor asked twice.
    record_root = getattr(args, "record_root", None) or args.monitor_root.resolve().parent / "records"
    estimate_id = result.get("estimate_id")
    record_file = estimate_record.record_path(record_root, estimate_id) if estimate_id else None
    if record_file is not None and record_file.exists():
        record = estimate_record.read_object(record_file)
        pending = estimate_record.pending_followup(record, args.message_id)
        if pending is not None:
            result["resume"] = pending
        elif record.get("status") == "awaiting_specs" and estimate_record.followup_sent(
            record, args.message_id
        ):
            review = next(
                (
                    item
                    for item in record.get("thread_reviews", [])
                    if isinstance(item, dict)
                    and item.get("source_message_id_sha256")
                    == estimate_record.sha256_text(args.message_id)
                ),
                None,
            )
            if review is not None and review.get("outcome") == "awaiting_specs":
                finish_processed(args.monitor_root, args.claim_root, record_root, args.message_id)
                result["outcome"] = "followup_already_sent"
                result["next_action"] = "done"
    return result


def intake(args: argparse.Namespace) -> dict[str, Any]:
    """Classify, route, decide ownership, and record one claimed message.

    These eight steps always run in the same order and never involve a
    judgment call, yet each one used to cost the run a model turn. Bundling
    them removes those turns. Every step is idempotent, so a resumed claim
    can run this again safely: the route is rebuilt, phases only advance,
    the initial record is retry-stable, and the owner alert deduplicates.
    """
    profile = read_object(args.shop_profile)
    mailbox = (profile.get("shop") or {}).get("outbound_mailbox")
    if not isinstance(mailbox, str) or not mailbox.strip():
        raise ValueError("shop profile is missing shop.outbound_mailbox")
    paths = inbox_monitor.prepare_claim_work(
        args.monitor_root, args.claim_root, args.message_id
    )
    message = read_object(Path(paths["gmail_message"]))
    thread = read_object(Path(paths["gmail_thread"]))
    if message.get("id") != args.message_id:
        raise ValueError("fetched Gmail message does not match the claimed message")
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    classification = gmail_classify.classify(message, mailbox)
    result: dict[str, Any] = {
        "message_id": args.message_id,
        "classification": classification["classification"],
        "classification_reason": classification["reason_code"],
        "work_paths": paths,
    }
    if classification["classification"] in NOT_CUSTOMER_MAIL:
        # Nothing a customer wrote is in here: an auto-reply, a calendar
        # invitation, a machine notification, a newsletter, or a coworker.
        # It is closed without a record, an alert, or a reply, so it can never
        # become a phantom estimate that later mail gets matched against.
        kolo_safe.complete_claimed(args.monitor_root, args.claim_root, args.message_id, token)
        result.update(
            {"outcome": f"{classification['classification']}_completed", "next_action": "done"}
        )
        return result
    if classification["classification"] != "customer_or_uncertain":
        reason = "uncorrelated_dsn" if classification["classification"] == "dsn_candidate" else "uncertain_classification"
        kolo_safe.manual_review_claimed(
            args.monitor_root, args.claim_root, args.message_id, token, reason
        )
        result.update({"outcome": "manual_review", "reason_code": reason, "next_action": "done"})
        return result

    route = gmail_route.build_route(message, mailbox)
    write_private(Path(paths["route"]), route)
    inbox_claim.advance_phase(args.claim_root, args.message_id, token, "routed")
    candidates = estimate_record.lookup_thread(args.record_root, route)
    write_private(Path(paths["candidate_records"]), candidates)
    messages = thread.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("fetched Gmail thread has no messages")
    shop_seen = any(gmail_text._address(gmail_text.header(m, "From")) == mailbox.strip().lower() for m in messages if isinstance(m, dict))
    decision = route_ownership.decide(route, candidates, args.claim_root, len(messages), shop_seen=shop_seen)
    result.update({
        "decision": decision["decision"],
        "reason_code": decision.get("reason_code"),
        "thread_message_count": len(messages),
    })
    if (
        decision["decision"] == "manual_review"
        and decision.get("reason_code") == "identity_has_active_estimate_on_another_thread"
        and not getattr(args, "force_new_inquiry", False)
        and len(messages) == 1
        and (estimate_record.refers_to_a_prior_piece(gmail_text.body_text(message, limit=4000))
             or estimate_record.refers_to_an_earlier_conversation(gmail_text.body_text(message, limit=4000)))
    ):
        # "The pendant you made for me, but smaller": a new piece after one on file, not the same piece continued
        # (the jeweler, 9 September 2026); the owner is not asked which.
        args.force_new_inquiry = True
    if (
        decision["decision"] == "manual_review"
        and decision.get("reason_code") == "identity_has_active_estimate_on_another_thread"
        and getattr(args, "force_new_inquiry", False)
    ):
        # The owner answered "new piece": treat this thread as a fresh inquiry.
        decision = {"decision": "new_inquiry", "reason_code": "owner_said_new_piece"}
        result["decision"] = "new_inquiry"
        result["reason_code"] = "owner_said_new_piece"
    if (
        decision["decision"] == "manual_review"
        and decision.get("reason_code") == "identity_has_active_estimate_on_another_thread"
    ):
        asked = ask_same_sender(args, token, route, decision.get("estimate_id"), message)
        result.update(asked)
        return result
    if decision["decision"] == "manual_review" and decision.get("reason_code") == "missing_thread_ownership":
        # A reply in a conversation the desk never started is not the desk's
        # business: close it quietly, no review, no notice.
        kolo_safe.complete_claimed(args.monitor_root, args.claim_root, args.message_id, token)
        result.update({"outcome": "not_desk_thread", "next_action": "done"})
        return result
    if decision["decision"] in {"manual_review", "owned_manual_review"}:
        kolo_safe.manual_review_claimed(
            args.monitor_root, args.claim_root, args.message_id, token, decision["reason_code"]
        )
        result.update({"outcome": "manual_review", "estimate_id": decision.get("estimate_id"), "next_action": "done"})
        return result
    if decision["decision"] == "new_inquiry":
        internal_date = message.get("internalDate")
        try:
            inbound_ms = int(internal_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("fetched Gmail message lacks a numeric internalDate") from exc
        record = estimate_record.create_initial_record(args.record_root, route, inbound_ms)
        mirror_record(record, Path(paths["inquiry_record"]))
    elif decision["decision"] == "owned":
        record = read_object(
            estimate_record.record_path(args.record_root, decision["estimate_id"])
        )
    else:
        kolo_safe.manual_review_claimed(
            args.monitor_root, args.claim_root, args.message_id, token, "missing_thread_ownership"
        )
        result.update({"outcome": "manual_review", "reason_code": "missing_thread_ownership", "next_action": "done"})
        return result
    inbox_claim.advance_phase(args.claim_root, args.message_id, token, "ownership_confirmed")
    estimate_id = record["estimate_id"]
    # No "customer replied" ping: the owner hears from the desk only when a
    # decision is needed or something final happened (WORKFLOW.md 6.10).
    result.update({
        "outcome": "ownership_confirmed",
        "estimate_id": estimate_id,
        "record_status": record["status"],
        "next_action": "review_thread",
    })
    return result


SENT_STATUSES = {"estimate_sent", "appointment_booked", "approved"}


def _work_paths(args: argparse.Namespace) -> dict[str, str]:
    return inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)


def review_thread(args: argparse.Namespace) -> dict[str, Any]:
    """Record the worker's review and run every deterministic step after it.

    The worker supplies only its judgment: before an estimate, the merged
    specification and the missing required fields; after one, the
    post-estimate artifact. Everything else that used to cost a model round
    trip happens here: the thread ids come from the fetched thread, the
    review is persisted, and then either the follow-up is prepared, the
    post-estimate decision is finalized, the missing rate is asked, or the
    cost skeleton is built (including the spot price) ready for `price`.
    """
    review = read_object(args.review)
    paths = _work_paths(args)
    thread = read_object(Path(paths["gmail_thread"]))
    digest = gmail_text.thread_digest(thread, args.message_id)
    profile = read_object(args.shop_profile)
    record = estimate_record.read_object(
        estimate_record.record_path(args.record_root, args.estimate_id)
    )
    post_estimate = record.get("status") in SENT_STATUSES
    snapshot: dict[str, Any] = {
        "thread_id": digest["thread_id"],
        "source_message_id": args.message_id,
        "message_ids": digest["message_ids"],
        "missing_required_fields": [],
    }
    if post_estimate:
        if "post_estimate_artifact" not in review:
            raise ValueError("a post-estimate review needs post_estimate_artifact")
        snapshot["post_estimate_artifact"] = review["post_estimate_artifact"]
    else:
        if "specification" not in review or "missing_required_fields" not in review:
            raise ValueError("a pre-estimate review needs specification and missing_required_fields")
        snapshot["specification"] = review["specification"]
        snapshot["missing_required_fields"] = review["missing_required_fields"]
    record = estimate_record.record_thread_review(
        args.record_root, args.estimate_id, snapshot, profile
    )
    write_private(Path(paths["current_record"]), record)
    if getattr(args, "quiet", False):
        # Concierge mode before the owner's details: the review is on the record, nothing else runs yet.
        return {"outcome": "reviewed", "next": "done", "missing_required_fields": list(snapshot["missing_required_fields"])}
    if post_estimate:
        decision = finalize_post_estimate(
            argparse.Namespace(
                monitor_root=args.monitor_root,
                claim_root=args.claim_root,
                record_root=args.record_root,
                message_id=args.message_id,
                estimate_id=args.estimate_id,
                record_output=Path(paths["current_record"]),
                runner=getattr(args, "runner", subprocess.run),
            )
        )
        return {"outcome": "post_estimate_reviewed", **decision, "next": decision["next_action"]}

    pending = estimate_record.pending_followup(record, args.message_id)
    if pending is not None:
        return {
            "outcome": "specification_incomplete",
            "next": "send_spec_followup",
            "missing_required_fields": pending["missing_required_fields"],
            "initiating": pending["initiating"],
            "customer_reply": paths["customer_reply"],
            "route": paths["route"],
        }

    pricing = profile.get("pricing") or {}
    spot_evidence = None
    spot = pricing.get("spot_metal") or {}
    if isinstance(spot, dict) and spot.get("enabled"):
        metal = cost_components.extract_metal(record.get("specification")).get("metal")
        if metal:
            try:
                spot_evidence = spot_price.get_prices(
                    args.monitor_root.resolve().parent / "spot-cache.json",
                    spot.get("provider"),
                    spot.get("refresh_frequency"),
                    [metal],
                    spot.get("currency", "USD"),
                    spot.get("unit", "gram"),
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                kolo_safe.manual_review_claimed(
                    args.monitor_root, args.claim_root, args.message_id, None,
                    "spot_price_unavailable", runner=getattr(args, "runner", subprocess.run),
                )
                return {"outcome": "manual_review", "reason_code": "spot_price_unavailable",
                        "error": str(exc)[:200], "next": "done"}
            write_private(Path(paths["work_dir"]) / "spot-evidence.json", spot_evidence)
    skeleton = cost_components.prepare(record, profile, spot_evidence)
    if skeleton.get("unresolved"):
        try:
            asked = ask_missing_rate(args)
        except ValueError as exc:
            kolo_safe.manual_review_claimed(
                args.monitor_root, args.claim_root, args.message_id, None,
                "invalid_cost_components", runner=getattr(args, "runner", subprocess.run),
            )
            return {"outcome": "manual_review", "reason_code": "invalid_cost_components",
                    "error": str(exc)[:200], "next": "done"}
        return {**asked, "next": "done"}
    write_private(Path(paths["work_dir"]) / "cost-skeleton.json", skeleton)
    return {
        "outcome": "specification_complete",
        "next": "price",
        "fill": skeleton["fill"],
        "fee_catalog": [item["rate_key"] for item in skeleton.get("fee_catalog", [])],
        "stone_catalog": [item["rate_key"] for item in skeleton.get("stone_catalog", [])],
        "typical_finished_weights": pricing.get("typical_finished_weights") or {},
        "pieces": skeleton.get("pieces") or [],
    }


ORDER_LEVEL_FEE_WORDS = ("shipping", "postage", "courier")


def _order_level_fee(rate_key: str) -> bool:
    """A fee the order pays once, not each piece (shipping was charged per piece live, 7 September 2026)."""
    return any(word in str(rate_key).lower() for word in ORDER_LEVEL_FEE_WORDS)


def price(args: argparse.Namespace) -> dict[str, Any]:
    """Fill the skeleton with the worker's quantities, finalize, request approval.

    The worker's whole contribution is a few numbers: finished grams, bench
    hours, a missing center carat, and which fee and accent-stone catalog
    entries apply. Rates, unit costs, the price, the binding, the brief, the
    record, the mirror, and the claim finish are all deterministic.
    """
    paths = _work_paths(args)
    skeleton_path = Path(paths["work_dir"]) / "cost-skeleton.json"
    skeleton = read_object(skeleton_path)
    profile = read_object(args.shop_profile)
    lines = skeleton["cost_components"]
    fee_catalog = {item["rate_key"]: item for item in skeleton.get("fee_catalog", [])}
    stone_catalog = {item["rate_key"]: item for item in skeleton.get("stone_catalog", [])}
    piece_quantities = getattr(args, "pieces", None)
    if piece_quantities:
        # One set of numbers per piece (MULTI-PIECE-PLAN.md): each fills its
        # own metal and labor line, its center stone if one is open, its fees
        # and accent stones, all labelled so the card reads per piece.
        piece_map = skeleton.get("pieces") or []
        if len(piece_quantities) != len(piece_map):
            raise ValueError(f"expected quantities for {len(piece_map)} piece(s)")
        for info, chosen in zip(piece_map, piece_quantities):
            label = str(info.get("label") or "piece")
            tag = f" ({label})"
            for value, name in ((chosen.get("finished_grams"), "finished grams"), (chosen.get("bench_hours"), "bench hours")):
                if value is None or value <= 0:
                    raise ValueError(f"{name} must be a positive number for the {label}")
            lines["metal_lines"][info["metal_line"]]["quantity_grams"] = float(chosen["finished_grams"])
            lines["labor_lines"][info["labor_line"]]["hours"] = float(chosen["bench_hours"])
            center = info.get("center_stone_line")
            if center is not None and lines["stone_lines"][center].get("quantity") is None:
                if chosen.get("center_carat") is None or chosen["center_carat"] <= 0:
                    raise ValueError(f"center carat is required for the {label}")
                lines["stone_lines"][center]["quantity"] = float(chosen["center_carat"])
            for key in chosen.get("fees") or []:
                if key not in fee_catalog:
                    raise ValueError(f"unknown fee '{key}'; choose from the fee catalog")
                if _order_level_fee(key) and any(line.get("rate_key") == key for line in lines["other_hard_cost_lines"]):
                    continue  # one order ships once, however many pieces
                lines["other_hard_cost_lines"].append({**fee_catalog[key], "label": fee_catalog[key]["label"] + tag})
            for accent in chosen.get("accents") or []:
                key, carats = accent.get("key"), accent.get("carats")
                if key not in stone_catalog:
                    raise ValueError(f"unknown accent stone '{key}'; choose from the stone catalog")
                if not isinstance(carats, (int, float)) or carats <= 0:
                    raise ValueError("accent stone carats must be positive")
                lines["stone_lines"].append({"stone": key.replace("_", " ") + tag, "rate_key": key,
                                             "quantity": float(carats), "unit_cost": float(stone_catalog[key]["rate"])})
    else:
        if len(skeleton.get("pieces") or []) > 1:
            raise ValueError("this estimate has more than one piece; it is priced per piece by the desk's inline path")
        for value, label in ((args.finished_grams, "finished grams"), (args.bench_hours, "bench hours")):
            if value is None or value <= 0:
                raise ValueError(f"{label} must be a positive number")
        lines["metal_lines"][0]["quantity_grams"] = float(args.finished_grams)
        lines["labor_lines"][0]["hours"] = float(args.bench_hours)
        if lines["stone_lines"]:
            if lines["stone_lines"][0].get("quantity") is None:
                if args.center_carat is None or args.center_carat <= 0:
                    raise ValueError("center carat is required for this piece")
                lines["stone_lines"][0]["quantity"] = float(args.center_carat)
        for key in args.fees or []:
            if key not in fee_catalog:
                raise ValueError(f"unknown fee '{key}'; choose from the fee catalog")
            lines["other_hard_cost_lines"].append(dict(fee_catalog[key]))
        for spec in args.accents or []:
            key, _, quantity = spec.partition(":")
            if key not in stone_catalog:
                raise ValueError(f"unknown accent stone '{key}'; choose from the stone catalog")
            try:
                carats = float(quantity)
            except ValueError as exc:
                raise ValueError("accent stones are written as rate_key:total_carats") from exc
            if carats <= 0:
                raise ValueError("accent stone carats must be positive")
            lines["stone_lines"].append({
                "stone": key.replace("_", " "),
                "rate_key": key,
                "quantity": carats,
                "unit_cost": float(stone_catalog[key]["rate"]),
            })
    overrides = (getattr(args, "unit_costs", None) or {})
    if overrides:
        # The owner's unit costs from the cost sheet (9 September 2026), matched by the line's item words.
        for group, label in (("metal_lines", "metal"), ("stone_lines", "stone"), ("labor_lines", "task"), ("other_hard_cost_lines", "label")):
            for line in lines.get(group) or []:
                item = str(line.get(label) or "")
                for name, cost in overrides.items():
                    if name and (name.lower() == item.lower() or name.lower() == str(line.get("rate_key") or "").replace("_", " ").lower()):
                        line["rate" if group == "labor_lines" else "total_cost" if group == "other_hard_cost_lines" else "unit_cost"] = float(cost)
    write_private(skeleton_path, skeleton)
    state = cost_components.finalize(skeleton, profile)
    write_private(Path(paths["current_state"]), state)
    record = request_approval(
        argparse.Namespace(
            monitor_root=args.monitor_root,
            claim_root=args.claim_root,
            record_root=args.record_root,
            message_id=args.message_id,
            estimate_id=args.estimate_id,
            current_state=Path(paths["current_state"]),
            approval_request=Path(paths["approval_request"]),
            shop_profile=args.shop_profile,
            record_output=Path(paths["current_record"]),
            renderings=getattr(args, "renderings", None),
        )
    )
    return {
        "outcome": "approval_requested",
        "proposed_price": state.get("proposed_price"),
        "cost_components": state.get("cost_components"),
        "record_status": record.get("status"),
        "next": "done",
    }


def _customer_name(monitor_root: Path, claim_root: Path, message_id: str) -> str:
    headers = kolo_safe.claimed_message_headers(monitor_root, claim_root, message_id)
    sender = headers.get("From") or ""
    return kolo_safe._sender_display(sender) if sender else "A customer"


def ask_same_sender(
    args: argparse.Namespace,
    token: str,
    route: dict[str, Any],
    existing_estimate_id: str | None,
    message: dict[str, Any],
) -> dict[str, Any]:
    """Same customer, new thread: ask the owner, in words, same piece or new."""
    existing = {}
    if existing_estimate_id:
        try:
            existing = estimate_record.read_object(
                estimate_record.record_path(args.record_root, existing_estimate_id)
            )
        except (OSError, ValueError):
            existing = {}
    who = _customer_name(args.monitor_root, args.claim_root, args.message_id)
    old_subject = " ".join(((existing.get("route") or {}).get("original_subject") or "").split())[:80]
    old_words = owner_questions.summary_of_piece(existing.get("specification")) if existing.get("specification") else "a piece"
    new_subject = " ".join((route.get("original_subject") or "").split())[:80]
    text = (
        f"{who} wrote in a new email thread (\"{new_subject}\") but already has an open estimate "
        f"with us for {old_words}"
        + (f' ("{old_subject}", {existing.get("status", "open").replace("_", " ")})' if old_subject else "")
        + ". Is this the same piece, or a new one? Reply \"same\" and I will carry that estimate on in the new "
        "thread, or \"new\" and I will quote it as a separate estimate."
    )
    root = owner_questions.questions_root(args.monitor_root)
    _created, question = owner_questions.create_decision(
        root, "same_sender", existing_estimate_id or "jed-0000000000000000", args.message_id, text,
        {"existing_estimate_id": existing_estimate_id, "new_subject": new_subject},
    )
    question = _attach_answer_command(root, args.monitor_root, question)
    question = owner_questions.deliver(
        root, question, runner=getattr(args, "runner", subprocess.run),
        extra_args=kolo_safe.owner_channel_args(args.monitor_root),
    )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "same_sender_question")
    return {
        "outcome": "awaiting_owner",
        "question_id": question["question_id"],
        "reference": owner_questions.reference(question["question_id"]),
        "delivery": question["delivery"]["status"],
        "estimate_id": existing_estimate_id,
        "next_action": "done",
    }


def ask_unclear_reply(args: argparse.Namespace, record: dict[str, Any], outcome: str) -> dict[str, Any]:
    """A reply after an estimate the desk could not read: ask the owner what it meant."""
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    who = _customer_name(args.monitor_root, args.claim_root, args.message_id)
    snippet = ""
    try:
        paths = inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)
        message = read_object(Path(paths["gmail_message"]))
        snippet = " ".join(gmail_text.body_text(message, limit=600).split())[:240]
    except (OSError, ValueError, json.JSONDecodeError):
        snippet = ""
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their estimate"
    why = "it may change the design" if outcome == "design_change_detected" else "I could not tell what they mean"
    text = (
        f"{who} replied on the estimate for {piece}"
        + (f': "{snippet}"' if snippet else "")
        + f". I did not act because {why}. Is this a second piece to quote, a change to this one, "
        "are they accepting the estimate, or will you handle it? Reply \"second piece\", \"change\", "
        "\"accepts\", or \"I will handle it\"."
    )
    root = owner_questions.questions_root(args.monitor_root)
    _created, question = owner_questions.create_decision(
        root, "unclear_reply", args.estimate_id, args.message_id, text, {"outcome": outcome},
    )
    question = _attach_answer_command(root, args.monitor_root, question)
    question = owner_questions.deliver(
        root, question, runner=getattr(args, "runner", subprocess.run),
        extra_args=kolo_safe.owner_channel_args(args.monitor_root),
    )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "unclear_reply_question")
    return {
        "outcome": "awaiting_owner",
        "question_id": question["question_id"],
        "reference": owner_questions.reference(question["question_id"]),
        "delivery": question["delivery"]["status"],
        "next_action": "done",
    }


def _reply_snippet(args: argparse.Namespace) -> str:
    try:
        paths = inbox_monitor.prepare_claim_work(args.monitor_root, args.claim_root, args.message_id)
        message = read_object(Path(paths["gmail_message"]))
        return " ".join(gmail_text.body_text(message, limit=600).split())[:240]
    except (OSError, ValueError, json.JSONDecodeError):
        return ""


def ask_stuck_claim(p: dict[str, Path], message_id: str, error: str, attempts: int, runner: Any = subprocess.run) -> dict[str, Any]:
    """The tick tried this email several times and could not finish: the owner decides (plan 3.3).

    The claim parks behind the question, so it is visible in open-questions
    and gets the one-day reminder; retry runs it again, skip or handle
    myself closes it as the owner's.
    """
    token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
    who = _customer_name(p["monitor_root"], p["claim_root"], message_id)
    snippet = ""
    try:
        paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)
        message = read_object(Path(paths["gmail_message"]))
        snippet = " ".join(gmail_text.body_text(message, limit=600).split())[:200]
    except (OSError, ValueError, json.JSONDecodeError):
        snippet = ""
    estimate_id = "jed-0000000000000000"
    try:
        estimate_id = inbox_monitor.load_queue_item(p["monitor_root"], message_id).get("estimate_id") or estimate_id
    except (OSError, ValueError, KeyError):
        pass
    text = (
        f"I could not finish {who}'s email"
        + (f' ("{snippet}")' if snippet else "")
        + f" after {attempts} tries: {error[:200]}. "
        "Reply \"retry\" and I will try again, \"skip\" and I will set it aside for you to handle, or \"handle myself\"."
    )
    root = owner_questions.questions_root(p["monitor_root"])
    # A claim can get stuck more than once (the owner said retry, it failed
    # again): each time is a new question with its own code. A closed one
    # must never be reused, or nothing reaches the owner and the claim parks
    # with no question to resume it (6 September 2026).
    earlier = [q for q in owner_questions.list_questions(root)
               if q["kind"] == "stuck_claim" and q["gmail_message_id"].split("#")[0] == message_id and q["status"] != "open"]
    qid_message = message_id if not earlier else f"{message_id}#round{len(earlier) + 1}"
    created, question = owner_questions.create_decision(
        root, "stuck_claim", estimate_id, qid_message, text, {"error": error[:300], "attempts": attempts, "source_message_id": message_id},
    )
    question = _attach_answer_command(root, p["monitor_root"], question)
    if created:
        owner_questions.deliver(root, question, runner=runner, extra_args=kolo_safe.owner_channel_args(p["monitor_root"]))
    inbox_monitor.park_item(p["monitor_root"], message_id, p["claim_root"], token, "stuck_question")
    return {"outcome": "awaiting_owner", "question_id": question["question_id"]}


def _answer_stuck_claim(args: argparse.Namespace, workspace: Path, p: dict[str, Path], root: Path,
                        question: dict[str, Any], outcome: str) -> dict[str, Any]:
    import inbox_watcher  # local import: inbox_watcher imports this module

    message_id = _question_message_id(question)
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "stuck_claim", "decision": outcome}
    if outcome == "retry":
        # The retry itself runs in the tick, with its own clock and a fresh
        # budget (a reopen clears the counters); the answer only reopens.
        _resume_parked_claim(p, message_id)
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result.update(_hand_to_tick(p, message_id))
        return result
    if _claim_parked(p, message_id):
        _close_parked_claim(p, message_id, f"owner_decided_{outcome}")
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    result["claim"] = f"owner_decided_{outcome}"
    return result


OWNER_SAYS_ESTIMATE_FILE = "owner-says-estimate.json"


def _inventory_fact(record: dict[str, Any]) -> dict[str, str]:
    """A ready-made inquiry: the visit is to see what is in the shop, not to settle a design."""
    if not record.get("inventory_inquiry"):
        return {}
    return {"ready-made pieces": "they asked about pieces already made or in stock; say you would be glad to show them what "
                                 "is ready in the shop and similar pieces that can be made for them, and invite them in; "
                                 "promise nothing about what is in stock, ask for no design details, no prices"}


def ask_out_of_scope(args: argparse.Namespace, note: str) -> dict[str, Any]:
    """The reading says the message is not an estimate request: the owner decides, nothing is filed silently.

    An appraisal, the price of something in stock, or a job status is out of
    the desk's scope (WORKFLOW.md), but a customer's own message never
    disappears into a list: the owner hears it as a question. "quote it"
    reads the message as a custom order from what they wrote; "handle
    myself" leaves the thread to the owner.
    """
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    who = _customer_name(args.monitor_root, args.claim_root, args.message_id)
    snippet = _reply_snippet(args)
    why = f" ({note.strip().rstrip('.')})" if note and note.strip() else ""
    text = (
        f"{who} wrote"
        + (f': "{snippet}"' if snippet else " to the shop")
        + f". That reads as something the desk does not quote{why}: an appraisal, the price of a piece in stock, or a job status. "
        "Reply \"quote it\" to estimate it as a custom piece from what they wrote, or \"handle myself\"."
    )
    root = owner_questions.questions_root(args.monitor_root)
    _created, question = owner_questions.create_decision(
        root, "out_of_scope", args.estimate_id, args.message_id, text, {"note": str(note or "")[:200]},
    )
    question = _attach_answer_command(root, args.monitor_root, question)
    if _created:
        question = owner_questions.deliver(
            root, question, runner=getattr(args, "runner", subprocess.run),
            extra_args=kolo_safe.owner_channel_args(args.monitor_root),
        )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "out_of_scope_question")
    return question


def ask_details_needed(p: dict[str, Path], record: dict[str, Any], message_id: str, runner: Any,
                       still_missing: list[str] | None = None) -> dict[str, Any]:
    """Concierge mode: the standing question the owner answers after the call or the visit (the jeweler, 9 September 2026).

    Dormant (no reminders: a visit may be a week away), delivered once with
    its code; the answer prices and renders. Asked again, with what is still
    missing, when the owner's details left a gap.
    """
    root = owner_questions.questions_root(p["monitor_root"])
    who = kolo_safe._sender_display(str((record.get("route") or {}).get("recipient") or "the customer"))
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their piece"
    gap = ""
    if still_missing:
        import pipeline  # local import: pipeline imports this module

        gap = " Still missing: " + "; ".join(pipeline.describe_missing(record.get("specification") or {}, still_missing)) + "."
    text = (
        f"After your call or visit with {who} about {piece}, reply here with the piece's details (the stone and origin, its size or "
        f"carat, the metal and karat, the setting) and I will price it, render it, and file one card.{gap} "
        "Or say \"price it\" to use what I have, or \"handle myself\"."
    )
    suffix = f"#gap{len(still_missing)}" if still_missing else ""
    created, question = owner_questions.create_decision(
        root, "details_needed", record["estimate_id"], f"{message_id}{suffix}", text, {"still_missing": list(still_missing or [])},
        dormant=True,
    )
    question = _attach_answer_command(root, p["monitor_root"], question)
    if created:
        owner_questions.deliver(root, question, runner=runner, extra_args=kolo_safe.owner_channel_args(p["monitor_root"]))
    return question


def _answer_details_needed(args: argparse.Namespace, p: dict[str, Path], root: Path, question: dict[str, Any],
                           outcome: str) -> dict[str, Any]:
    """The owner's details after the visit: the estimate is priced and rendered from them, one card, one email."""
    message_id = _question_message_id(question)
    estimate_id = question["estimate_id"]
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "details_needed", "decision": outcome}
    if outcome == "handle_myself":
        try:
            estimate_record.retire(p["record_root"], estimate_id, "owner_handles_thread", "the owner handles this thread after the visit")
        except ValueError:
            pass
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result["note"] = "the desk leaves this thread to the owner"
        return result
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    if record.get("status") != "awaiting_specs":
        raise ValueError(f"estimate {estimate_id} is {record.get('status')}; the details question is over")
    if outcome == "details_given":
        facts = estimate_record.owner_facts_in_words(args.answer, record.get("specification") or {})
        if not facts:
            raise ValueError("no details found in the answer; give the stone, its size or carat, the metal and karat, or say \"price it\"")
        estimate_record.owner_supplies_facts(p["record_root"], estimate_id, facts)
        try:
            import ledger  # local import: the ledger never imports this module

            ledger.add_facts(workspace_of(p["monitor_root"]) / "estimate-desk", estimate_id, [
                {"field": k, "piece": None, "stone": ledger.stone_of(k), "value": v, "source": "owner", "gmail_message_id": message_id,
                 "span": str(args.answer)[:200]} for k, v in facts.items()])
        except Exception:  # noqa: BLE001 - the record carries the facts; the ledger catches up on the re-read
            pass
        result["facts"] = facts
    estimate_record.mark_concierge(p["record_root"], estimate_id, details=True, details_answer=str(args.answer)[:200])
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    # The offer's claim finished long ago; it is reopened on purpose and the tick renders and prices the record.
    inbox_monitor.reopen_item(p["monitor_root"], message_id, p["claim_root"], 1, allow_processed=True)
    result.update(_hand_to_tick(p, message_id, "price_and_render", estimate_id))
    return result


def send_acknowledgement(p: dict[str, Path], record: dict[str, Any], message_id: str, body: str, runner: Any) -> dict[str, Any]:
    """Concierge mode: the one email that says the owner will work up the estimate; journaled, then the claim is complete."""
    import gateway_token  # local import; only needed when sending

    customer_content_guard.validate_customer_text(body)
    if customer_content_guard.DOLLAR_AMOUNT_RE.search(body):
        raise ValueError("the acknowledgement must not mention any dollar amount")
    work_dir = p["monitor_root"].resolve().parent / "work" / f"acknowledge-{inbox_claim.claim_key(message_id)[:16]}"
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload_path, response_path = work_dir / "gmail-payload.json", work_dir / "gmail-provider-response.json"
    _reuse_or_build_payload(payload_path, lambda: gmail_reply.build_reply(record["route"], body))
    token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
    delivery = gmail_safe.send_reply_claimed(
        p["claim_root"], message_id, token, f"acknowledged:{record['estimate_id']}:{message_id}",
        payload_path, response_path, gateway_token.load_token(), runner=runner,
    )
    updated = estimate_record.mark_concierge(p["record_root"], record["estimate_id"], acknowledged=message_id,
                                             acknowledged_provider_message_id=delivery.get("id"))
    mirror_record(updated, work_dir / "current-record.json")
    kolo_safe.complete_claimed(p["monitor_root"], p["claim_root"], message_id, token)
    return {"outcome": "acknowledged", "provider_message_id": delivery.get("id")}


def _number(text: Any) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(text or "").replace(",", ""))
    return float(match.group(0)) if match else None


def draft_quantities(draft: dict[str, Any]) -> dict[str, Any]:
    """The owner's numbers from a cost sheet block: grams, hours, the center carat, and unit costs by item."""
    out: dict[str, Any] = {"unit_costs": {}}
    for line in draft.get("lines") or []:
        if not isinstance(line, dict):
            continue
        kind, item = str(line.get("line") or "").lower(), str(line.get("item") or "")
        quantity, unit_cost = _number(line.get("quantity")), _number(line.get("unit_cost"))
        if kind == "metal" and quantity is not None and "finished_grams" not in out:
            out["finished_grams"] = quantity
        elif kind == "labor" and quantity is not None and "bench_hours" not in out:
            out["bench_hours"] = quantity
        elif kind == "stones" and quantity is not None and "center_carat" not in out:
            out["center_carat"] = quantity
        if unit_cost is not None and item:
            out["unit_costs"][item] = unit_cost
    return out


def act_on_sheet_ready(workspace: Path, record: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """A cost sheet block marked ready: the details and numbers on it price the estimate, like a chat answer (9 September 2026)."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(Path(workspace).resolve())
    estimate_id = str(record.get("estimate_id") or "")
    if record.get("status") != "awaiting_specs":
        raise ValueError(f"estimate {estimate_id} is {record.get('status')}; nothing to price from the sheet")
    message_id = str((record.get("route") or {}).get("gmail_message_id") or "")
    if not message_id:
        raise ValueError("the record names no message to price from")
    facts = estimate_record.owner_facts_in_words(str(draft.get("details") or ""), record.get("specification") or {})
    if facts:
        estimate_record.owner_supplies_facts(p["record_root"], estimate_id, facts)
        try:
            import ledger  # local import: the ledger never imports this module

            ledger.add_facts(workspace_of(p["monitor_root"]) / "estimate-desk", estimate_id, [
                {"field": k, "piece": None, "stone": ledger.stone_of(k), "value": v, "source": "owner", "gmail_message_id": message_id,
                 "span": str(draft.get("details") or "")[:200]} for k, v in facts.items()])
        except Exception:  # noqa: BLE001 - the record carries the facts
            pass
    quantities = draft_quantities(draft)
    estimate_record.mark_sheet_draft_acted(p["record_root"], estimate_id, str(draft.get("hash") or ""), quantities)
    profile = read_object(p["shop_profile"])
    concierge = estimate_record.desk_mode(profile) == "concierge"
    if concierge:
        estimate_record.mark_concierge(p["record_root"], estimate_id, details=True, details_answer="from the cost sheet")
    inbox_monitor.reopen_item(p["monitor_root"], message_id, p["claim_root"], 1, allow_processed=True)
    step = "price_and_render" if concierge else "price_from_record"
    handed = _hand_to_tick(p, message_id, step, estimate_id)
    return {"facts": facts, "quantities": quantities, "step": step, **handed}


def ask_prior_piece(args: argparse.Namespace, record: dict[str, Any], specification: dict[str, Any] | None = None) -> dict[str, Any]:
    """The customer says the shop made the piece and the desk has nothing on file: the owner's books decide.

    The jeweler's rule (9 September 2026): a piece on file is never asked
    about; one the desk cannot find goes to the owner before anything is
    sent. "Details" price the new piece from the owner's words; "not on
    file" makes the desk ask the customer what it still needs (with or
    without a photo); "handle myself" leaves the thread to the owner.
    """
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    who = _customer_name(args.monitor_root, args.claim_root, args.message_id)
    spec = specification or record.get("specification") or {}
    piece = owner_questions.summary_of_piece(spec) if spec else "a piece"
    snippet = _reply_snippet(args)
    text = (
        f"{who} says you made {piece} for them before"
        + (f' ("{snippet}")' if snippet else "")
        + ", and the desk has nothing on file for it. Reply with the original's details (stone and origin, its shape and size or "
        "carat, the metal and karat) and I will price the new one from them; or \"not on file\" and I will ask them what I still "
        "need; or \"handle myself\"."
    )
    root = owner_questions.questions_root(args.monitor_root)
    _created, question = owner_questions.create_decision(
        root, "prior_piece", args.estimate_id, args.message_id, text, {"piece": piece[:160]},
    )
    question = _attach_answer_command(root, args.monitor_root, question)
    if _created:
        question = owner_questions.deliver(
            root, question, runner=getattr(args, "runner", subprocess.run),
            extra_args=kolo_safe.owner_channel_args(args.monitor_root),
        )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "prior_piece_question")
    return question


def _answer_prior_piece(args: argparse.Namespace, p: dict[str, Path], root: Path, question: dict[str, Any],
                        outcome: str) -> dict[str, Any]:
    """The owner's word on a piece the desk could not find: details, not on file, or handle myself."""
    message_id = _question_message_id(question)
    estimate_id = question["estimate_id"]
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "prior_piece", "decision": outcome}
    if outcome == "handle_myself":
        if _claim_parked(p, message_id):
            _close_parked_claim(p, message_id, "owner_decided_handle_myself")
        try:
            estimate_record.retire(p["record_root"], estimate_id, "owner_handles_thread", "the owner handles this repeat customer's thread")
        except ValueError:
            pass
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result["note"] = "the desk leaves this thread to the owner"
        return result
    if outcome == "details_given":
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
        facts = estimate_record.owner_facts_in_words(args.answer, record.get("specification") or {})
        if not facts:
            raise ValueError("no details found in the answer; give the stone, its size or carat, and the metal, or say \"not on file\"")
        estimate_record.owner_supplies_facts(p["record_root"], estimate_id, facts)
        try:
            import ledger  # local import: the ledger never imports this module

            ledger.add_facts(workspace_of(p["monitor_root"]) / "estimate-desk", estimate_id, [
                {"field": k, "piece": None, "stone": ledger.stone_of(k), "value": v, "source": "owner", "gmail_message_id": message_id,
                 "span": str(args.answer)[:200]} for k, v in facts.items()])
        except Exception:  # noqa: BLE001 - the record carries the facts; the ledger catches up on the re-read
            pass
        estimate_record.mark_prior_piece(p["record_root"], estimate_id, {"on_file": True, "owner": "details", "facts": sorted(facts)})
        result["facts"] = facts
    else:
        estimate_record.mark_prior_piece(p["record_root"], estimate_id, {"on_file": False, "owner": "not_on_file"})
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    _resume_parked_claim(p, message_id)
    result.update(_hand_to_tick(p, message_id))
    return result


def ask_followup_stalled(args: argparse.Namespace, record: dict[str, Any], repeated: list[str]) -> dict[str, Any]:
    """The customer was asked for these details once and did not give them: the owner decides.

    Sending the same question twice is what a broken machine does; a customer
    who asks why the shop needs a stone's color grade deserves a person's
    answer. Options: price it without the details (they become the jeweler's
    choice), ask once more, or the owner takes the thread.
    """
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    who = _customer_name(args.monitor_root, args.claim_root, args.message_id)
    snippet = _reply_snippet(args)
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their piece"
    fields = ", ".join(f.replace("_", " ") for f in repeated)
    text = (
        f"{who} replied about {piece}"
        + (f': "{snippet}"' if snippet else "")
        + f". I already asked once for {fields} and did not get it, so I did not ask again. "
        "Reply \"skip\" to price it without those details (they become your call), "
        "\"ask again\" to send the question once more, or \"handle myself\"."
    )
    root = owner_questions.questions_root(args.monitor_root)
    _created, question = owner_questions.create_decision(
        root, "followup_stalled", args.estimate_id, args.message_id, text, {"repeated": list(repeated)},
    )
    question = _attach_answer_command(root, args.monitor_root, question)
    if _created:
        question = owner_questions.deliver(
            root, question, runner=getattr(args, "runner", subprocess.run),
            extra_args=kolo_safe.owner_channel_args(args.monitor_root),
        )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "followup_stalled_question")
    return {"outcome": "awaiting_owner", "question_id": question["question_id"],
            "reference": owner_questions.reference(question["question_id"]), "next_action": "done"}


def ask_missing_rate(args: argparse.Namespace) -> dict[str, Any]:
    """Ask the owner for the one rate pricing lacks, and park this claim.

    WORKFLOW.md 6.10: a missing rate is not an error and not a review. The
    question goes to the owner's channel in plain words, the claim waits as
    awaiting_owner with its work directory intact, and answer_question()
    resumes it once the owner replies. Everything here is idempotent: a
    repeat neither re-asks nor re-sends.
    """
    claim_token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    record = estimate_record.read_object(
        estimate_record.record_path(args.record_root, args.estimate_id)
    )
    queue_item = inbox_monitor.load_queue_item(args.monitor_root, args.message_id)
    if queue_item["thread_id"] != record["route"]["thread_id"]:
        raise ValueError("the claimed message is not on this estimate's thread")
    profile = read_object(args.shop_profile)
    missing = cost_components.missing_rates(record, profile)
    if not missing:
        raise ValueError("no rate is missing for this specification; price it instead")
    rate = missing[0]
    headers = kolo_safe.claimed_message_headers(args.monitor_root, args.claim_root, args.message_id)
    customer = kolo_safe._sender_display(headers.get("From", "")) if headers.get("From") else None
    root = owner_questions.questions_root(args.monitor_root)
    created, question = owner_questions.create_missing_rate(
        root,
        args.estimate_id,
        args.message_id,
        rate,
        customer,
        owner_questions.summary_of_piece(record.get("specification")),
    )
    question = _attach_answer_command(root, args.monitor_root, question)
    question = owner_questions.deliver(
        root, question, runner=getattr(args, "runner", subprocess.run),
        extra_args=kolo_safe.owner_channel_args(args.monitor_root),
    )
    inbox_monitor.park_item(
        args.monitor_root, args.message_id, args.claim_root, claim_token, "missing_rate"
    )
    return {
        "outcome": "awaiting_owner",
        "question_id": question["question_id"],
        "reference": owner_questions.reference(question["question_id"]),
        "created": created,
        "delivery": question["delivery"]["status"],
        "rate_kind": rate["rate_kind"],
        "rate_key": rate["suggested_key"],
        "estimate_id": args.estimate_id,
        "next_action": "done",
    }


def open_questions(args: argparse.Namespace) -> list[dict[str, Any]]:
    """What the desk is still waiting on from the owner, oldest first."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    root = owner_questions.questions_root(p["monitor_root"])
    return [
        {
            "question_id": q["question_id"],
            "reference": owner_questions.reference(q["question_id"]),
            "estimate_id": q["estimate_id"],
            "kind": q["kind"],
            "asked_at": q["asked_at"],
            "delivery": q["delivery"]["status"],
            "text": q["text"],
        }
        for q in owner_questions.list_questions(root, "open")
    ]


def answer_question(args: argparse.Namespace) -> dict[str, Any]:
    """Take the owner's reply, save the rate, and resume the parked inquiry.

    Runs in the main Kolo session, which is where the owner's answer arrives.
    The reply is read for exactly one number; the number goes on the rate
    card with provenance; the parked claim reopens under a worker lease and
    a one-shot worker is started to price the piece. The price still goes
    through the normal approval, so a misread number is caught there.
    """
    import inbox_watcher  # local import: inbox_watcher imports this module

    workspace = args.workspace.resolve()
    p = inbox_watcher.paths_for(workspace)
    root = owner_questions.questions_root(p["monitor_root"])
    question = _question_to_answer(args, p, root)
    if question["status"] == "answered" and question["kind"] == "missing_rate":
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], question["estimate_id"]))
        if record.get("status") == "awaiting_specs" and _rate_claim_resumable(p, question):
            # The rate is on the card already; the pricing after it never
            # finished. Price now from the recorded review.
            message_id, estimate_id = _question_message_id(question), question["estimate_id"]
            _resume_parked_claim(p, message_id)
            priced = _price_after_rate_answer(args, workspace, p, message_id, estimate_id)
            return {"outcome": "replayed", "question_id": question["question_id"], "estimate_id": estimate_id, **(priced or {})}
    if question["status"] == "answered":
        if question["kind"] in owner_questions.DECISION_KINDS and _claim_still_waiting(p, question):
            # An earlier run recorded the answer and then failed before the
            # inquiry moved; replay the recorded answer rather than refusing.
            args.answer = question["answer"]["text"]
            replayed = answer_decision(args, workspace, p, root, question)
            replayed["replayed"] = True
            return replayed
        return {
            "outcome": "already_answered",
            "question_id": question["question_id"],
            "answer": question.get("answer"),
        }
    if question["kind"] in owner_questions.DECISION_KINDS:
        return answer_decision(args, workspace, p, root, question)
    if question["kind"] != "missing_rate":
        raise ValueError("unsupported question kind")
    # Refuse before writing anything if the estimate is not in the state the
    # question left it in. A hand-edited or already-priced record must be
    # repaired or handled deliberately, not turned into a misleading review.
    record = estimate_record.read_object(
        estimate_record.record_path(p["record_root"], question["estimate_id"])
    )
    try:
        route_ownership.validate_record(record)
    except ValueError as exc:
        raise ValueError(
            f"estimate record {question['estimate_id']} is invalid ({exc}); "
            "repair it before answering"
        ) from exc
    if record.get("status") != "awaiting_specs":
        raise ValueError(
            f"estimate record {question['estimate_id']} is {record.get('status')}, "
            "not awaiting_specs; nothing to price"
        )
    value = owner_questions.parse_amount(args.answer)
    question = owner_questions.record_answer(root, question, args.answer, value)
    if re.search(r"(?i)\b(?:once|one[- ]time|just this (?:one|time|estimate|job)|this (?:estimate|job|one) only)\b", str(args.answer or "")):
        # "use 450 once": this estimate only, the card untouched (the owner, 9 September 2026).
        estimate_record.set_one_time_rate(p["record_root"], question["estimate_id"], question["rate"]["rate_kind"],
                                          question["rate"]["rate_key"], value)
    else:
        owner_questions.save_rate(
            p["shop_profile"],
            question["rate"]["rate_kind"],
            question["rate"]["rate_key"],
            value,
            owner_questions.answer_provenance(question),
        )
    message_id = _question_message_id(question)
    estimate_id = question["estimate_id"]
    import cron_config  # local import keeps module import order unchanged

    reopened = _resume_parked_claim(p, message_id)
    work_dir = Path(reopened["work_paths"]["work_dir"])
    if not Path(reopened["work_paths"]["gmail_message"]).exists():
        import gmail_fetch  # local import; only needed when the work file was cleaned up

        gmail_fetch.fetch_claimed(
            p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token()
        )
    write_private(
        work_dir / "intake-result.json",
        {
            "message_id": message_id,
            "estimate_id": estimate_id,
            "outcome": "owner_answered",
            "question_id": question["question_id"],
            "next_action": "review_thread",
            "work_paths": reopened["work_paths"],
        },
    )
    result = {
        "outcome": "answered",
        "question_id": question["question_id"],
        "estimate_id": estimate_id,
        "rate_kind": question["rate"]["rate_kind"],
        "rate_key": question["rate"]["rate_key"],
        "value": value,
        "worker_job_id": None,
    }
    priced = _price_after_rate_answer(args, workspace, p, message_id, estimate_id)
    if priced is None:
        raise ValueError("inline judgment is switched off in pipeline.json; the desk has no other way to price")
    result.update(priced)
    return result


def _question_to_answer(args: argparse.Namespace, p: dict[str, Path], root: Path) -> dict[str, Any]:
    """The named question, the only open one, or an answered one whose inquiry is still waiting."""
    if args.question:
        return owner_questions.find(root, args.question)
    coded, rest = owner_questions.code_in_answer(root, args.answer or "")
    if coded is not None:
        args.answer = rest or args.answer
        return coded
    try:
        return owner_questions.pick_open(root, args.answer or "")
    except ValueError as exc:
        answered = [
            q for q in owner_questions.list_questions(root, "answered")
            if (q["kind"] in owner_questions.DECISION_KINDS and _claim_still_waiting(p, q))
            or (q["kind"] == "missing_rate" and _rate_claim_resumable(p, q))
        ]
        if len(answered) == 1:
            return answered[0]
        dormant = _dormant_for_answer(p, root, args.answer or "")
        if dormant is not None:
            return dormant
        raise exc


def _dormant_for_answer(p: dict[str, Path], root: Path, answer: str) -> dict[str, Any] | None:
    """After a rejected appointment card the owner just says what they want.

    The words go to the customer whose card was filed most recently, or to
    the one whose name or address the owner mentioned when several are open.
    """
    candidates = [q for q in owner_questions.list_questions(root, "open")
                  if q["kind"] == "appointment_next" and q.get("dormant")]
    if not candidates:
        return None
    words = answer.lower()
    named = []
    for q in candidates:
        try:
            record = estimate_record.read_object(estimate_record.record_path(p["record_root"], q["estimate_id"]))
        except (OSError, ValueError):
            continue
        recipient = str(record.get("route", {}).get("recipient", "")).lower()
        local = recipient.split("@")[0]
        display = kolo_safe._sender_display(recipient).lower()
        if recipient and (recipient in words or (local and local in words) or any(part in words for part in display.split() if len(part) > 2)):
            named.append(q)
    pool = named or candidates
    return sorted(pool, key=lambda q: q["asked_at"])[-1]


def _claim_parked(p: dict[str, Path], message_id: str) -> bool:
    try:
        state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], message_id))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return state.get("status") == "awaiting_owner"


def _claim_still_waiting(p: dict[str, Path], question: dict[str, Any]) -> bool:
    """True while the answered question's inquiry has not moved past the answer.

    Parked means the answer never took; processing without an intake result
    means an earlier attempt reopened the claim and failed before intake.
    Anything else has moved on, and replaying would double the work.
    """
    if question.get("kind") == "appointment_next":
        return False
    try:
        state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], _question_message_id(question)))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if state.get("status") == "awaiting_owner":
        return True
    if state.get("status") != "processing":
        return False
    work_dir = p["monitor_root"].resolve().parent / "work" / inbox_claim.claim_key(_question_message_id(question))
    return not (work_dir / "intake-result.json").exists()


RESUMABLE_REVIEW_REASONS = frozenset({
    "conflicting_thread_review_for_source_message",
    "stale_processing_retry_exhausted",
    "initiating_claim_not_processed",
})


def _rate_claim_resumable(p: dict[str, Path], question: dict[str, Any]) -> bool:
    try:
        state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], _question_message_id(question)))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if state.get("status") == "awaiting_owner":
        return True
    if state.get("status") == "processing" and "inline_attempts" in state:
        # The answer's own run owns it: replay when that run recorded a
        # failure or its lease has lapsed (it died); never under a live run.
        return bool(state.get("last_error")) or not inbox_claim.recovery_lease_active(state)
    return state.get("status") == "manual_review" and state.get("reason_code") in RESUMABLE_REVIEW_REASONS


def _price_after_rate_answer(args: argparse.Namespace, workspace: Path, p: dict[str, Path],
                             message_id: str, estimate_id: str) -> dict[str, Any] | None:
    """Price inline from the recorded review; None means fall back to a worker."""
    import inbox_watcher  # local import: inbox_watcher imports this module
    import pipeline  # local import: pipeline imports this module

    switch = pipeline.settings(workspace / "estimate-desk")
    if not switch.get("inline"):
        return None
    return _hand_to_tick(p, message_id, "price_from_record", estimate_id)


def _resume_parked_claim(p: dict[str, Path], message_id: str) -> dict[str, Any]:
    """Lease the parked claim for the answer; carry on if an earlier attempt already reopened it."""
    import cron_config  # local import keeps module import order unchanged

    state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], message_id))
    if state.get("status") == "manual_review" and state.get("reason_code") in RESUMABLE_REVIEW_REASONS:
        # A review the desk opened on itself (a conflict or a stale claim), not
        # one the owner asked for: take it back and carry on.
        item = inbox_monitor.load_queue_item(p["monitor_root"], message_id)
        if item.get("review_status", "open") == "open":
            inbox_monitor.resolve_manual_review(p["monitor_root"], item["gmail_message_id_sha256"])
        inbox_claim.reopen(p["claim_root"], message_id, cron_config.WORKER_LEASE_SECONDS, allow_manual_review=True)
        queue_item = inbox_monitor.load_queue_item(p["monitor_root"], message_id)
        for key in ("review_status", "review_resolved_at"):
            queue_item.pop(key, None)
        inbox_monitor.atomic_write_json(inbox_monitor.queue_path(p["monitor_root"], message_id), queue_item)
        state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], message_id))
        queue_item = inbox_monitor.sync_claim(p["monitor_root"], message_id, {"acquired": True, **state})
        return {"queue_item": queue_item, "claim": {"acquired": True, "resumed": True, **state},
                "work_paths": inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id)}
    if state.get("status") == "awaiting_owner":
        return inbox_monitor.reopen_item(p["monitor_root"], message_id, p["claim_root"], cron_config.WORKER_LEASE_SECONDS)
    if state.get("status") == "processing":
        token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
        state = inbox_claim.delegate(p["claim_root"], message_id, token, cron_config.WORKER_LEASE_SECONDS)
        queue_item = inbox_monitor.sync_claim(p["monitor_root"], message_id, {"acquired": True, **state})
        return {
            "queue_item": queue_item,
            "claim": {"acquired": True, "resumed": True, **state},
            "work_paths": inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], message_id),
        }
    raise ValueError(
        f"claim is {state.get('status')}; the inquiry is no longer waiting on this answer"
    )


def _close_parked_claim(p: dict[str, Path], message_id: str, reason: str) -> None:
    """Finish a parked claim on the owner's word: terminal, reviewed, no card."""
    _resume_parked_claim(p, message_id)
    token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
    inbox_claim.finish(p["claim_root"], message_id, token, "manual_review", reason)
    inbox_monitor.reconcile_terminal(p["monitor_root"], message_id, p["claim_root"])
    inbox_monitor.cleanup_claim_work(p["monitor_root"], message_id)
    item = inbox_monitor.load_queue_item(p["monitor_root"], message_id)
    inbox_monitor.resolve_manual_review(p["monitor_root"], item["gmail_message_id_sha256"])


def answer_decision(
    args: argparse.Namespace, workspace: Path, p: dict[str, Path], root: Path, question: dict[str, Any]
) -> dict[str, Any]:
    """Apply a fixed-outcome answer: same piece or new; what an unclear reply meant."""
    import cron_config  # local import keeps module import order unchanged
    import inbox_watcher  # local import: inbox_watcher imports this module

    outcome = owner_questions.match_option(question, args.answer)
    message_id = _question_message_id(question)
    result: dict[str, Any] = {
        "outcome": "answered", "question_id": question["question_id"], "kind": question["kind"], "decision": outcome,
    }
    if question["kind"] == "same_sender" and outcome == "new":
        # Lease the claim first: if that fails nothing is recorded, so the
        # same command can simply be run again.
        reopened = _resume_parked_claim(p, message_id)
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        if not Path(reopened["work_paths"]["gmail_message"]).exists():
            import gmail_fetch  # local import; only needed when the work file was cleaned up

            gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token())
        intake_result = intake(argparse.Namespace(
            monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
            message_id=message_id, shop_profile=p["shop_profile"], force_new_inquiry=True,
        ))
        result["intake"] = {k: intake_result.get(k) for k in ("decision", "estimate_id", "next_action", "outcome")}
        if intake_result.get("next_action") != "review_thread":
            return result
        work_dir = Path(reopened["work_paths"]["work_dir"])
        write_private(work_dir / "intake-result.json", intake_result)
        result.update(_hand_to_tick(p, message_id))
        return result
    if question["kind"] == "same_sender" and outcome == "same":
        return _answer_same_piece(args, workspace, p, root, question)
    if question["kind"] == "out_of_scope" and outcome == "quote":
        # The owner says quote it: the message is read again as an estimate request, by the tick.
        reopened = _resume_parked_claim(p, message_id)
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        work_dir = Path(reopened["work_paths"]["work_dir"])
        write_private(work_dir / OWNER_SAYS_ESTIMATE_FILE, {"question_id": question["question_id"], "answer": str(args.answer)[:200]})
        result.update(_hand_to_tick(p, message_id))
        return result
    if question["kind"] == "prior_piece":
        return _answer_prior_piece(args, p, root, question, outcome)
    if question["kind"] == "details_needed":
        return _answer_details_needed(args, p, root, question, outcome)
    if question["kind"] == "unclear_reply" and outcome in {"design_change", "second_piece"}:
        return _answer_design_change(args, workspace, p, root, question, outcome)
    if question["kind"] == "appointment_next":
        return _answer_appointment_next(args, workspace, p, root, question, outcome)
    if question["kind"] == "price_next":
        return _answer_price_next(args, p, root, question, outcome)
    if question["kind"] == "rendering_next":
        return _answer_rendering_next(args, workspace, p, root, question, outcome)
    if question["kind"] == "command_failed":
        return _answer_command_failed(args, p, root, question, outcome)
    if question["kind"] == "stuck_claim":
        return _answer_stuck_claim(args, workspace, p, root, question, outcome)
    if question["kind"] == "followup_stalled" and outcome in {"skip", "ask_again"}:
        import pipeline  # local import: pipeline imports this module

        estimate_id = question["estimate_id"]
        switch = pipeline.settings(workspace / "estimate-desk")
        openclaw = args.openclaw or inbox_watcher.default_openclaw()
        reopened = _resume_parked_claim(p, message_id)
        if not Path(reopened["work_paths"]["gmail_message"]).exists():
            import gmail_fetch  # local import; only needed when the work file was cleaned up

            gmail_fetch.fetch_claimed(p["monitor_root"], p["claim_root"], message_id, gateway_token.load_token())
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        if outcome == "skip":
            estimate_record.mark_jewelers_choice(p["record_root"], estimate_id, message_id, list(question.get("context", {}).get("repeated") or []))
            result.update(_hand_to_tick(p, message_id, "price_from_record", estimate_id))
        else:
            result.update(_hand_to_tick(p, message_id, "resend_followup", estimate_id))
        return result
    # Every other outcome: the owner takes the conversation from here.
    reason = f"owner_decided_{outcome}"
    _close_parked_claim(p, message_id, reason)
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    result["claim"] = reason
    return result


RENDERING_NOTE = (
    "Hello,\n\nAttached are design renderings of {piece}. They are for guidance only: they show the "
    "direction of the design, and a rendering that comes close is still not the finished piece, so "
    "small details may differ. The written specification and the final design you approve are what "
    "we make.\n\nIf you would like anything changed, reply here and tell me.\n\n{shop}\n"
)


def _rendering_images(paths: dict[str, str]) -> list[Path]:
    return [Path(paths[key]) for key in ("rendering_image_1", "rendering_image_2", "rendering_image_3", "rendering_image_4")
            if key in paths and Path(paths[key]).exists()]


def _sha256_file(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def request_rendering_approval(args: argparse.Namespace) -> dict[str, Any]:
    """Show the owner the views, file the approval, park the claim."""
    paths = _work_paths(args)
    images = _rendering_images(paths)
    if not images:
        raise ValueError("no materialized rendering images to approve")
    record = estimate_record.read_object(estimate_record.record_path(args.record_root, args.estimate_id))
    route_ownership.validate_record(record)
    token = inbox_claim.authoritative_claim_token(args.claim_root, args.message_id)
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their estimate"
    details = {
        "schema_version": 1,
        "action_type": "send_rendering",
        "estimate_id": args.estimate_id,
        "gmail_message_id": args.message_id,
        "thread_id": record["route"]["thread_id"],
        "customer_email": record["route"]["recipient"],
        "piece": piece,
        "images": [{"slot": index, "sha256": _sha256_file(image)} for index, image in enumerate(images, start=1)],
        **({"checker": str(getattr(args, "checker", ""))[:200]} if getattr(args, "checker", None) else {}),
        **({"archetype": str(getattr(args, "archetype", ""))[:40]} if getattr(args, "archetype", None) else {}),
        **({"revised": str(getattr(args, "revised", ""))[:160]} if getattr(args, "revised", None) else {}),
        **({"revision": int(getattr(args, "revision", 1))} if int(getattr(args, "revision", 1) or 1) > 1 else {}),
        "execute": execute_line(
            args.monitor_root, "send-approved-rendering",
            estimate_id=args.estimate_id, message_id=args.message_id, brief_id="<Brief ID>",
        ),
    }
    revision = int(getattr(args, "revision", 1) or 1)
    approval_path = Path(paths["work_dir"]) / "rendering-approval.json"
    suffix = f":r{revision}" if revision > 1 else ""
    action_key = f"rendering_approval:{args.estimate_id}:{args.message_id}{suffix}"
    if approval_path.exists() and read_object(approval_path) != details:
        existing = read_object(approval_path)
        claim_state = inbox_claim.read_state(inbox_claim.claim_path(args.claim_root, args.message_id))
        carded = (claim_state.get("external_actions") or {}).get(action_key) is not None
        if revision > int(existing.get("revision") or 1):
            # The owner passed on those views; keep them as history, bind the new ones.
            write_private(Path(paths["work_dir"]) / f"rendering-approval-r{int(existing.get('revision') or 1)}.json", existing)
        elif not carded:
            # A binding no card was ever filed against: a run that died between
            # writing it and filing (7 September 2026: killed during the
            # previews; the next run rendered again and was refused). History,
            # then the images the owner will actually see.
            stale = 1
            while (Path(paths["work_dir"]) / f"rendering-approval-stale-{stale}.json").exists():
                stale += 1
            write_private(Path(paths["work_dir"]) / f"rendering-approval-stale-{stale}.json", existing)
        else:
            raise ValueError("existing rendering approval binding changed")
    write_private(approval_path, details)
    runner = getattr(args, "runner", subprocess.run)
    customer = kolo_safe._sender_display(record["route"]["recipient"])
    try:
        profile = read_object(args.shop_profile) if getattr(args, "shop_profile", None) else {}
        _prepare_email(
            {"monitor_root": args.monitor_root, "shop_profile": args.shop_profile}, record, args.message_id, "rendering",
            {"piece": piece, "shop name": (profile.get("shop") or {}).get("name") or "the shop"},
            RENDERING_NOTE.format(piece=piece, shop=(profile.get("shop") or {}).get("name") or "the shop"),
            Path(paths["customer_reply"]), _digest_from_work(paths, args.message_id, profile), runner,
        )
    except Exception:  # noqa: BLE001 - the executor drafts if this did not happen
        pass
    labels: dict[int, str] = {}
    try:
        report = read_object(Path(paths["work_dir"]) / "rendering-report.json")
        if len(report.get("pieces") or []) > 1:
            labels = {int(v["slot"]): str(v.get("piece") or "") for v in report.get("views") or []}
    except (OSError, ValueError, KeyError, TypeError):
        labels = {}
    for index, image in enumerate(images, start=1):
        which = f" ({labels[index]})" if labels.get(index) else ""
        kolo_safe.send_owner_preview(
            args.monitor_root,
            f"Rendering {index} of {len(images)}{which} for {customer}'s {piece}. An approval card follows; "
            "approve it to email these to the customer.",
            image, runner=runner,
        )
    approver = activation_binding.load(activation_binding.binding_path(args.monitor_root))
    kolo_safe.request_rendering_approval_claimed(
        args.claim_root, args.message_id, token, action_key,
        args.estimate_id, approval_path, approver["session_key"], runner=runner,
    )
    inbox_monitor.park_item(args.monitor_root, args.message_id, args.claim_root, token, "rendering_approval")
    _register_brief(args.monitor_root, "rendering", kolo_safe.rendering_title(details), args.estimate_id, args.message_id, runner)
    return {"outcome": "rendering_approval_requested", "images": len(images), "next": "done", "revision": revision}


def send_approved_rendering(args: argparse.Namespace) -> dict[str, Any]:
    """The owner approved: verify the very images they saw, then send them."""
    import cron_config  # local import keeps module import order unchanged
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    runner = getattr(args, "runner", subprocess.run)
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], args.estimate_id))
    source_hash = estimate_record.sha256_text(args.message_id)
    for delivery in record.get("rendering_deliveries") or []:
        if isinstance(delivery, dict) and delivery.get("source_message_id_sha256") == source_hash \
                and delivery.get("status") == "sent":
            # The line already ran to the end (the session pasted it before the
            # tick, or a retry follows a finished run): the same outcome, no
            # second email, and a repeat report Kolo may refuse is tolerated.
            result = {"outcome": "already_sent", "images": len(delivery.get("image_sha256") or []),
                      "record_status": record.get("status")}
            _report_brief(args, result, runner, repeat=True)
            return result
    state = inbox_claim.read_state(inbox_claim.claim_path(p["claim_root"], args.message_id))
    if state.get("status") == "processing":
        # An earlier run of this command reopened the claim and died; carry on.
        paths = inbox_monitor.prepare_claim_work(p["monitor_root"], p["claim_root"], args.message_id)
    else:
        reopened = inbox_monitor.reopen_item(p["monitor_root"], args.message_id, p["claim_root"], cron_config.WORKER_LEASE_SECONDS)
        paths = reopened["work_paths"]
    try:
        return _send_approved_rendering(args, p, paths, runner)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, judge.JudgmentError):
        # The send did not happen: the claim goes back behind its card, so
        # the tick does not re-render it; the owner's "retry" runs this line.
        try:
            token = inbox_claim.authoritative_claim_token(p["claim_root"], args.message_id)
            inbox_monitor.park_item(p["monitor_root"], args.message_id, p["claim_root"], token, "rendering_approval")
        except (OSError, ValueError):
            pass
        raise


def _send_approved_rendering(args: argparse.Namespace, p: dict[str, Path], paths: dict[str, str], runner: Any) -> dict[str, Any]:
    approval = read_object(Path(paths["work_dir"]) / "rendering-approval.json")
    if approval.get("estimate_id") != args.estimate_id or approval.get("gmail_message_id") != args.message_id:
        raise ValueError("rendering approval does not match this estimate and message")
    images = _rendering_images(paths)
    expected = {item["slot"]: item["sha256"] for item in approval.get("images", [])}
    if len(images) != len(expected) or any(_sha256_file(img) != expected.get(i) for i, img in enumerate(images, start=1)):
        raise ValueError("rendering images changed since the owner approved them")
    body = Path(paths["customer_reply"])
    shop_now = (read_object(p["shop_profile"]).get("shop") or {}).get("name") or "the shop"
    held = _read_held(p, "rendering", args.estimate_id, args.message_id)
    if held and held.get("body"):
        body.write_text(str(held["body"]), encoding="utf-8")
        body_source = "held"
    elif body.exists() and body.read_text(encoding="utf-8").strip():
        body_source = "prepared"
    else:
        record_now = estimate_record.read_object(estimate_record.record_path(p["record_root"], args.estimate_id))
        text, body_source = _draft_customer_email(
            p, record_now, args.message_id, "rendering",
            {"piece": approval.get("piece") or "the piece", "shop name": shop_now},
            RENDERING_NOTE.format(piece=approval.get("piece") or "the piece", shop=shop_now), args,
        )
        body.write_text(text, encoding="utf-8")
    if getattr(args, "hold", False):
        # The booking (or the price) from the same email is still open: the pictures wait for it (the owner, 9 September 2026).
        _hold_email(p, "rendering", args.estimate_id, args.message_id, body.read_text(encoding="utf-8"))
        token = inbox_claim.authoritative_claim_token(p["claim_root"], args.message_id)
        inbox_monitor.park_item(p["monitor_root"], args.message_id, p["claim_root"], token, "rendering_approval")
        result = {"outcome": "rendering_held", "images": len(images), "email": body_source,
                  "note": "the renderings go out with the other card's email"}
        _report_brief(args, result, runner)
        return result
    partner = _partner_entry(p, args)
    if partner is not None:
        merged, _extra = _bundle_with_partner(p, partner, "rendering", body.read_text(encoding="utf-8"), shop_now)
        body.write_text(merged, encoding="utf-8")
    body_text = body.read_text(encoding="utf-8")  # the send closes the claim and cleans its folder
    record = send_rendering(argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        message_id=args.message_id, estimate_id=args.estimate_id, body=body, images=images,
        gmail_payload=Path(paths["gmail_payload"]), provider_response=Path(paths["gmail_provider_response"]),
        record_output=Path(paths["current_record"]), approved_rendering=approval,
    ))
    if partner is not None:
        delivery = next((d for d in reversed(record.get("rendering_deliveries") or []) if isinstance(d, dict)
                         and d.get("source_message_id_sha256") == estimate_record.sha256_text(args.message_id)), {})
        receipt = {"id": delivery.get("provider_message_id"), "threadId": delivery.get("thread_id")}
        record = _record_partner_delivery(p, partner, body_text, images, receipt, runner)
    result = {"outcome": "rendering_sent", "images": len(images), "record_status": record.get("status"), "email": body_source,
              **({"bundled_with": partner["kind"]} if partner else {})}
    _report_brief(args, result, runner, repeat=bool(getattr(args, "released", False)))
    return result


RESCHEDULE_NOTE = (
    "Hello,\n\nDone, we have moved your visit to {when} at {shop}. The earlier invitation is cancelled and a "
    "new calendar invitation is on its way to this address.\n\nWe will go over the design for {piece} together. "
    "If this time stops working for you, reply here and we will find another.\n\n{shop}\n"
)

CONFIRMATION_NOTE = (
    "Hello,\n\nYou are booked: {when} at {shop}. A calendar invitation is on its way to this address; "
    "please accept it so it lands on your calendar.\n\nWe will go over the design for {piece} together. "
    "If the time stops working for you, reply here and we will find another.\n\n{shop}\n"
)


HELD_EMAIL_FILE = "held-email.json"


def _booking_work_dir(p: dict[str, Path], message_id: str) -> Path:
    return p["monitor_root"].resolve().parent / "work" / f"booking-{inbox_claim.claim_key(message_id)[:16]}"


def _claim_work_dir(p: dict[str, Path], message_id: str) -> Path:
    """The claim's work folder without touching the claim (a parked claim keeps it)."""
    return p["monitor_root"].resolve().parent / "work" / inbox_monitor.message_key(message_id)


def _held_email_path(p: dict[str, Path], kind: str, estimate_id: str, message_id: str) -> Path:
    """Where a held send keeps its email until its partner card is decided (the owner, 9 September 2026)."""
    if kind == "appointment":
        return _booking_work_dir(p, message_id) / HELD_EMAIL_FILE
    if kind == "rendering":
        return _claim_work_dir(p, message_id) / HELD_EMAIL_FILE
    if kind == "price":
        return estimate_work_dir(p["monitor_root"], estimate_id, message_id) / HELD_EMAIL_FILE
    raise ValueError("unsupported held kind")


def _hold_email(p: dict[str, Path], kind: str, estimate_id: str, message_id: str, body: str, **extra: Any) -> Path:
    path = _held_email_path(p, kind, estimate_id, message_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(path, {"kind": kind, "estimate_id": estimate_id, "message_id": message_id, "body": body,
                         "held_at": datetime.now(timezone.utc).isoformat(), **extra})
    return path


def _read_held(p: dict[str, Path], kind: str, estimate_id: str, message_id: str) -> dict[str, Any] | None:
    path = _held_email_path(p, kind, estimate_id, message_id)
    if not path.exists():
        return None
    try:
        return read_object(path)
    except (OSError, ValueError):
        return None


def _partner_entry(p: dict[str, Path], args: argparse.Namespace) -> dict[str, Any] | None:
    """The held partner card named by --with-held, from the registry."""
    partner_id = getattr(args, "with_held", None)
    if not partner_id:
        return None
    entry = brief_registry.load(p["monitor_root"], partner_id)
    if entry is None:
        raise ValueError(f"no card {partner_id} on file to bundle with")
    if entry.get("outcome") != "held":
        raise ValueError(f"card {partner_id} is {entry.get('outcome')}, not held")
    return entry


def _held_rendering_images(p: dict[str, Path], message_id: str) -> list[Path]:
    """The very images on the held rendering card, verified against its approval."""
    work_dir = _claim_work_dir(p, message_id)
    approval = read_object(work_dir / "rendering-approval.json")
    images = [work_dir / f"rendering-{slot}.png" for slot in range(1, 5) if (work_dir / f"rendering-{slot}.png").exists()]
    expected = {item["slot"]: item["sha256"] for item in approval.get("images", [])}
    if len(images) != len(expected) or any(_sha256_file(img) != expected.get(i) for i, img in enumerate(images, start=1)):
        raise ValueError("rendering images changed since the owner approved them")
    return images


def _record_partner_delivery(p: dict[str, Path], partner: dict[str, Any], body: str, images: list[Path],
                             delivery: dict[str, Any], runner: Any) -> dict[str, Any]:
    """After a bundled send, the held partner's own effect goes on the record as if it had sent the email."""
    kind, estimate_id, message_id = partner["kind"], partner["estimate_id"], partner["message_id"]
    if kind == "rendering":
        record = estimate_record.record_rendering_sent(p["record_root"], estimate_id, message_id, body, images, delivery)
        if _claim_parked(p, message_id):
            _resume_parked_claim(p, message_id)
        finish_processed(p["monitor_root"], p["claim_root"], p["record_root"], message_id)
        outcome = {"outcome": "rendering_sent", "images": len(images), "bundled": True}
    elif kind == "price":
        work_dir = estimate_work_dir(p["monitor_root"], estimate_id, message_id)
        approved = read_object(work_dir / "approved.json")
        current = estimate_record.current_approval_state(p["record_root"], estimate_id)
        valid, errors = approval_guard.verify_execution(approved, current)
        if not valid:
            raise ValueError("approval verification failed: " + "; ".join(errors))
        record = estimate_record.record_estimate_sent(p["record_root"], estimate_id, message_id, approved, current, delivery)
        outcome = {"outcome": "estimate_sent", "price": approved.get("owner_approved_price"), "bundled": True}
    elif kind == "appointment":
        held = _read_held(p, "appointment", estimate_id, message_id) or {}
        chosen = held.get("chosen") or {}
        record = estimate_record.record_appointment_booked(p["record_root"], estimate_id, {
            "estimate_id": estimate_id, "source_message_id": message_id, "calendar_event_id": held.get("event_id"),
            "confirmed_start": chosen.get("start"), "confirmed_end": chosen.get("end"),
            "confirmation_message_id": delivery["id"], "confirmation_thread_id": delivery["threadId"],
        })
        _supersede_reject_question(p, estimate_id, message_id, "the card was approved and the time booked")
        outcome = {"outcome": "appointment_booked", "confirmed_start": chosen.get("start"), "bundled": True}
    else:
        raise ValueError("unsupported partner kind")
    brief_registry.mark(p["monitor_root"], partner["brief_id"], "executed", "sent in one email with its partner card")
    _report_brief(argparse.Namespace(brief_id=partner["brief_id"]), outcome, runner, repeat=True)  # reported once when held
    return record


def _bundle_with_partner(p: dict[str, Path], partner: dict[str, Any], own_kind: str, own_body: str, shop: str) -> tuple[str, list[Path]]:
    """The one email: the confirmation first, the pictures or the price after it; the partner's images ride along."""
    held = _read_held(p, partner["kind"], partner["estimate_id"], partner["message_id"])
    if held is None:
        raise ValueError(f"card {partner['brief_id']} is held but its email is missing; run the doctor")
    partner_body = str(held.get("body") or "")
    if partner["kind"] == "appointment":
        body = customer_mail.merge_bodies(partner_body, own_body, shop)
    elif own_kind == "appointment":
        body = customer_mail.merge_bodies(own_body, partner_body, shop)
    else:
        body = customer_mail.merge_bodies(partner_body, own_body, shop)
    images: list[Path] = []
    if partner["kind"] == "rendering":
        images = _held_rendering_images(p, partner["message_id"])
    elif partner["kind"] == "price":
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], partner["estimate_id"]))
        work_dir = estimate_work_dir(p["monitor_root"], partner["estimate_id"], partner["message_id"])
        for item in (record.get("concierge") or {}).get("renderings") or []:
            image = work_dir / f"rendering-{int(item['slot'])}.png"
            if image.is_file() and _sha256_file(image) == item.get("sha256"):
                images.append(image)
    return body, images


def book_approved_appointment(args: argparse.Namespace) -> dict[str, Any]:
    """The owner approved a time: re-check it, book it, confirm it, record it. One command."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], args.estimate_id))
    route_ownership.validate_record(record)
    runner = getattr(args, "runner", subprocess.run)
    existing = record.get("appointment_booked") if isinstance(record.get("appointment_booked"), dict) else None
    approval = read_object(approval_store_path(p["monitor_root"], args.estimate_id, args.message_id))
    if approval.get("estimate_id") != args.estimate_id or approval.get("source_message_id") != args.message_id:
        raise ValueError("appointment approval does not match this estimate and message")
    source_hash = estimate_record.sha256_text(args.message_id)
    if not any(
        isinstance(item, dict) and item.get("source_message_id_sha256") == source_hash
        for item in record.get("appointment_approval_requests", [])
    ):
        raise ValueError("record has no approval request for this message")
    options = approval.get("calendar_availability") or []
    if getattr(args, "start", None):
        chosen = next((o for o in options if o["start"] == args.start), None)
        if chosen is None:
            raise ValueError("--start must be one of the options the owner saw")
    else:
        index = int(getattr(args, "option", 1) or 1)
        if not 1 <= index <= len(options):
            raise ValueError(f"option must be between 1 and {len(options)}")
        chosen = options[index - 1]
    if existing and existing.get("confirmed_start") == chosen["start"]:
        result = {"outcome": "already_booked", "confirmed_start": existing.get("confirmed_start")}
        _report_brief(args, result, runner, repeat=True)
        return result
    profile = read_object(p["shop_profile"])
    scheduling = profile.get("scheduling") or {}
    calendar_id = scheduling.get("calendar")
    if not calendar_id:
        raise ValueError("no calendar configured in the shop profile")
    import gateway_token  # local import: only needed here

    # Everything that can refuse, before the calendar is touched: a claim
    # parked behind another card (a rendering filed from the same email) is
    # fine, but a claim in no usable state must fail here, not after an
    # event exists that nothing records.
    if inbox_claim.claim_path(p["claim_root"], args.message_id).exists():
        inbox_claim.authoritative_claim_token(p["claim_root"], args.message_id, allow_processed=True, allow_parked=True)
    token = gateway_token.load_token()
    opener = getattr(args, "opener", None)
    kwargs = {"opener": opener} if opener else {}
    outside = slots.outside_windows(scheduling, [chosen])
    if outside:
        raise ValueError("the approved time is outside the declared consultation windows; ask the desk for new options")
    shop = (profile.get("shop") or {}).get("name") or "the shop"
    piece = approval.get("piece") or "your piece"
    work_dir = p["monitor_root"].resolve().parent / "work" / f"booking-{inbox_claim.claim_key(args.message_id)[:16]}"
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    event_path = work_dir / "calendar-event.json"
    saved = read_object(event_path) if event_path.exists() else {}
    description = f"Design consultation for {piece}. Estimate {args.estimate_id.upper()}."
    event = None
    if isinstance(saved, dict) and saved.get("desk_slot_start") == chosen["start"]:
        if saved.get("id"):
            # An earlier run created the event and died before recording it;
            # the slot is already ours, so use that event rather than book twice.
            event = saved
        else:
            # An earlier run was killed inside the create call. The calendar
            # knows whether it went through: adopt the desk's own event by
            # the estimate id it wrote in the description, never by time alone.
            mark = f"Estimate {args.estimate_id.upper()}."
            for candidate in calendar_query.list_events(calendar_id, chosen["start"], chosen["end"], token, **kwargs):
                if mark in str(candidate.get("description") or ""):
                    event = candidate
                    write_private(event_path, {**event, "desk_slot_start": chosen["start"]})
                    break
    if event is None:
        # Approval does not make stale availability current.
        receipt = calendar_query.query_freebusy(
            chosen["start"], chosen["end"], scheduling.get("timezone") or "UTC", calendar_id, token, **kwargs
        )
        busy = receipt["response_body"]["calendars"][calendar_id].get("busy", [])
        if busy:
            raise ValueError("the approved time is no longer free; ask the desk for new options")
        # Write-ahead: the slot is journaled before the call, so a kill inside
        # the call leaves a mark the next run can check against the calendar.
        write_private(event_path, {"desk_slot_start": chosen["start"], "started_at": datetime.now(timezone.utc).isoformat()})
        event = calendar_query.create_event(
            calendar_id, chosen["start"], chosen["end"], scheduling.get("timezone") or "UTC",
            f"{shop}: design consultation, {piece}"[:200], description,
            record["route"]["recipient"], token, **kwargs,
        )
        write_private(event_path, {**event, "desk_slot_start": chosen["start"]})
    cancelled_old = None
    if existing:
        # Rescheduling: the new event is in; take the old one off the calendar.
        cancelled_old = calendar_query.delete_event(calendar_id, existing.get("calendar_event_id", ""), token, **kwargs) \
            if existing.get("calendar_event_id") else False
        fixed = RESCHEDULE_NOTE.format(when=chosen["label"], shop=shop, piece=piece)
        kind = "reschedule"
    else:
        fixed = CONFIRMATION_NOTE.format(when=chosen["label"], shop=shop, piece=piece)
        kind = "confirmation"
    prepared = prepared_email_path(approval_store_path(p["monitor_root"], args.estimate_id, args.message_id))
    if kind == "confirmation" and prepared.exists() and chosen["label"] in prepared.read_text(encoding="utf-8"):
        body, body_source = prepared.read_text(encoding="utf-8"), "prepared"
    else:
        body, body_source = _draft_customer_email(p, record, args.message_id, kind, {
            "piece": piece, "time_labels": [chosen["label"]], "shop name": shop,
            "previous time (now cancelled)": existing.get("confirmed_start") if existing else "",
            **({"the visit": "the visit is to design your perfect piece together (write to the customer as you); never mention an estimate or a quote"}
               if record.get("status") == "awaiting_specs" else {}),
            **_inventory_fact(record),
        }, fixed, args)
    held = _read_held(p, "appointment", args.estimate_id, args.message_id)
    if held and held.get("body") and held.get("chosen", {}).get("start") == chosen["start"]:
        body, body_source = str(held["body"]), "held"  # the email drafted when the time was booked and the send held
    if getattr(args, "hold_email", False):
        # The partner card (a rendering, a price) from the same email is still open: the time is on the calendar,
        # the confirmation waits so both go out as one email (the owner, 9 September 2026).
        _hold_email(p, "appointment", args.estimate_id, args.message_id, body, chosen=chosen, event_id=event["id"], kind_of_email=kind)
        result = {"outcome": "appointment_booked_email_held", "confirmed_start": chosen["start"], "calendar_event_id": event["id"],
                  "email": body_source, "note": "the time is on your calendar; the confirmation goes out with the other card's email"}
        _report_brief(args, result, runner)
        return result
    partner = _partner_entry(p, args)
    images: list[Path] = []
    if partner is not None:
        body, images = _bundle_with_partner(p, partner, "appointment", body, shop)
    customer_content_guard.validate_customer_text(body)
    payload_path, response_path = work_dir / "gmail-payload.json", work_dir / "gmail-provider-response.json"
    _reuse_or_build_payload(payload_path, lambda: gmail_reply.build_reply(record["route"], body, images or None))
    delivery = gmail_safe.send_reply_claimed(
        p["claim_root"], args.message_id, None,
        f"appointment_confirmation:{args.estimate_id}:{args.message_id}",
        payload_path, response_path, token, runner=runner, allow_processed_claim=True, allow_parked_claim=True,
    )
    booked = estimate_record.record_appointment_booked(p["record_root"], args.estimate_id, {
        "estimate_id": args.estimate_id,
        "source_message_id": args.message_id,
        "calendar_event_id": event["id"],
        "confirmed_start": chosen["start"],
        "confirmed_end": chosen["end"],
        "confirmation_message_id": delivery["id"],
        "confirmation_thread_id": delivery["threadId"],
    })
    mirror_record(booked, work_dir / "current-record.json")
    _supersede_reject_question(p, args.estimate_id, args.message_id, "the card was approved and the time booked")
    if partner is not None:
        booked = _record_partner_delivery(p, partner, body, images, delivery, runner)
        mirror_record(booked, work_dir / "current-record.json")
    result = {"outcome": "appointment_rescheduled" if existing else "appointment_booked",
              "confirmed_start": chosen["start"], "calendar_event_id": event["id"], "record_status": booked.get("status"),
              "email": body_source, **({"bundled_with": partner["kind"]} if partner else {})}
    if existing:
        result["previous_start"] = existing.get("confirmed_start")
        result["previous_event_cancelled"] = bool(cancelled_old)
    _report_brief(args, result, runner, repeat=bool(getattr(args, "released", False)))
    return result


OFFER_NOTE = (
    "Hello,\n\nHappy to set up a time to go over the design for {piece} together. Here is what is open on "
    "our side:\n\n{lines}\n\nReply with the one that works and we will lock it in. If none of these fit, "
    "tell us what does and we will find something.\n\n{shop}\n"
)

OFFER_NOTE_OUTSIDE_HOURS = (
    "Hello,\n\nHappy to set up a time to go over the design for {piece} together. We take design "
    "consultations {hours}, so {asked} falls outside our hours. Here is what is open on our side:\n\n{lines}\n\n"
    "Reply with the one that works and we will lock it in. If none of these fit, tell us what does within "
    "those hours and we will find something.\n\n{shop}\n"
)


_ASK_STOP = {"which", "would", "you", "like", "what", "your", "the", "a", "an", "or", "do", "have", "could", "confirm", "that", "rather", "than", "one", "with"}


def _asks_covered(body: str, asks: list[str]) -> bool:
    """Every promised question is in the email: at least half of each question's distinctive words appear (live, 9 Sep 2026: a draft dropped one)."""
    text = re.sub(r"[^a-z0-9 ]+", " ", str(body or "").lower())
    for ask in asks:
        words = [w for w in re.findall(r"[a-z0-9]+", str(ask).lower()) if w not in _ASK_STOP and len(w) > 2]
        if not words:
            continue
        hits = sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"s?\b", text))
        if hits * 2 < len(words):
            return False
    return True


def _offer_facts(approval: dict[str, Any], piece: str, labels: list[str], shop: str,
                 understanding: str | None = None) -> tuple[dict[str, Any], str]:
    """The facts and the fixed text for an offer email; a time outside the hours is said plainly, with the hours.

    With questions to ask and a photo on the record, the customer's vision is
    confirmed first, the way a jeweler would say it (the owner, 9 September 2026).
    """
    lines = "\n".join(f"- {l}" for l in labels)
    outside = [str(o) for o in (approval.get("outside_hours") or []) if str(o).strip()]
    hours = str(approval.get("hours") or "").strip()
    facts: dict[str, Any] = {"piece": piece, "time_labels": labels, "shop name": shop}
    if outside and hours:
        facts["consultation hours"] = hours
        facts["the time they asked for is outside those hours"] = "; ".join(outside)
        fixed = OFFER_NOTE_OUTSIDE_HOURS.format(piece=piece, hours=hours, asked="; ".join(outside), lines=lines, shop=shop)
    else:
        fixed = OFFER_NOTE.format(piece=piece, lines=lines, shop=shop)
    asks = [str(q).strip() for q in (approval.get("ask_for") or []) if str(q).strip()]
    if asks:
        # The customer also asked for a price: the same email asks the details the estimate needs (8 September 2026).
        # In concierge mode the questions are budget and timeframe, introduced as such (the jeweler, 9 September 2026).
        facts["details to ask for the estimate, one bullet each, plain questions"] = asks
        questions = "\n".join(f"- {q}" for q in asks)
        vision = ""
        if understanding:
            facts["their vision from their photo and words, confirm it first in one sentence the way a jeweler would"] = understanding
            vision = pipeline_understanding_line(understanding) + "\n\n"
        intro = str(approval.get("ask_intro") or "").strip()
        if intro:
            facts["how to introduce the questions"] = intro
            lead = f"{intro}\n\n{questions}\n\nIf you are not sure about either, no problem at all; we can work it out when we talk."
        else:
            lead = (f"Since you asked about the price as well, to get the estimate started could you tell me:\n\n{questions}\n\n"
                    "If you are not sure about any of it, say so and I will suggest what usually looks best.")
        fixed = fixed.replace(f"\n\n{shop}\n", f"\n\n{vision}{lead}\n\n{shop}\n")
    return facts, fixed


def pipeline_understanding_line(understanding: str) -> str:
    import pipeline  # local import: pipeline imports this module

    return pipeline.UNDERSTANDING_LINE.format(vision=understanding)


def _send_times(p: dict[str, Path], record: dict[str, Any], message_id: str, options: list[dict[str, Any]],
                piece: str, runner: Any, label: str, args: argparse.Namespace | None = None) -> dict[str, Any]:
    profile = read_object(p["shop_profile"])
    shop = (profile.get("shop") or {}).get("name") or "the shop"
    labels = [o.get("label") or o["start"] for o in options]
    store = approval_store_path(p["monitor_root"], record["estimate_id"], message_id)
    approval_now = read_object(store) if store.exists() else {}
    understanding = estimate_record.vision_in_words(record.get("specification"), on_file=estimate_record.prior_basis(record)) \
        if approval_now.get("ask_for") else None
    facts, fixed = _offer_facts(approval_now, piece, labels, shop, understanding)
    prepared = prepared_email_path(store)
    if prepared.exists() and all(l in prepared.read_text(encoding="utf-8") for l in labels):
        body, body_source = prepared.read_text(encoding="utf-8"), "prepared"
    else:
        body, body_source = _draft_customer_email(p, record, message_id, "offer", facts, fixed, args or argparse.Namespace())
    if judge.bench_measurement_questions(body):
        body, body_source = fixed, "fallback"  # never a technical question to a customer
    if not _asks_covered(body, list(approval_now.get("ask_for") or []) + ([understanding] if understanding else [])):
        body, body_source = fixed, "fallback"  # the card promised these questions; the fixed text asks them all
    customer_content_guard.validate_customer_text(body)
    work_dir = p["monitor_root"].resolve().parent / "work" / f"offer-{inbox_claim.claim_key(message_id)[:16]}-{label}"
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload_path, response_path = work_dir / "gmail-payload.json", work_dir / "gmail-provider-response.json"
    _reuse_or_build_payload(payload_path, lambda: gmail_reply.build_reply(record["route"], body))
    delivery = gmail_safe.send_reply_claimed(
        p["claim_root"], message_id, None, f"times_offered:{record['estimate_id']}:{message_id}:{label}",
        payload_path, response_path, gateway_token.load_token(), runner=runner, allow_processed_claim=True,
        allow_parked_claim=True,
    )
    updated = estimate_record.record_times_offered(p["record_root"], record["estimate_id"], message_id, options, delivery)
    if approval_now.get("ask_for"):
        # The questions went with the times: the record knows the ask was made, so a reply is judged against it.
        if (record.get("route") or {}).get("gmail_message_id") == message_id and not record.get("spec_gate_reply"):
            updated = estimate_record.record_spec_gate_sent(p["record_root"], record["estimate_id"], body, delivery)
        else:
            updated = estimate_record.record_followup_sent(p["record_root"], record["estimate_id"], message_id, body, delivery)
    mirror_record(updated, work_dir / "current-record.json")
    return {"outcome": "times_offered", "options": labels, "provider_message_id": delivery["id"], "email": body_source,
            "asked": list(approval_now.get("ask_for") or [])}


def send_approved_times(args: argparse.Namespace) -> dict[str, Any]:
    """The owner approved the offer: email those times to the customer. One command."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    runner = getattr(args, "runner", subprocess.run)
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], args.estimate_id))
    route_ownership.validate_record(record)
    approval = read_object(approval_store_path(p["monitor_root"], args.estimate_id, args.message_id))
    if approval.get("estimate_id") != args.estimate_id or approval.get("source_message_id") != args.message_id:
        raise ValueError("appointment approval does not match this estimate and message")
    if approval.get("action_type") != "appointment_offer":
        raise ValueError("this approval is a booking, not an offer; run book-approved-appointment")
    source_hash = estimate_record.sha256_text(args.message_id)
    for existing in record.get("times_offered") or []:
        if isinstance(existing, dict) and existing.get("source_message_id_sha256") == source_hash:
            result = {"outcome": "already_offered", "options": [o.get("label") for o in existing.get("options", [])]}
            _report_brief(args, result, runner, repeat=True)
            return result
    options = approval.get("calendar_availability") or []
    if not options:
        raise ValueError("this card had no times to offer; reject it and answer the desk's question")
    result = _send_times(p, record, args.message_id, options, approval.get("piece") or "your piece", runner, "approved", args)
    _supersede_reject_question(p, args.estimate_id, args.message_id, "the card was approved and the times offered")
    _report_brief(args, result, runner)
    return result


def _profile_for(args: argparse.Namespace) -> dict[str, Any]:
    """The shop profile: the flag when given, else the workspace's own copy."""
    path = getattr(args, "shop_profile", None) or workspace_of(args.monitor_root) / "estimate-desk" / "shop-profile.json"
    try:
        return read_object(Path(path))
    except (OSError, ValueError):
        return {}


def _appointment_next_text(record: dict[str, Any], approval: dict[str, Any]) -> str:
    customer = kolo_safe._sender_display(record["route"]["recipient"])
    piece = approval.get("piece") or "their piece"
    asked = "; ".join(approval.get("requested_times") or []) or "no particular time"
    what = "booking" if approval.get("action_type") == "appointment_booking" else "offering those times"
    return (
        f"You passed on {what} for {customer} ({piece}; they asked for {asked}). What would you like to do? "
        "Reply with times to offer them (for example \"Tuesday 2pm or Wednesday at 11\"), "
        "\"other times\" and I will pick new free ones, or \"handle myself\" and I will leave the thread to you."
    )


def _appointment_ask_text(record: dict[str, Any], approval: dict[str, Any], reason: str) -> str:
    """The same question, asked without a card: the calendar offered nothing."""
    customer = kolo_safe._sender_display(record["route"]["recipient"])
    piece = approval.get("piece") or "their piece"
    asked = "; ".join(approval.get("requested_times") or []) or "no particular time"
    return (
        f"{customer} asked to meet about {piece} (they asked for {asked}), but I have no time to offer: {reason}. "
        "What would you like to do? Reply with times to offer them (for example \"Tuesday 2pm or Wednesday at 11\"), "
        "\"other times\" and I will look again, or \"handle myself\" and I will leave the thread to you."
    )


def _supersede_reject_question(p: dict[str, Path], estimate_id: str, message_id: str, why: str) -> None:
    root = owner_questions.questions_root(p["monitor_root"])
    qid = owner_questions.question_id(estimate_id, "appointment_next", message_id)
    try:
        question = owner_questions.load(root, qid)
    except (OSError, ValueError):
        return
    owner_questions.supersede(root, question, why)


def appointment_rejected(args: argparse.Namespace) -> dict[str, Any]:
    """The owner rejected an appointment card: ask them what to do for this customer.

    Only reachable if Kolo delivers the rejection; otherwise the owner replies
    with the code from the card and the same dormant question answers.
    """
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    runner = getattr(args, "runner", subprocess.run)
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], args.estimate_id))
    route_ownership.validate_record(record)
    approval = read_object(approval_store_path(p["monitor_root"], args.estimate_id, args.message_id))
    root = owner_questions.questions_root(p["monitor_root"])
    _created, question = owner_questions.create_decision(
        root, "appointment_next", args.estimate_id, args.message_id, _appointment_next_text(record, approval),
        {"rejected_action": approval.get("action_type"), "rejected_options": approval.get("calendar_availability") or []},
    )
    question["dormant"] = False
    owner_questions.save(root, question)
    question = _attach_answer_command(root, p["monitor_root"], question)
    question = owner_questions.deliver(root, question, runner=runner, extra_args=kolo_safe.owner_channel_args(p["monitor_root"]))
    brief_id = getattr(args, "brief_id", None)
    return {"outcome": "owner_asked", "question_id": question["question_id"], "reference": owner_questions.reference(question["question_id"]),
            "brief_id": brief_id}


def _answer_appointment_next(args: argparse.Namespace, workspace: Path, p: dict[str, Path], root: Path,
                             question: dict[str, Any], outcome: str) -> dict[str, Any]:
    """Apply the owner's answer after a rejected appointment card.

    Times the owner names, or "other times", become a fresh offer card: the
    customer hears nothing until that card is approved. "Handle myself"
    leaves the thread to the owner.
    """
    import inbox_watcher  # local import: inbox_watcher imports this module

    runner = getattr(args, "runner", subprocess.run)
    message_id = _question_message_id(question)
    estimate_id = question["estimate_id"]
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"], "kind": "appointment_next", "decision": outcome}
    if outcome == "handle_myself":
        if _claim_parked(p, message_id):
            _close_parked_claim(p, message_id, "owner_decided_handle_myself")
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result["note"] = "the desk leaves this thread to the owner"
        return result
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
    route_ownership.validate_record(record)
    profile = read_object(p["shop_profile"])
    scheduling = profile.get("scheduling") or {}
    zone_name = scheduling.get("timezone") or "UTC"
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    now = _dt.now(ZoneInfo(zone_name))
    import pipeline  # local import: pipeline imports this module

    switch = pipeline.settings(workspace / "estimate-desk")
    judge_runner = getattr(args, "judge_runner", subprocess.run)
    openclaw = args.openclaw or inbox_watcher.default_openclaw()
    requested: list[str] = []
    if outcome == "times_given":
        judged = judge.resolve_owner_times(args.answer, now.strftime("%A %Y-%m-%d %H:%M"), zone_name,
                                           switch.get("model"), judge_runner, openclaw)
        requested = judged.get("resolved_times", [])
        if not requested:
            raise ValueError("could not read a specific day and time from that reply; give a day and a clock time")
    opener = getattr(args, "opener", None)
    round_no = len([q for q in owner_questions.list_questions(root) if q["kind"] == "appointment_next"
                    and q["estimate_id"] == estimate_id and q["status"] == "answered"]) + 1
    out_dir = p["monitor_root"].resolve().parent / "work" / f"offer-{inbox_claim.claim_key(message_id)[:16]}-round{round_no}"
    offered = slots.offer_times(profile, gateway_token.load_token(), out_dir, now=now, opener=opener,
                                requested=requested, force_offer=True)
    options = offered["options"]
    if outcome == "times_given":
        wanted = {r for r in requested}
        options = [o for o in options if o["start"][:16] in wanted] or options
    if not options:
        raise ValueError("none of those times are free inside the declared windows; try others")
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their piece"
    intent = {
        "requested_times": [f"owner: {args.answer.strip()[:120]}"],
        "calendar_availability": [{"start": o["start"], "end": o["end"], "label": o["label"]} for o in options[:3]],
        "mode": "offer",
        "availability_note": "times you chose after passing on the last card",
    }
    approval = _appointment_approval_details(record, message_id, intent, p["monitor_root"])
    approval["source_message_id"] = message_id
    # A second card for the same message: keep it distinct from the first one.
    store = approval_store_path(p["monitor_root"], estimate_id, message_id)
    write_private(store, approval)
    qroot = root
    _created, dormant = owner_questions.create_decision(
        qroot, "appointment_next", estimate_id, f"{message_id}#round{round_no}", _appointment_next_text(record, approval),
        {"rejected_action": "appointment_offer", "rejected_options": approval["calendar_availability"]}, dormant=True,
    )
    _attach_answer_command(qroot, p["monitor_root"], dormant)
    approver = activation_binding.load(activation_binding.binding_path(p["monitor_root"]))
    approval_path = out_dir / "appointment-approval.json"
    write_private(approval_path, approval)
    parked = _claim_parked(p, message_id)
    if parked:
        # The question was asked without a card (the calendar offered
        # nothing), so this is the message's first card: file it under the
        # resumed claim and on the record exactly as the tick would have, and
        # end the claim processed, still the desk's, for the card's executor.
        # Closing it as a manual review instead (before 4.10.3) made the
        # executor refuse the approval (6 September 2026, Brief #26).
        _resume_parked_claim(p, message_id)
        kolo_safe.request_appointment_approval_claimed(
            p["claim_root"], message_id, None, f"appointment_approval:{estimate_id}:{message_id}",
            estimate_id, approval_path, approver["session_key"], runner=runner,
        )
        estimate_record.record_appointment_approval_requested(p["record_root"], estimate_id, message_id, approval)
    else:
        kolo_safe.run_command(
            kolo_safe.build_request_appointment_approval(estimate_id, approval_path, approver["session_key"]),
            runner=runner,
        )
    _rows, _reasoning, title = kolo_safe.appointment_card(approval, estimate_id)
    _register_brief(p["monitor_root"], "appointment", title, estimate_id, message_id, runner)
    if parked:
        finish_processed(p["monitor_root"], p["claim_root"], p["record_root"], message_id)
    result.update({"outcome": "offer_card_filed", "options": [o["label"] for o in options[:3]], "piece": piece})
    return result

def _draft_customer_email(p: dict[str, Path], record: dict[str, Any], message_id: str, kind: str,
                          facts: dict[str, Any], fallback: str, args: argparse.Namespace) -> tuple[str, str]:
    """Write the email for this thread, or fall back to the fixed text."""
    import inbox_watcher  # local import: inbox_watcher imports this module
    import pipeline  # local import: pipeline imports this module

    profile = read_object(p["shop_profile"])
    switch = pipeline.settings(p["monitor_root"].resolve().parent.parent / "estimate-desk")
    judge_runner = getattr(args, "judge_runner", subprocess.run)
    openclaw = getattr(args, "openclaw", None) or inbox_watcher.default_openclaw()
    try:
        digest = customer_mail.fetch_thread_digest(
            record, message_id, (profile.get("shop") or {}).get("outbound_mailbox"), gateway_token.load_token(),
            opener=getattr(args, "opener", None),
        )
    except Exception:  # noqa: BLE001 - a missing thread only costs the draft its context
        digest = {"messages": []}
    try:
        return customer_mail.draft(kind, facts, digest, profile, fallback, switch.get("model"), judge_runner, openclaw,
                                   customer_name=estimate_record.customer_first_name(record))
    except Exception:  # noqa: BLE001 - the fixed text always goes out
        return fallback, "fallback"


def estimate_work_dir(monitor_root: Path, estimate_id: str, message_id: str | None) -> Path:
    """One folder per (estimate, approval source message): a re-price later never reuses old mail."""
    key = inbox_claim.claim_key(message_id)[:16] if message_id else "none"
    return monitor_root.resolve().parent / "work" / f"estimate-{estimate_id}-{key}"


def prepared_email_path(store: Path) -> Path:
    return store.with_name(store.name.replace(".json", "") + ".email.txt")


def _prepare_email(p: dict[str, Path], record: dict[str, Any], message_id: str, kind: str, facts: dict[str, Any],
                   fallback: str, target: Path, digest: dict[str, Any] | None, runner: Any) -> str:
    """Write the customer email at filing time so approval only sends (seconds, not a draft)."""
    import inbox_watcher  # local import: inbox_watcher imports this module
    import pipeline  # local import: pipeline imports this module

    try:
        profile = read_object(p["shop_profile"])
        switch = pipeline.settings(p["monitor_root"].resolve().parent.parent / "estimate-desk")
        body, source = customer_mail.draft(
            kind, facts, digest or {"messages": []}, profile, fallback, switch.get("model"), runner,
            inbox_watcher.default_openclaw(),
        )
    except Exception:  # noqa: BLE001 - the fixed text is always available
        body, source = fallback, "fallback"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(body, encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return source


def _digest_from_work(paths: dict[str, str], message_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    thread_path = Path(paths["gmail_thread"]) if paths.get("gmail_thread") else None
    if thread_path and thread_path.exists():
        return gmail_text.thread_digest(read_object(thread_path), message_id, (profile.get("shop") or {}).get("outbound_mailbox"))
    return {"messages": []}


def _reuse_or_build_payload(path: Path, build: Any) -> dict[str, Any]:
    """A retry must send the very payload it journaled: same bytes, same binding.

    A reply payload carries a fresh Date and Message-ID each time it is built,
    so a second run after a crash would bind differently and be refused.
    The first build is kept beside the work and reused.
    """
    try:
        if path.exists():
            existing = read_object(path)
            if isinstance(existing, dict) and existing.get("raw"):
                return existing
    except (OSError, ValueError):
        pass
    payload = build()
    write_private(path, payload)
    return payload


EXECUTOR_COMMANDS = {
    "send-approved-estimate-brief", "send-approved-rendering", "book-approved-appointment",
    "send-approved-times", "appointment-rejected",
}
EXECUTOR_WHAT = {
    "send-approved-estimate-brief": "sending the estimate",
    "send-approved-rendering": "sending the renderings",
    "book-approved-appointment": "booking the appointment",
    "send-approved-times": "emailing the meeting times",
    "appointment-rejected": "handling the rejected appointment card",
}


LEASE_HELD_EXIT = 3


def _executor_key(args: argparse.Namespace) -> str:
    return inbox_claim.claim_key(getattr(args, "message_id", None) or getattr(args, "estimate_id", "") or "none")[:16]


def _command_failed(args: argparse.Namespace, argv: list[str], exc: BaseException) -> dict[str, Any]:
    """A card's command failed: mark the brief, ask the owner once, with the fix attached.

    RELIABILITY-PLAN.md 3.2. What the desk managed (a time held on the
    calendar) and what it did not (the email) are said in plain words; the
    replies are retry (the desk runs the same command again), release (the
    desk lets go of what it holds: its own calendar event), or handle myself.
    """
    import inbox_watcher  # local import: inbox_watcher imports this module

    # From the command line there is no runner; every Kolo call then goes
    # through the desk's one command helper, the same path the tick uses.
    runner = getattr(args, "runner", None) or (lambda argv, **_kw: kolo_safe.run_command(argv))
    error = str(exc)[:300]
    brief_id = getattr(args, "brief_id", None)
    if brief_id and brief_id != "<Brief ID>":
        try:
            kolo_safe.run_command(kolo_safe.build_update_brief(
                brief_id, "failed", {"command": args.command, "error": error}), runner=runner)
        except (OSError, ValueError, subprocess.CalledProcessError):
            pass
    p = inbox_watcher.paths_for(args.workspace.resolve())
    estimate_id = getattr(args, "estimate_id", None) or "jed-0000000000000000"
    try:
        record = estimate_record.read_object(estimate_record.record_path(p["record_root"], estimate_id))
        customer = kolo_safe._sender_display(record["route"]["recipient"])
    except (OSError, ValueError, KeyError):
        customer = "the customer"
    message_id = getattr(args, "message_id", None) or f"command:{args.command}"
    held = ""
    if args.command == "book-approved-appointment":
        event_path = (p["monitor_root"].resolve().parent / "work"
                      / f"booking-{inbox_claim.claim_key(getattr(args, 'message_id', '') or '')[:16]}" / "calendar-event.json")
        saved = read_object(event_path) if event_path.exists() else {}
        if isinstance(saved, dict) and saved.get("id"):
            held = " The time is held on your calendar; nothing was sent to the customer."
    text = (
        f"I could not finish {EXECUTOR_WHAT.get(args.command, args.command)} for {customer}: {error}.{held} "
        "Reply \"retry\" and I will run it again, \"release\" and I will let go of what I hold (a calendar hold, "
        "nothing already sent), or \"handle myself\"."
    )
    root = owner_questions.questions_root(p["monitor_root"])
    # A command can fail again after "retry": each failure is its own
    # question with its own code; a closed one is never reused.
    earlier = [q for q in owner_questions.list_questions(root)
               if q["kind"] == "command_failed" and q["gmail_message_id"].split("#")[0] == message_id
               and (q.get("context") or {}).get("command") == args.command and q["status"] != "open"]
    qid_message = message_id if not earlier else f"{message_id}#round{len(earlier) + 1}"
    created, question = owner_questions.create_decision(
        root, "command_failed", estimate_id, qid_message, text,
        {"argv": list(argv), "command": args.command, "error": error, "brief_id": brief_id, "source_message_id": message_id},
    )
    question = _attach_answer_command(root, p["monitor_root"], question)
    if created:
        owner_questions.deliver(root, question, runner=runner, extra_args=kolo_safe.owner_channel_args(p["monitor_root"]))
    return {"question_id": question["question_id"], "created": created}


def _answer_command_failed(args: argparse.Namespace, p: dict[str, Path], root: Path,
                           question: dict[str, Any], outcome: str) -> dict[str, Any]:
    context = question.get("context") or {}
    result: dict[str, Any] = {"outcome": "answered", "question_id": question["question_id"],
                              "kind": "command_failed", "decision": outcome}
    if outcome == "retry":
        argv = list(context.get("argv") or [])
        if not argv:
            raise ValueError("this question carries no command to retry")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv, _retry_of=question["question_id"])
        printed = buffer.getvalue().strip()
        if code != 0:
            raise ValueError("the retry failed again: " + (printed or "see the error above"))
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result["retried"] = json.loads(printed) if printed.startswith("{") else printed
        return result
    if outcome == "release":
        released = []
        if context.get("command") == "book-approved-appointment":
            key = inbox_claim.claim_key(_question_message_id(question))[:16]
            event_path = p["monitor_root"].resolve().parent / "work" / f"booking-{key}" / "calendar-event.json"
            saved = read_object(event_path) if event_path.exists() else {}
            record = estimate_record.read_object(estimate_record.record_path(p["record_root"], question["estimate_id"]))
            booked = (record.get("appointment_booked") or {}).get("calendar_event_id")
            if isinstance(saved, dict) and saved.get("id") and saved["id"] != booked:
                profile = read_object(p["shop_profile"])
                calendar_id = (profile.get("scheduling") or {}).get("calendar")
                calendar_query.delete_event(calendar_id, saved["id"], gateway_token.load_token())
                event_path.unlink(missing_ok=True)
                released.append(saved["id"])
        if question["status"] == "open":
            owner_questions.record_decision(root, question, args.answer, outcome)
        result["released"] = released
        return result
    if question["status"] == "open":
        owner_questions.record_decision(root, question, args.answer, outcome)
    result["note"] = "the desk leaves this to the owner"
    return result


def _report_brief(args: argparse.Namespace, result: dict[str, Any], runner: Any, repeat: bool = False) -> None:
    """Tell Kolo the brief is executed.

    `repeat` is for an outcome that was already reached by an earlier run
    (already sent, booked, offered): that run reported the brief, and Kolo
    refuses a second "executed" on the same brief (seen 6 September 2026 on
    Briefs #24 and #25), so the refusal is noted in the result, not raised.
    """
    brief_id = getattr(args, "brief_id", None)
    if not brief_id or brief_id == "<Brief ID>":
        return
    # Said in the output itself, because the main session keeps running update-brief after the line and
    # reporting the refusal as a fault (9 September 2026).
    result["brief_reported"] = f"this command reported brief {brief_id} as executed; run nothing else, in particular no kolo update-brief"
    workspace = getattr(args, "workspace", None)
    if workspace is not None:
        # The registry learns of it too, whoever ran the line (the tick or a pasted command), so a partner card
        # from the same email is never held waiting for a send that already happened.
        try:
            import inbox_watcher  # local import: inbox_watcher imports this module

            monitor_root = inbox_watcher.paths_for(Path(workspace).resolve())["monitor_root"]
            entry = brief_registry.load(monitor_root, brief_id)
            if entry is not None and entry.get("outcome") == "pending":
                brief_registry.mark(monitor_root, brief_id, "executed", "reported by the command that ran the line")
        except (OSError, ValueError):
            pass
    try:
        kolo_safe.run_command(kolo_safe.build_update_brief(brief_id, "executed", result), runner=runner)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        if not repeat:
            raise
        result["brief_report"] = "already reported by the earlier run: " + str(exc)[:120]


ACKNOWLEDGE_NOTE = (
    "Hello,\n\nThank you for the details on {piece}. I am going to work up the estimate myself and get back to you "
    "shortly. If it is easier to talk it through by phone or in person in the meantime, just say so.\n\n{shop}\n"
)
ESTIMATE_CLOSING = (
    "If you would like to move forward, reply here and we will set up a time to go over the design "
    "together.\n\n"
)
ESTIMATE_NOTE = (
    "Hello,\n\nThank you for the details on {piece}. Here is where the estimate lands:\n\n"
    "{spec_lines}\n\nEstimate: ${price}\n\n"
    "Just so you know how to read this: we estimate on the high end on purpose. This figure is pending "
    "final design approval, and once your design is finalized the final price is often a little lower. "
    "If it comes in under, we pass that straight along to you. Nothing is locked in until you have seen "
    "and approved the final design.\n\n{lead_time}This estimate is good through {valid_through}.\n\n"
    + ESTIMATE_CLOSING + "{shop}\n"
)


def estimate_email_facts(record: dict[str, Any], profile: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The facts an estimate email must carry, and the fixed fallback text."""
    from datetime import date, timedelta

    price = float(record["proposed_price"])
    shop = (profile.get("shop") or {}).get("name") or "the shop"
    terms = profile.get("terms") or {}
    valid_days = int(terms.get("quote_valid_days") or 7)
    lead = terms.get("lead_time_business_days")
    lead_time = f"Estimated lead time: about {lead} business days from design approval, not a guarantee. " if lead else ""
    valid_through = (date.today() + timedelta(days=valid_days)).strftime("%B %-d, %Y")
    spec = record.get("specification") or {}
    pieces = estimate_record.pieces_of(spec)
    if len(pieces) > 1:
        blocks = []
        for index, piece in enumerate(pieces):
            head = estimate_record.piece_label(spec, index).capitalize()
            body = "\n".join(f"- {key.replace('_', ' ').capitalize()}: {value}" for key, value in piece.items()
                             if value not in (None, "", []) and key not in ("notes", "piece_type"))
            blocks.append(f"{head}:\n{body}")
        spec_lines = "\n\n".join(blocks)
    else:
        spec_lines = "\n".join(
            f"- {key.replace('_', ' ').capitalize()}: {value}" for key, value in spec.items()
            if value not in (None, "", []) and key != "notes"
        )
    chosen = sorted({k.replace("stone_", "").replace("_", " ") for piece in pieces if isinstance(piece, dict)
                     for k, v in piece.items() if isinstance(v, str) and v.strip().lower() == "jeweler's choice"})
    revised = int(record.get("revision") or 0) > 0
    fixed = ESTIMATE_NOTE.format(
        piece=owner_questions.summary_of_piece(spec), spec_lines=spec_lines, price=f"{price:,.2f}",
        lead_time=lead_time, valid_through=valid_through, shop=shop,
    )
    if revised:
        fixed = fixed.replace("Here is where the estimate lands:", "Here is where the updated estimate lands:", 1)
    if len(pieces) > 1:
        specification_words = "; ".join(
            estimate_record.piece_label(spec, i) + ": " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in piece.items()
                                                                     if v not in (None, "", []) and k not in ("notes", "piece_type"))
            for i, piece in enumerate(pieces)
        )
    else:
        specification_words = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in spec.items() if v not in (None, "", []) and k != "notes")
    facts = {
        "piece": owner_questions.summary_of_piece(spec), "price": f"${price:,.2f}",
        **({"pieces": f"{len(pieces)} pieces in this order, priced together; the price is the total for all of them"} if len(pieces) > 1 else {}),
        "specification": specification_words,
        "lead time": (f"about {lead} business days from design approval, an estimate not a guarantee" if lead else ""),
        "valid_through": valid_through, "shop name": shop,
        **({"updated": "this is an updated estimate after the customer's change; say so, and that it replaces the earlier figure"}
           if revised else {}),
    }
    if chosen:
        facts["chosen by the jeweler, say if you have a preference"] = ", ".join(chosen)
    if (record.get("prior_piece") or {}).get("on_file"):
        facts["on file"] = "this follows the piece the shop made for them before, with the changes they named; say so in a sentence"
    renders = (record.get("concierge") or {}).get("renderings") or []
    if renders:
        facts["renderings attached"] = f"{len(renders)} view{'s' if len(renders) != 1 else ''} of the design, for guidance only"
        fixed = fixed.replace("Estimate: $", "Attached are renderings of the design, for guidance only.\n\nEstimate: $", 1)
    booked = record.get("appointment_booked") if isinstance(record.get("appointment_booked"), dict) else None
    if booked and booked.get("confirmed_start"):
        facts["meeting booked"] = str(booked["confirmed_start"])[:16].replace("T", " ")
        fixed = fixed.replace(ESTIMATE_CLOSING, "We will go over the design together at your visit.\n\n")
    elif any(isinstance(r, dict) and r.get("status") == "pending_approval" for r in record.get("appointment_approval_requests") or []):
        # The meeting card is on the owner's phone (live, 9 September 2026: the estimate said "I will confirm your
        # visit separately" and then asked them to reply to set up a time).
        facts["their visit"] = ("the time they asked for is being confirmed separately, in its own email; say only that, "
                                "and do not invite them to set up a time or ask about one")
        fixed = fixed.replace(ESTIMATE_CLOSING, "Your visit is being confirmed separately, in its own email.\n\n")
    reference = str(spec.get("reference_images") or "").strip()
    if reference.lower().startswith("from the photo"):
        facts["read from their photo"] = reference[:160]
    return facts, fixed


def send_approved_estimate_brief(args: argparse.Namespace) -> dict[str, Any]:
    """The owner approved the price: write the customer email and send it. One command."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    runner = getattr(args, "runner", subprocess.run)
    record = estimate_record.read_object(estimate_record.record_path(p["record_root"], args.estimate_id))
    route_ownership.validate_record(record)
    if record.get("status") == "estimate_sent":
        result = {"outcome": "already_sent", "price": record.get("proposed_price")}
        _report_brief(args, result, runner, repeat=True)
        return result
    if record.get("status") != "pending_approval":
        raise ValueError(f"estimate is {record.get('status')}, not pending_approval")
    price = float(getattr(args, "approved_price", None) or record["proposed_price"])
    if abs(price - float(record["proposed_price"])) > 0.005:
        raise ValueError("only the card's price can be sent; reject the card and answer the desk's question with the price")
    approved = {
        "approval_status": "approved",
        "estimate_id": args.estimate_id,
        "owner_approved_price": price,
        "binding_hash": record["approval_binding_hash"],
    }
    profile = read_object(p["shop_profile"])
    source_message = record.get("approval_source_message_id") or ""
    work_dir = estimate_work_dir(p["monitor_root"], args.estimate_id, source_message)
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared = work_dir / "customer-reply.txt"
    if prepared.exists() and prepared.read_text(encoding="utf-8").strip():
        body, body_source = prepared.read_text(encoding="utf-8"), "prepared"
    else:
        facts, fixed = estimate_email_facts(record, profile)
        body, body_source = _draft_customer_email(p, record, source_message, "estimate", facts, fixed, args)
        prepared.write_text(body, encoding="utf-8")
    write_private(work_dir / "approved.json", approved)
    held = _read_held(p, "price", args.estimate_id, source_message)
    if held and held.get("body"):
        body, body_source = str(held["body"]), "held"
        prepared.write_text(body, encoding="utf-8")
    if getattr(args, "hold", False):
        # The booking from the same email is still open: the estimate waits for it (the owner, 9 September 2026).
        _hold_email(p, "price", args.estimate_id, source_message, body)
        result = {"outcome": "estimate_held", "price": price, "email": body_source,
                  "note": "the estimate goes out with the booking's confirmation in one email"}
        _report_brief(args, result, runner)
        return result
    partner = _partner_entry(p, args)
    if partner is not None:
        shop_now = (read_object(p["shop_profile"]).get("shop") or {}).get("name") or "the shop"
        body, _extra = _bundle_with_partner(p, partner, "price", body, shop_now)
        prepared.write_text(body, encoding="utf-8")
    images: list[Path] = []
    for item in (record.get("concierge") or {}).get("renderings") or []:
        # The very views the owner approved with the price; a changed file is refused, as for a rendering card.
        image = work_dir / f"rendering-{int(item['slot'])}.png"
        if not image.is_file() or _sha256_file(image) != item.get("sha256"):
            raise ValueError("the renderings changed since the owner approved the card; run the doctor before sending")
        images.append(image)
    sent = send_approved_estimate(argparse.Namespace(
        images=images,
        claim_root=p["claim_root"], record_root=p["record_root"], estimate_id=args.estimate_id,
        approved=work_dir / "approved.json", body=work_dir / "customer-reply.txt",
        gmail_payload=work_dir / "gmail-send.json", provider_response=work_dir / "gmail-provider-response.json",
        record_output=work_dir / "current-record.json", message_id=None, current_state=None,
    ))
    if partner is not None:
        delivery = sent.get("estimate_delivery") if isinstance(sent.get("estimate_delivery"), dict) else {}
        receipt = {"id": delivery.get("provider_message_id"), "threadId": delivery.get("thread_id")}
        sent = _record_partner_delivery(p, partner, body, images, receipt, runner)
    result = {"outcome": "estimate_sent", "price": price, "record_status": sent.get("status"), "email": body_source,
              **({"bundled_with": partner["kind"]} if partner else {})}
    _report_brief(args, result, runner, repeat=bool(getattr(args, "released", False)))
    return result


def reject_rendering(args: argparse.Namespace) -> dict[str, Any]:
    """The owner held the renderings back: close the claim, send nothing."""
    import inbox_watcher  # local import: inbox_watcher imports this module

    p = inbox_watcher.paths_for(args.workspace.resolve())
    _close_parked_claim(p, args.message_id, "owner_rejected_rendering")
    return {"outcome": "rendering_rejected", "message_id": args.message_id}


def finalize_post_estimate(args: argparse.Namespace) -> dict[str, Any]:
    """Mirror and safely route one persisted post-estimate decision."""
    record, decision = estimate_record.post_estimate_decision(
        args.record_root, args.estimate_id, args.message_id
    )
    mirror_record(record, args.record_output)
    outcome = decision["outcome"]
    intents = decision["intents"]
    if outcome in {"design_change_detected", "classification_uncertain"}:
        asked = ask_unclear_reply(args, record, outcome)
        return {**asked, "outcome": outcome, "question_outcome": asked.get("outcome"),
                "should_finalize": True, "intents": intents, "next_action": "done"}
    if outcome != "post_estimate_continuation":
        reason_codes = {
            "design_change_detected": "design_change_detected",
            "classification_uncertain": "classification_uncertain",
            "classification_malformed": "classification_malformed",
        }
        kolo_safe.manual_review_claimed(
            args.monitor_root,
            args.claim_root,
            args.message_id,
            None,
            reason_codes[outcome],
        )
        return {
            "outcome": outcome,
            "should_finalize": True,
            "intents": intents,
            "next_action": "manual_review",
        }
    runner = getattr(args, "runner", subprocess.run)
    who = kolo_safe._sender_display(str((record.get("route") or {}).get("recipient") or "")) or "the customer"
    piece = owner_questions.summary_of_piece(record.get("specification")) if record.get("specification") else "their piece"
    if "cancellation" in intents:
        # The customer cancelled: the booked time comes off the calendar and the owner hears it in one sentence.
        # Nothing goes to the customer from the desk; the reply is the owner's.
        booked = record.get("appointment_booked") if isinstance(record.get("appointment_booked"), dict) else None
        released = ""
        if booked and booked.get("calendar_event_id"):
            try:
                profile = read_object(args.shop_profile) if getattr(args, "shop_profile", None) else {}
                calendar_id = (profile.get("scheduling") or {}).get("calendar") or "primary"
                kwargs = {"opener": args.opener} if getattr(args, "opener", None) else {}
                calendar_query.delete_event(calendar_id, str(booked["calendar_event_id"]), gateway_token.load_token(), **kwargs)
                record = estimate_record.record_appointment_cancelled(args.record_root, args.estimate_id, args.message_id, "customer cancelled")
                released = f" Their meeting on {str(booked.get('confirmed_start') or '')[:16].replace('T', ' ')} is off the calendar."
            except Exception as exc:  # noqa: BLE001 - the owner still hears about it, with the calendar left for them
                released = f" I could not release the calendar time ({str(exc)[:80]}); please remove it yourself."
        try:
            kolo_safe.tell_owner(args.monitor_root, f"{who} cancelled ({piece}).{released} Nothing was sent to them; the reply is yours.", runner)
        except Exception:  # noqa: BLE001
            pass
        intents = [i for i in intents if i != "cancellation"]
    if "estimate_acceptance" in intents:
        try:
            kolo_safe.tell_owner(args.monitor_root, f"{who} accepted the estimate for {piece}. Nothing more for the desk to send; the next step is yours.", runner)
        except Exception:  # noqa: BLE001
            pass
    actionable = set(intents) & {"rendering_request", "appointment_request"}
    if not actionable:
        finish_processed(
            args.monitor_root, args.claim_root, args.record_root, args.message_id
        )
        return {
            "outcome": outcome,
            "should_finalize": True,
            "intents": intents,
            "next_action": "finalize",
        }
    next_action = (
        "request_appointment_approval_then_send_rendering"
        if actionable == {"rendering_request", "appointment_request"}
        else (
            "request_appointment_approval"
            if "appointment_request" in actionable
            else "send_rendering"
        )
    )
    return {
        "outcome": outcome,
        "should_finalize": False,
        "intents": intents,
        "next_action": next_action,
    }


def record_appointment_booked(args: argparse.Namespace) -> dict[str, Any]:
    """Persist and mirror an immutable successful appointment receipt."""
    before = read_object(
        estimate_record.record_path(args.record_root, args.estimate_id)
    )
    record = estimate_record.record_appointment_booked(
        args.record_root, args.estimate_id, read_object(args.receipt)
    )
    if before == record:
        write_private(args.record_output, record)
    else:
        mirror_record(record, args.record_output)
    return record


def add_common_paths(
    parser: argparse.ArgumentParser, *, message_required: bool = True
) -> None:
    parser.add_argument("--claim-root", type=Path, required=True)
    parser.add_argument("--record-root", type=Path, required=True)
    parser.add_argument("--message-id", required=message_required)
    parser.add_argument("--estimate-id", required=True)
    parser.add_argument("--record-output", type=Path, required=True)


def main(argv: list[str] | None = None, _retry_of: str | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    spec = sub.add_parser("send-spec-followup")
    add_common_paths(spec)
    spec.add_argument("--monitor-root", type=Path, required=True)
    spec.add_argument("--route", type=Path, required=True)
    spec.add_argument("--body", type=Path, required=True)
    spec.add_argument("--gmail-payload", type=Path, required=True)
    spec.add_argument("--provider-response", type=Path, required=True)
    spec.add_argument("--initiating", action="store_true")
    approval = sub.add_parser("request-approval")
    add_common_paths(approval)
    approval.add_argument("--monitor-root", type=Path, required=True)
    approval.add_argument("--current-state", type=Path, required=True)
    approval.add_argument("--approval-request", type=Path, required=True)
    approval.add_argument("--shop-profile", type=Path, required=True)
    send = sub.add_parser("send-approved-estimate")
    add_common_paths(send, message_required=False)
    send.add_argument("--current-state", type=Path)
    send.add_argument("--approved", type=Path, required=True)
    send.add_argument("--body", type=Path, required=True)
    send.add_argument("--gmail-payload", type=Path, required=True)
    send.add_argument("--provider-response", type=Path, required=True)
    rendering = sub.add_parser("send-rendering")  # refused: kept so old prompts fail loudly
    add_common_paths(rendering)
    rendering.add_argument("--monitor-root", type=Path, required=True)
    rendering.add_argument("--body", type=Path, required=True)
    rendering.add_argument(
        "--image", dest="images", type=Path, action="append", required=True
    )
    ask_render = sub.add_parser("request-rendering-approval")
    for name in ("--monitor-root", "--claim-root", "--record-root", "--shop-profile"):
        ask_render.add_argument(name, type=Path, required=True)
    ask_render.add_argument("--message-id", required=True)
    ask_render.add_argument("--estimate-id", required=True)
    ask_render.add_argument("--checker", default=None)
    ask_render.add_argument("--archetype", default=None)
    rendering.add_argument("--gmail-payload", type=Path, required=True)
    rendering.add_argument("--provider-response", type=Path, required=True)
    appointment = sub.add_parser("request-appointment-approval")
    add_common_paths(appointment)
    appointment.add_argument("--shop-profile", type=Path, default=None)
    appointment.add_argument("--monitor-root", type=Path, required=True)
    appointment.add_argument("--appointment-intent", type=Path, required=True)
    appointment.add_argument("--appointment-approval", type=Path, required=True)
    appointment.add_argument("--defer-finalize-for-rendering", action="store_true")
    take = sub.add_parser("intake")
    take.add_argument("--monitor-root", type=Path, required=True)
    take.add_argument("--claim-root", type=Path, required=True)
    take.add_argument("--record-root", type=Path, required=True)
    take.add_argument("--message-id", required=True)
    take.add_argument("--shop-profile", type=Path, required=True)
    resolve = sub.add_parser("resolve-review-approval")
    resolve.add_argument("--monitor-root", type=Path, required=True)
    resolve.add_argument("--review-key", required=True)
    resolve.add_argument("--brief-id", required=True)
    start = sub.add_parser("worker-start")
    start.add_argument("--monitor-root", type=Path, required=True)
    start.add_argument("--claim-root", type=Path, required=True)
    start.add_argument("--message-id", required=True)
    start.add_argument("--record-root", type=Path, default=None)
    not_inquiry = sub.add_parser("not-an-inquiry")
    not_inquiry.add_argument("--monitor-root", type=Path, required=True)
    not_inquiry.add_argument("--claim-root", type=Path, required=True)
    not_inquiry.add_argument("--record-root", type=Path, required=True)
    not_inquiry.add_argument("--message-id", required=True)
    not_inquiry.add_argument("--estimate-id", required=True)
    not_inquiry.add_argument("--reason", required=True)
    not_inquiry.add_argument("--record-output", type=Path, required=True)
    review = sub.add_parser("review-thread")
    for name in ("--monitor-root", "--claim-root", "--record-root", "--shop-profile", "--review"):
        review.add_argument(name, type=Path, required=True)
    review.add_argument("--message-id", required=True)
    review.add_argument("--estimate-id", required=True)
    pricing = sub.add_parser("price")
    for name in ("--monitor-root", "--claim-root", "--record-root", "--shop-profile"):
        pricing.add_argument(name, type=Path, required=True)
    pricing.add_argument("--message-id", required=True)
    pricing.add_argument("--estimate-id", required=True)
    pricing.add_argument("--finished-grams", type=float, required=True)
    pricing.add_argument("--bench-hours", type=float, required=True)
    pricing.add_argument("--center-carat", type=float, default=None)
    pricing.add_argument("--fee", dest="fees", action="append", default=[])
    pricing.add_argument("--accent", dest="accents", action="append", default=[])
    render_ok = sub.add_parser("send-approved-rendering")
    render_ok.add_argument("--workspace", type=Path, required=True)
    render_ok.add_argument("--estimate-id", required=True)
    render_ok.add_argument("--message-id", required=True)
    render_ok.add_argument("--brief-id", default=None)
    render_ok.add_argument("--hold", action="store_true", help="keep the pictures for one email with the partner card from the same message")
    render_ok.add_argument("--with-held", default=None, help="the held partner card's brief id: one email carries both")
    render_ok.add_argument("--released", action="store_true", help="a held send going alone: its card was reported when held")
    book = sub.add_parser("book-approved-appointment")
    book.add_argument("--workspace", type=Path, required=True)
    book.add_argument("--estimate-id", required=True)
    book.add_argument("--message-id", required=True)
    book.add_argument("--brief-id", default=None)
    book.add_argument("--option", type=int, default=1)
    book.add_argument("--start", default=None)
    book.add_argument("--hold-email", action="store_true", help="book the time, keep the confirmation for one email with the partner card")
    book.add_argument("--with-held", default=None, help="the held partner card's brief id: one email carries both")
    book.add_argument("--released", action="store_true", help="a held send going alone: its card was reported when held")
    offer = sub.add_parser("send-approved-times")
    offer.add_argument("--workspace", type=Path, required=True)
    offer.add_argument("--estimate-id", required=True)
    offer.add_argument("--message-id", required=True)
    offer.add_argument("--brief-id", default=None)
    rejected = sub.add_parser("appointment-rejected")
    rejected.add_argument("--workspace", type=Path, required=True)
    rejected.add_argument("--estimate-id", required=True)
    rejected.add_argument("--message-id", required=True)
    rejected.add_argument("--brief-id", default=None)
    send_brief = sub.add_parser("send-approved-estimate-brief")
    send_brief.add_argument("--workspace", type=Path, required=True)
    send_brief.add_argument("--estimate-id", required=True)
    send_brief.add_argument("--brief-id", default=None)
    send_brief.add_argument("--approved-price", type=float, default=None)
    send_brief.add_argument("--hold", action="store_true", help="keep the estimate for one email with the booking from the same message")
    send_brief.add_argument("--with-held", default=None, help="the held partner card's brief id: one email carries both")
    send_brief.add_argument("--released", action="store_true", help="a held send going alone: its card was reported when held")
    render_no = sub.add_parser("reject-rendering")
    render_no.add_argument("--workspace", type=Path, required=True)
    render_no.add_argument("--message-id", required=True)
    ask_rate = sub.add_parser("ask-missing-rate")
    ask_rate.add_argument("--monitor-root", type=Path, required=True)
    ask_rate.add_argument("--claim-root", type=Path, required=True)
    ask_rate.add_argument("--record-root", type=Path, required=True)
    ask_rate.add_argument("--shop-profile", type=Path, required=True)
    ask_rate.add_argument("--message-id", required=True)
    ask_rate.add_argument("--estimate-id", required=True)
    questions = sub.add_parser("open-questions")
    questions.add_argument("--workspace", type=Path, required=True)
    answer = sub.add_parser("answer-question")
    answer.add_argument("--workspace", type=Path, required=True)
    answer.add_argument("--base-dir", type=Path, required=True)
    answer.add_argument("--question", default=None)
    answer.add_argument("--answer", required=True)
    answer.add_argument("--openclaw", default=None)
    finalize = sub.add_parser("finalize-post-estimate")
    add_common_paths(finalize)
    finalize.add_argument("--monitor-root", type=Path, required=True)
    booked = sub.add_parser("record-appointment-booked")
    booked.add_argument("--record-root", type=Path, required=True)
    booked.add_argument("--estimate-id", required=True)
    booked.add_argument("--receipt", type=Path, required=True)
    booked.add_argument("--record-output", type=Path, required=True)
    args = parser.parse_args(argv)
    workspace_arg = getattr(args, "workspace", None) or (workspace_of(args.monitor_root) if getattr(args, "monitor_root", None) else None)
    if workspace_arg is not None:
        rehearsal.apply(Path(workspace_arg).resolve())
    try:
        if args.command == "send-spec-followup":
            record = send_spec_followup(args)
        elif args.command == "request-approval":
            record = request_approval(args)
        elif args.command == "send-approved-estimate":
            record = send_approved_estimate(args)
        elif args.command == "request-appointment-approval":
            record = request_appointment_approval(args)
        elif args.command == "intake":
            record = intake(args)
        elif args.command == "not-an-inquiry":
            record = not_an_inquiry(args)
        elif args.command == "worker-start":
            record = worker_start(args)
        elif args.command == "resolve-review-approval":
            record = resolve_review_approval(args)
        elif args.command in EXECUTOR_COMMANDS:
            executors = {
                "send-approved-rendering": send_approved_rendering,
                "book-approved-appointment": book_approved_appointment,
                "send-approved-estimate-brief": send_approved_estimate_brief,
                "send-approved-times": send_approved_times,
                "appointment-rejected": appointment_rejected,
            }
            desk = args.workspace.resolve() / "estimate-desk"
            try:
                with run_lease.hold(desk, args.command, _executor_key(args)):
                    record = executors[args.command](args)
            except run_lease.LeaseHeld as exc:
                # Not a failure: the other run finishes the work, reports the
                # brief, or asks the owner itself. Nobody is asked twice.
                print(json.dumps({"error": str(exc), "in_progress": True, "asked_owner": False}, sort_keys=True),
                      file=sys.stderr)
                return LEASE_HELD_EXIT
            except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError, judge.JudgmentError) as exc:
                if _retry_of is None:
                    try:
                        asked = _command_failed(args, argv, exc)
                    except (OSError, ValueError, subprocess.CalledProcessError):
                        asked = None
                    print(json.dumps({"error": str(exc), "asked_owner": bool(asked and asked.get("created"))}, sort_keys=True),
                          file=sys.stderr)
                    return 2
                raise
        elif args.command == "reject-rendering":
            record = reject_rendering(args)
        elif args.command == "review-thread":
            record = review_thread(args)
        elif args.command == "price":
            record = price(args)
        elif args.command == "ask-missing-rate":
            record = ask_missing_rate(args)
        elif args.command == "open-questions":
            record = open_questions(args)
        elif args.command == "answer-question":
            record = answer_question(args)
        elif args.command == "finalize-post-estimate":
            record = finalize_post_estimate(args)
        elif args.command == "record-appointment-booked":
            record = record_appointment_booked(args)
        elif args.command == "request-rendering-approval":
            record = request_rendering_approval(args)
        elif args.command == "send-rendering":
            raise ValueError(RENDERING_GATE)
        else:
            raise ValueError(f"unknown command {args.command}")
        print(json.dumps(record, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
        judge.JudgmentError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
