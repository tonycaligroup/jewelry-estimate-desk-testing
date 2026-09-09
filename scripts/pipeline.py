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
import re
import subprocess
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import estimate_record
import gmail_text
import inbox_claim
import inbox_monitor
import judge
import ledger
import kolo_safe
import owner_questions
import rendering_materialize
import slots
import reading_check
import route_ownership
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
    "dimensions": "roughly what length or size would you like?",
    "setting_style": "what look do you have in mind for the setting, or shall we suggest one?",
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


def question_lines(missing: list[str], specification: dict[str, Any] | None = None) -> list[str]:
    """The plain questions for what is missing, one per detail, as a customer reads them."""
    asks = []
    for name in missing[:8]:
        if reading_check.is_confirm(name):
            asks.append(reading_check.question_for(name) or name)
            continue
        index, field = estimate_record.split_field_name(name)
        question = FIELD_QUESTIONS.get(field, f"could you tell us the {field.replace('_', ' ')}?")
        if index is not None:
            question = f"for the {estimate_record.piece_label(specification or {}, index)}, {question}"
        asks.append(question[0].upper() + question[1:])
    return asks


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


EXAMPLE_PHOTOS_FILE = "example-photos.json"
MAX_EXAMPLE_PHOTOS = 2


def example_photos(p: dict[str, Path], message_id: str, paths: dict[str, str], openclaw: str,
                   command_runner: Runner, initiating: bool) -> list[str]:
    """What the desk read from the customer's example photos, once per claim (WORKFLOW.md 6.2, 8 September 2026).

    A first inquiry reads the customer's newest photos on the thread; a
    reply reads only photos attached to that reply. The readings are kept
    in the claim's work so a resumed claim never pays for them twice, and
    any failure leaves the reading to the words alone.
    """
    cache = Path(paths["work_dir"]) / EXAMPLE_PHOTOS_FILE
    if cache.exists():
        try:
            stored = workflow_safe.read_object(cache)
            return [str(t) for t in (stored.get("photos") or []) if str(t).strip()]
        except (OSError, ValueError):
            pass
    found: list[str] = []
    try:
        import artwork as artwork_module  # local import, as in render_step

        thread = workflow_safe.read_object(Path(paths["gmail_thread"])) if Path(paths["gmail_thread"]).exists() else {}
        try:
            mailbox = (workflow_safe.read_object(p["shop_profile"]).get("shop") or {}).get("outbound_mailbox") if p.get("shop_profile") else None
        except (OSError, ValueError):
            mailbox = None
        parts = artwork_module.image_parts(thread, mailbox)
        if not initiating:
            parts = [part for part in parts if part.get("message_id") == message_id]
        if parts:
            import gateway_token  # local import; only needed when the thread carries images
            import rendering

            vision_model, _image_model = _render_settings(p)
            rendering.PROVIDER_MODE = _render_options(p)["provider"]
            images = artwork_module.collect(thread, Path(paths["work_dir"]) / "examples", gateway_token.load_token(),
                                            mailbox=mailbox, limit=MAX_EXAMPLE_PHOTOS)
            for image in images[:MAX_EXAMPLE_PHOTOS]:
                text = rendering.describe_example(image, judge.EXAMPLE_PHOTO_PROMPT, openclaw, command_runner, vision_model)
                if text:
                    found.append(text)
    except Exception as exc:  # noqa: BLE001 - a photo is a bonus; the words are read either way
        error = f"{type(exc).__name__}: {str(exc)[:200]}"
    else:
        error = ""
    try:
        workflow_safe.write_private(cache, {"photos": found, **({"error": error} if error else {})})
    except (OSError, ValueError):
        pass
    return found


def _send_followup(
    p: dict[str, Path], base_dir: Path, message_id: str, estimate_id: str,
    digest: dict[str, Any], missing: list[str], initiating: bool, paths: dict[str, str],
    profile: dict[str, Any], model: str | None, judge_runner: Runner, openclaw: str | None,
    command_runner: Runner = subprocess.run, photos: list[str] | None = None,
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
                                       shop_name, model, judge_runner, openclaw, photos=photos)
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


PROGRESS_FILE = "rendering-progress.json"


def _render_settings(p: dict[str, Path]) -> tuple[str | None, str | None]:
    """The vision and image models: the profile may pin them; the defaults are named, never the environment's guess."""
    import rendering

    try:
        profile_now = workflow_safe.read_object(p["shop_profile"]) if p.get("shop_profile") else {}
    except (OSError, ValueError):
        profile_now = {}
    settings_block = profile_now.get("rendering") or {}
    vision_model = str(settings_block.get("vision_model") or "").strip() or rendering.DEFAULT_VISION_MODEL
    image_model = str(settings_block.get("image_model") or "").strip() or None
    return vision_model, image_model


DEFAULT_VIEWS_PER_PIECE = 2
DEFAULT_PARALLEL_VIEWS = 8  # every view of a rendering at once (72 at once ran clean on the proxy, 8 September 2026)
MAX_PARALLEL_VIEWS = 12


def _render_options(p: dict[str, Path]) -> dict[str, Any]:
    """The profile's rendering choices: provider (auto|direct|cli), vision_check (bool), parallel (1-4)."""
    import image_provider

    try:
        profile_now = workflow_safe.read_object(p["shop_profile"]) if p.get("shop_profile") else {}
    except (OSError, ValueError):
        profile_now = {}
    block = profile_now.get("rendering") or {}
    provider = str(block.get("provider") or "auto").strip().lower()
    if provider not in image_provider.MODES:
        provider = "auto"
    parallel = block.get("parallel")
    if not isinstance(parallel, int) or isinstance(parallel, bool) or not 1 <= parallel <= MAX_PARALLEL_VIEWS:
        parallel = DEFAULT_PARALLEL_VIEWS
    size = str(block.get("size") or "").strip()
    if not re.fullmatch(r"\d{3,4}x\d{3,4}", size):
        size = "1024x1024"
    quality = str(block.get("quality") or "").strip().lower()
    if quality not in image_provider.QUALITIES:
        quality = "auto"
    return {"provider": provider, "vision_check": block.get("vision_check") is not False, "parallel": parallel,
            "size": size, "quality": quality}


def _views_per_piece(p: dict[str, Path]) -> int:
    """How many views each piece gets: the profile's rendering.views_per_piece (1 or 2), default two."""
    try:
        profile_now = workflow_safe.read_object(p["shop_profile"]) if p.get("shop_profile") else {}
    except (OSError, ValueError):
        profile_now = {}
    value = (profile_now.get("rendering") or {}).get("views_per_piece")
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 2:
        return value
    return DEFAULT_VIEWS_PER_PIECE


def _plan_rendering(
    p: dict[str, Path], message_id: str, record: dict[str, Any], paths: dict[str, str], openclaw: str,
    command_runner: Runner, model: str | None, art: Path | None,
) -> dict[str, Any]:
    """The whole rendering as data: every view to render, in order, plus the views a revision keeps."""
    import rendering

    work_dir = Path(paths["work_dir"])
    thread = workflow_safe.read_object(Path(paths["gmail_thread"])) if Path(paths["gmail_thread"]).exists() else {}
    context = judge.thread_text(gmail_text.thread_digest(thread, message_id)) if thread else ""
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
    specification = record.get("specification") or {}
    pieces = estimate_record.pieces_of(specification)
    views_each = min(_views_per_piece(p), 2 if len(pieces) <= 2 else 1)
    set_note = " The pieces are a matching set: one design language, the same metal finish and motifs, each piece its own size." \
        if len(pieces) > 1 and estimate_record.is_set(specification) else ""
    kept: dict[str, list[dict[str, Any]]] = {}
    if note and only:
        for view in (previous or {}).get("views") or []:
            if view.get("piece") not in only and Path(str(view.get("image") or "")).exists():
                kept.setdefault(str(view["piece"]), []).append(view)
    plan: dict[str, Any] = {"pieces": [], "views": [], "prompts": [], "references": [],
                            **({"revision": note, "round": int(change.get("round") or 2)} if note else {})}
    slot = 1
    for index, piece in enumerate(pieces[:4]):
        label = estimate_record.piece_label(specification, index)
        if label in kept:
            previous_plan = next((pc.get("plan") for pc in (previous or {}).get("pieces") or [] if pc.get("label") == label), None)
            plan["pieces"].append({"label": label, "plan": previous_plan or {}, "kept": True})
            for view in kept[label]:
                plan["views"].append({**view, "slot": slot, "piece": label, "kept": True, "done": True})
                slot += 1
            continue
        piece_spec = {**piece, "notes": (str(piece.get("notes") or "") + set_note).strip()} if len(pieces) > 1 else dict(specification)
        if note and (not only or label in only):
            piece_spec = rendering._with_change(piece_spec, note)
        out_dir = work_dir / "renders" / (f"piece-{index + 1}" if len(pieces) > 1 else "")
        try:
            planned = rendering.plan_piece(piece_spec, out_dir, openclaw, artwork=art, context=context, model=model,
                                           runner=command_runner, views=views_each if len(pieces) > 1 else 2)
        except judge.JudgmentError as exc:
            raise ValueError(f"rendering plan failed: {exc}") from exc
        plan["pieces"].append({"label": label, "plan": planned["plan"]})
        plan["prompts"].extend(planned["prompts"])
        plan["references"] = planned["references"]
        for view in planned["views"]:
            plan["views"].append({**view, "slot": slot, "piece": label, "done": False})
            slot += 1
    plan["plan"] = next((pc["plan"] for pc in plan["pieces"] if pc.get("plan")), {})
    return plan


MAX_VIEW_STARTS = 3  # a view started this many times without finishing is not going to: the owner is asked


def render_step(
    p: dict[str, Path], message_id: str, estimate_id: str, record: dict[str, Any],
    paths: dict[str, str], openclaw: str, command_runner: Runner,
    model: str | None = None, judge_runner: Runner | None = None, deadline: float | None = None,
) -> dict[str, Any]:
    """One tick's worth of rendering: plan on the first call, one view per call, the card when the last view is done.

    Progress lives in the claim's work folder (`rendering-progress.json`), so
    a rendering spans ticks: each call renders one view and returns
    `rendering_in_progress`; the caller releases the claim and the next tick
    calls again. A crash costs one view. Nothing runs outside the watcher:
    no job, no second environment, nothing in the owner's routines list
    (RELEASE-PLAN-4.12.md follow-up, 7 September 2026).
    """
    import artwork as artwork_module
    import rendering

    work_dir = Path(paths["work_dir"])
    progress_path = work_dir / PROGRESS_FILE
    progress = workflow_safe.read_object(progress_path) if progress_path.exists() else None
    art = None
    resumed = False
    report_path = work_dir / "rendering-report.json"
    if progress is None and report_path.exists() and not (work_dir / "rendering-change.json").exists():
        # Every view was rendered and the run died filing the card (7 September
        # 2026: killed during the previews, the next run planned and rendered
        # again). The finished report is the rendering; only the card is owed.
        finished = workflow_safe.read_object(report_path)
        views = finished.get("views") or []
        if views and all(Path(str(v.get("image") or "")).is_file() for v in views):
            progress = {**finished, "views": [{**v, "done": True} for v in views]}
            resumed = True
    if progress is None:
        thread = workflow_safe.read_object(Path(paths["gmail_thread"])) if Path(paths["gmail_thread"]).exists() else {}
        try:
            import gateway_token  # local import; only needed when the thread carries images

            try:
                mailbox = (workflow_safe.read_object(p["shop_profile"]).get("shop") or {}).get("outbound_mailbox") if p.get("shop_profile") else None
            except (OSError, ValueError):
                mailbox = None
            found = artwork_module.collect(thread, work_dir / "artwork", gateway_token.load_token(), mailbox=mailbox)
            art = found[-1] if found else None
        except Exception:  # noqa: BLE001 - artwork is a bonus; a render without it still goes to the owner
            art = None
        progress = _plan_rendering(p, message_id, record, paths, openclaw, command_runner, model, art)
        progress["artwork"] = str(art) if art else None
        workflow_safe.write_private(progress_path, progress)
    art = Path(progress["artwork"]) if progress.get("artwork") else None
    vision_model, image_model = _render_settings(p)
    options = _render_options(p)
    rendering.PROVIDER_MODE = options["provider"]
    rendering.IMAGE_SIZE = options["size"]
    rendering.IMAGE_QUALITY = options["quality"]
    import image_provider
    direct = image_provider.available(options["provider"])
    pending = [v for v in progress["views"] if not v.get("done")]
    if pending:
        refs = [Path(r) for r in progress.get("references") or []]
        done_count = len(progress["views"]) - len(pending)
        left = rendering.remaining_seconds(deadline)
        if left is not None and left < rendering.START_MIN_SECONDS:
            # Too little of the tick left to finish a view: nothing started, nothing counted; next tick.
            return {"outcome": "rendering_in_progress", "done": done_count, "of": len(progress["views"]), "next": "again",
                    "deferred": True}
        # The provider reached directly answers in seconds and takes several
        # calls at once (Kolo's probe, 8 September 2026): every pending view
        # runs in this tick, `parallel` at a time. Through the CLI it is one
        # view per tick, as before (one command at a time, minutes each).
        batch = pending if direct else pending[:1]
        # A tick killed mid-view leaves no result behind, only this count; a
        # view that keeps dying is not tried forever (7 September 2026: a
        # regeneration ran the tick past its 300 s and the job was killed).
        for view in batch:
            view["started"] = int(view.get("started") or 0) + 1
            if view["started"] > MAX_VIEW_STARTS:
                # The owner is asked (the stuck-claim question); "retry" or a
                # requeue gets a fresh count of starts, like a fresh retry budget.
                view["started"] = 0
                workflow_safe.write_private(progress_path, progress)
                raise ValueError(f"rendering view {view['slot']} did not finish in {MAX_VIEW_STARTS} ticks; "
                                 "the image or vision step is too slow for the watcher")
        workflow_safe.write_private(progress_path, progress)
        lock = threading.Lock()
        failures: list[BaseException] = []

        def one(view: dict[str, Any]) -> None:
            piece_plan = next((pc.get("plan") for pc in progress["pieces"] if pc.get("label") == view.get("piece")),
                              progress.get("plan") or {})
            try:
                result = rendering.render_view(view, piece_plan or {}, refs, openclaw, artwork=art, vision_model=vision_model,
                                               image_model=image_model, runner=command_runner, deadline=deadline,
                                               check=options["vision_check"])
            except Exception as exc:  # noqa: BLE001 - recorded, re-raised below
                with lock:
                    # A failure that raises is counted by the claim's retry budget
                    # already; only a silent death (the tick killed) keeps the start.
                    view["started"] -= 1
                    failures.append(exc)
                    workflow_safe.write_private(progress_path, progress)
                return
            with lock:
                if result.get("deferred"):
                    view["started"] -= 1
                else:
                    view.update({**result, "slot": view["slot"], "piece": view.get("piece"), "done": True})
                workflow_safe.write_private(progress_path, progress)

        if len(batch) == 1:
            one(batch[0])
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(options["parallel"], len(batch)))) as pool:
                list(pool.map(one, batch))
        if failures:
            raise failures[0]
        remaining = len([v for v in progress["views"] if not v.get("done")])
        if remaining:
            return {"outcome": "rendering_in_progress", "done": len(progress["views"]) - remaining, "of": len(progress["views"]),
                    "next": "again", **({"deferred": True} if len(progress["views"]) - remaining == done_count else {})}
    report = {
        "plan": progress.get("plan") or {}, "prompts": progress.get("prompts") or [], "references": progress.get("references") or [],
        "pieces": progress["pieces"], "views": [{k: v for k, v in view.items() if k != "done"} for view in progress["views"]],
        **({"revision": progress["revision"]} if progress.get("revision") else {}),
    }
    report["all_passed"] = all(v.get("passed") for v in report["views"])
    note = str(progress.get("revision") or "")
    images: list[Path] = []
    # The slot files are write-once (a different image in a slot is refused).
    # A revision replaces the images the owner passed on, and a run after a
    # crash replaces what the dead run left; either way the previous file is
    # kept beside the slot as history, never sent, never lost.
    label = f"r{int(progress.get('round') or 2) - 1}" if note else "prev"
    for slot in range(1, 5) if not resumed else ():
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
    # until they approve. The progress file outlives the card step: a run
    # that dies here resumes to the card, it does not render again.
    carded = workflow_safe.request_rendering_approval(argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        shop_profile=p.get("shop_profile"), message_id=message_id, estimate_id=estimate_id,
        runner=command_runner, checker=checker,
        archetype=", ".join(dict.fromkeys(str((pc.get("plan") or {}).get("archetype") or "")
                                          for pc in report.get("pieces") or [{"plan": report["plan"]}])).strip(", "),
        revised=note or None, revision=int(progress.get("round") or 1) if note else 1,
    ))
    progress_path.unlink(missing_ok=True)
    return carded


def render_and_send(
    p: dict[str, Path], message_id: str, estimate_id: str, record: dict[str, Any],
    paths: dict[str, str], openclaw: str, command_runner: Runner,
    model: str | None = None, judge_runner: Runner | None = None,
) -> dict[str, Any]:
    """Every step at once: the lab and the tests call this; the desk calls `render_step` once per tick."""
    while True:
        done = render_step(p, message_id, estimate_id, record, paths, openclaw, command_runner, model=model, judge_runner=judge_runner)
        if done.get("outcome") != "rendering_in_progress":
            return done


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
    try:
        from zoneinfo import ZoneInfo as _Zone

        resolved = slots.resolve_requested(asked, resolved, datetime.now(_Zone(zone_name)))
    except (KeyError, ValueError, OSError):
        pass
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
        if offered.get("outside_hours"):
            intent["outside_hours"] = list(offered["outside_hours"])[:3]
            intent["hours"] = str(offered.get("hours") or "")[:160]
        if offered.get("reason"):
            intent["availability_note"] = offered["reason"]
        elif offered.get("outside_hours") and offered.get("mode") == "offer":
            intent["availability_note"] = (
                f"the time they asked for ({'; '.join(offered['outside_hours'][:2])}) is outside your hours; these are free"
            )[:160]
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
    return render_step(p, message_id, estimate_id, record, paths, openclaw, command_runner, model=model, judge_runner=judge_runner)


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
    if pending is not None and any(reading_check.is_confirm(f) for f in pending["missing_required_fields"]):
        # A recorded ask that carries reading checks: run the check again on
        # the same words before honouring it. A check the current code does
        # not raise (a dead run's stale reading, an older version) is dropped;
        # an emptied ask is no ask, and the message goes on to be priced.
        current = reading_check.names(reading_check.compare(digest, record.get("specification") or {}))
        record = estimate_record.drop_stale_confirms(p["record_root"], estimate_id, message_id, current)
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
    known = estimate_record.known_specification(record)
    photos = example_photos(p, message_id, paths, openclaw or judge.default_openclaw(), command_runner, initiating)
    shop_bodies = [str(m.get("body") or "") for m in digest.get("messages") or [] if m.get("sent_by") == "shop"]
    handled_words = " ".join(reading_check.strip_shop_lines(reading_check.own_words(str(m.get("body") or "")), shop_bodies)
                             for m in digest.get("messages") or [] if m.get("sent_by") == "customer" and m.get("claimed"))
    judged = judge.triage_and_extract(digest, model, judge_runner, openclaw, known=known, photos=photos) if initiating else None
    reread = False
    if judged and judged["kind"] != "estimate_request":
        if (Path(paths["work_dir"]) / workflow_safe.OWNER_SAYS_ESTIMATE_FILE).exists():
            judged, reread = {**judged, "kind": "estimate_request", "note": "the owner said to quote it"}, True
        elif judged["kind"] in ("not_an_estimate_request", "not_a_quote_request", "inventory_request") \
                and not estimate_record.asks_for_inventory(handled_words) and estimate_record.reads_like_an_order(handled_words):
            # "Do you have a 14k WG lab tennis bracelet, 7-inch, ready to ship?" names a piece and its facts:
            # a shop that makes to order quotes it, whatever the reading called it.
            judged, reread = {**judged, "kind": "estimate_request", "note": "reads like an order: quoted as a custom piece"}, True
    triage = {"kind": judged["kind"], "note": judged["note"]} if judged else {"kind": "estimate_request", "note": "reply on an open estimate"}
    inventory = bool(record.get("inventory_inquiry")) or triage["kind"] == "inventory_request" or (
        triage["kind"] in ("not_an_estimate_request", "not_a_quote_request") and estimate_record.asks_for_inventory(handled_words)
    )
    if inventory and not record.get("appointment_booked"):
        return _inventory_inquiry(p, message_id, estimate_id, record, digest, paths, model, judge_runner, openclaw, command_runner, triage)
    if triage["kind"] in NOT_AN_INQUIRY:
        workflow_safe.not_an_inquiry(_namespace(
            p, message_id, estimate_id, reason=triage["kind"], record_output=Path(paths["current_record"]),
        ))
        return {"outcome": "not_an_inquiry", "reason": triage["kind"], "next": "done"}
    if triage["kind"] == "not_an_estimate_request":
        # Out of scope by the reading (an appraisal, stock, job status): the owner decides; nothing is filed silently.
        asked = workflow_safe.ask_out_of_scope(_namespace(p, message_id, estimate_id, runner=command_runner), triage.get("note") or "")
        return {"outcome": "awaiting_owner", "question_id": asked.get("question_id"), "next": "done"}
    if triage["kind"] == "escalation":
        return _manual_review(p, message_id, "customer_escalation", command_runner)

    if judged and not reread:
        specification = judged["specification"]
    else:
        specification = judge.extract_specification(digest, model, judge_runner, openclaw, known=known, photos=photos)["specification"]
    specification = estimate_record.carry_prior_facts(record, specification)
    specification = estimate_record.merge_known_facts(record, specification)
    specification = estimate_record.settle_center_stone(
        specification, " ".join(reading_check.own_words(str(m.get("body") or "")) for m in digest.get("messages") or []
                                if m.get("sent_by") == "customer"))
    # The message being handled decides a meeting request in code: a
    # reschedule ("can we do Friday at 4pm?") is a meeting, not a questionnaire.
    specification = estimate_record.settle_scheduling_intent(specification, handled_words)
    if not initiating:
        # "Before I come in, can I get a ballpark?": the first email's meeting request does not ride along.
        specification = estimate_record.drop_carried_scheduling_intent(specification, record, handled_words)
        # "I don't know, you decide": the details of the last ask become the jeweler's choice, as the follow-up promised.
        specification = estimate_record.settle_left_to_jeweler(specification, record, handled_words)
    # The ledger (RELEASE-PLAN-4.15.md): every fact with its source. The reading is absorbed row by row, a
    # customer's written word is never overwritten by a photo or a re-read, and what stands is what the
    # gate, the record, the card, and the emails see.
    specification = estimate_record.settle_grades(specification)
    ledger.migrate(desk, record)
    ledger.absorb(desk, estimate_id, specification, message_id, handled_words, " ".join(photos),
                  changeable=ledger.changeable_fields(record))
    specification = ledger.specification(desk, estimate_id, specification) or specification
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
        if specification.get("scheduling_intent") and (
            not record.get("appointment_booked") or estimate_record.asks_to_reschedule(handled_words)
        ):
            # Meeting first: a customer who asks to come in gets the meeting,
            # not a questionnaire. The details are settled at the meeting or
            # in a later email, and pricing picks up from there. When they
            # also ask for a price ("could I get a ballpark before I come
            # in?"), the desk pursues both in one email after the owner's
            # approval: the offer card carries the questions, and the email
            # that offers the times asks them (the owner's rule, 8 September
            # 2026: nothing reaches the customer before the approval).
            intent = appointment_intent(p, digest, paths, model, judge_runner, openclaw, estimate_id=estimate_id)
            if estimate_record.asks_for_estimate(handled_words):
                intent["ask_for"] = question_lines(reviewed["missing_required_fields"], specification)
            intent_path = Path(paths["appointment_intent"])
            workflow_safe.write_private(intent_path, intent)
            workflow_safe.request_appointment_approval(argparse.Namespace(
                monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
                shop_profile=p.get("shop_profile"), message_id=message_id, estimate_id=estimate_id,
                appointment_intent=intent_path, appointment_approval=Path(paths["appointment_approval"]),
                record_output=Path(paths["current_record"]), defer_finalize_for_rendering=False,
                runner=command_runner, judge_runner=judge_runner,
            ))
            return {"outcome": "appointment_approval_requested", "before_estimate": True,
                    "asks": list(intent.get("ask_for") or []), "next": "done"}
        # A shop email later in the thread than this message means the desk's question went out after the
        # customer wrote (a second email before the first tick): the thread's order says so in the fake and live.
        messages = digest.get("messages") or []
        claimed_at = next((i for i, m in enumerate(messages) if m.get("claimed")), None)
        predates = claimed_at is not None and any(m.get("sent_by") == "shop" for m in messages[claimed_at + 1:])
        if not reviewed["initiating"] and predates:
            asked_fields, _sent_at = estimate_record.last_ask(record)
            if set(reviewed["missing_required_fields"]) <= asked_fields:
                # Written before the desk's question went out (a second email before the first tick): its
                # facts are on the record, the question already covers what is left, nothing more is sent.
                token = inbox_claim.authoritative_claim_token(p["claim_root"], message_id)
                kolo_safe.complete_claimed(p["monitor_root"], p["claim_root"], message_id, token)
                return {"outcome": "read_before_the_ask", "next": "done"}
        repeated = estimate_record.followup_stalled(record, message_id, reviewed["missing_required_fields"], predates_ask=predates)
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
            reviewed["initiating"], paths, profile, model, judge_runner, openclaw, command_runner, photos=photos,
        )
    if nxt == "price":
        return _price_after_review(p, message_id, estimate_id, specification, reviewed, model, judge_runner, openclaw, command_runner)
    raise ValueError(f"review-thread returned an unknown next step {nxt!r}")


INVENTORY_REPLY_LIMIT = 2


def _inventory_inquiry(
    p: dict[str, Path], message_id: str, estimate_id: str, record: dict[str, Any], digest: dict[str, Any], paths: dict[str, str],
    model: str | None, judge_runner: Runner, openclaw: str | None, command_runner: Runner, triage: dict[str, Any],
) -> dict[str, Any]:
    """A ready-made inquiry (WORKFLOW.md triage table): offer a visit; after two replies without a booking, the owner.

    The shop shows what is in stock at a visit, so the desk never quotes or
    questions such a customer: it offers times (or books the time they name)
    through the usual appointment card. When two replies have gone out and
    nothing is booked, the desk tells the owner to open the email and
    handle it, and leaves the thread to them.
    """
    if not record.get("inventory_inquiry"):
        record = estimate_record.mark_inventory_inquiry(p["record_root"], estimate_id, message_id, triage.get("note") or "")
    shop_replies = sum(1 for m in digest.get("messages") or [] if m.get("sent_by") == "shop")
    if shop_replies >= INVENTORY_REPLY_LIMIT:
        kolo_safe.manual_review_claimed(p["monitor_root"], p["claim_root"], message_id, None, "inventory_handoff", runner=command_runner)
        if record.get("status") in route_ownership.ACTIVE_STATUSES and record.get("status") not in workflow_safe.SENT_STATUSES:
            estimate_record.retire(p["record_root"], estimate_id, "owner_handles_thread",
                                   "ready-made inquiry: two replies went out without a booking; the owner handles the thread")
        return {"outcome": "owner_handoff", "reason_code": "inventory_handoff", "next": "done"}
    intent_path = Path(paths["appointment_intent"])
    workflow_safe.write_private(intent_path, appointment_intent(p, digest, paths, model, judge_runner, openclaw, estimate_id=estimate_id))
    workflow_safe.request_appointment_approval(argparse.Namespace(
        monitor_root=p["monitor_root"], claim_root=p["claim_root"], record_root=p["record_root"],
        shop_profile=p.get("shop_profile"), message_id=message_id, estimate_id=estimate_id,
        appointment_intent=intent_path, appointment_approval=Path(paths["appointment_approval"]),
        record_output=Path(paths["current_record"]), defer_finalize_for_rendering=False,
        runner=command_runner, judge_runner=judge_runner,
    ))
    return {"outcome": "appointment_approval_requested", "before_estimate": True, "inventory": True, "next": "done"}


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
