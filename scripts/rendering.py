#!/usr/bin/env python3
"""Renderings that hold together: plan, assemble, render, check.

One cheap judgment maps a request onto a closed list of construction
archetypes (templates/render/*.json). Code assembles the prompt from that
archetype's fixed clauses plus the specification. The image model renders
two views, with the customer's artwork and an optional exemplar attached as
reference images. A vision model then answers the archetype's yes-or-no
questions about each render; a failing render is regenerated once with the
failed questions named. Nothing here touches desk state, so it can be run
on its own (scripts/render_lab.py) or from the pipeline.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import judge

Runner = Callable[..., subprocess.CompletedProcess[str]]
ARCHETYPE_DIR = Path(__file__).resolve().parent.parent / "templates" / "render"
# The model that grades the views, named explicitly: a one-shot command job
# resolved the environment's default to a model the instance had no right to
# use (403, 6 September 2026), while this one is the model the pod's own
# image tool reports. The profile may pin another (rendering.vision_model).
DEFAULT_VISION_MODEL = "litellm/kolo-best-available"
_CHECK_LOCK = threading.Lock()
DESCRIBE_TRIES = 3
DESCRIBE_PAUSE_SECONDS = 3
IMAGE_TIMEOUT_MS = 180_000
# The watcher tick is 300 s (cron_config.WATCHER_TIMEOUT_SECONDS). A view is
# one image call plus a vision check, regenerated once on a failed check: up
# to 900 s in the worst case, which killed a tick live on 7 September 2026.
# With a deadline the step never starts what it cannot finish: no render
# with under START_MIN_SECONDS left, no regeneration with under
# REGENERATE_MIN_SECONDS left, no vision retry with under RETRY_MIN_SECONDS
# left, and the image call's own timeout shrinks to what remains.
START_MIN_SECONDS = 120
REGENERATE_MIN_SECONDS = 200
RETRY_MIN_SECONDS = 100
IMAGE_MIN_TIMEOUT_MS = 60_000


def remaining_seconds(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()
MARK_SOURCES = ("artwork", "initials", "none")


def archetypes() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for path in sorted(ARCHETYPE_DIR.glob("*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("id"):
            exemplar = path.with_name(f"{value['id']}.exemplar.png")
            value["exemplar"] = str(exemplar) if exemplar.exists() else None
            found[value["id"]] = value
    return found


def spec_text(specification: dict[str, Any] | str) -> str:
    if isinstance(specification, str):
        return specification.strip()[:1200]
    return ", ".join(
        f"{key.replace('_', ' ')}: {value}" for key, value in sorted(specification.items())
        if value not in (None, "", []) and not isinstance(value, bool)
    )[:1200]


def check_plan(known: list[str]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def check(value: dict[str, Any]) -> dict[str, Any]:
        archetype = value.get("archetype")
        if archetype not in known:
            raise ValueError("archetype must be one of: " + ", ".join(known))
        source = value.get("mark_source", "none")
        if source not in MARK_SOURCES:
            raise ValueError("mark_source must be artwork, initials, or none")
        exact = value.get("must_be_exact", [])
        if not isinstance(exact, list) or any(not isinstance(x, str) for x in exact):
            raise ValueError("must_be_exact must be a list of short strings")
        return {
            "archetype": archetype,
            "mark_source": source,
            "must_be_exact": [x.strip()[:80] for x in exact][:6],
            "fine_lettering": bool(value.get("fine_lettering")),
            "notes": str(value.get("notes") or "")[:200],
        }
    return check


def plan_render(
    specification: dict[str, Any] | str,
    context: str = "",
    has_artwork: bool = False,
    model: str | None = None,
    runner: Runner = subprocess.run,
    openclaw: str | None = None,
) -> dict[str, Any]:
    """Which archetype, where the mark comes from, what must be exact."""
    known = archetypes()
    menu = "; ".join(f"{k} = {v['label']}" for k, v in known.items())
    prompt = (
        "You plan a product rendering for a custom-jewelry shop. Choose the construction archetype that best "
        f"fits the piece from this closed list and nothing else: {menu}. Say where any mark comes from: "
        "\"artwork\" when the customer supplied a logo or drawing" + (" (they did)" if has_artwork else " (none was supplied)") +
        ", \"initials\" when letters are given in words, \"none\" otherwise. List up to six things that must be "
        "exact in the render (stone shape, metal color, the mark, an engraving). Say whether the mark has fine "
        "lettering or small detail that image models usually get wrong. Answer with one JSON object only: "
        '{"archetype": "<id>", "mark_source": "artwork|initials|none", "must_be_exact": ["..."], '
        '"fine_lettering": true|false, "notes": "<one line>"}\n\n'
        f"SPECIFICATION: {spec_text(specification)}\n\n" + (f"CONTEXT:\n{context[:3000]}\n" if context else "")
    )
    return judge.ask_json(prompt, check_plan(list(known)), model, runner, openclaw)


def build_prompts(plan: dict[str, Any], specification: dict[str, Any] | str, has_artwork: bool, has_exemplar: bool) -> list[str]:
    """Two prompts assembled from the archetype's clauses; the model never writes them."""
    arch = archetypes()[plan["archetype"]]
    refs = []
    if has_artwork:
        refs.append("Image one is the customer's mark: reproduce it exactly, letter for letter and shape for shape, do not restyle it.")
    if has_exemplar:
        refs.append(f"Image {'two' if has_artwork else 'one'} shows the construction to follow; copy how it holds together, not its design.")
    exact = plan.get("must_be_exact") or []
    exact_clause = (" Must be exact: " + "; ".join(exact) + ".") if exact else ""
    base = (
        f"{arch['photo']} The piece: {arch['label']}. {arch['construction']} "
        f"Specification: {spec_text(specification)}.{exact_clause} "
        + " ".join(refs) + " Exactly as specified, one design, no alternates, no text in the image."
    )
    return [f"{base} View: {view}." for view in arch["views"][:2]]


def image_argv(prompt: str, refs: list[Path], output: Path, openclaw: str, model: str | None = None,
               timeout_ms: int = IMAGE_TIMEOUT_MS) -> list[str]:
    argv = [openclaw, "infer", "image"]
    if refs:
        argv.append("edit")
        for ref in refs:
            argv += ["--file", str(ref)]
    else:
        argv.append("generate")
    argv += ["--prompt", prompt, "--size", "1024x1024", "--output", str(output), "--timeout-ms", str(int(timeout_ms)), "--json"]
    if model:
        argv += ["--model", model]
    return argv


def _envelope(stdout: str) -> dict[str, Any]:
    raw = stdout or ""
    start = raw.find("{")
    if start < 0:
        raise ValueError("image command returned no JSON")
    value = json.loads(raw[start:])
    if not isinstance(value, dict) or value.get("ok") is False:
        raise ValueError(str(value.get("error") if isinstance(value, dict) else value)[:200] or "image command failed")
    return value


def render(prompt: str, refs: list[Path], output: Path, openclaw: str, runner: Runner = subprocess.run,
           model: str | None = None, deadline: float | None = None) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = IMAGE_TIMEOUT_MS
    left = remaining_seconds(deadline)
    if left is not None:
        timeout_ms = int(min(IMAGE_TIMEOUT_MS, max(IMAGE_MIN_TIMEOUT_MS, (left - 30) * 1000)))
    completed = runner(image_argv(prompt, refs, output, openclaw, model, timeout_ms), check=True, capture_output=True, text=True, shell=False)
    envelope = _envelope(completed.stdout)
    outputs = envelope.get("outputs") or []
    path = outputs[0].get("path") if outputs and isinstance(outputs[0], dict) else None
    if not isinstance(path, str) or not path:
        raise ValueError("image command returned no file path")
    return Path(path)


def describe_argv(image: Path, prompt: str, openclaw: str, vision_model: str | None) -> list[str]:
    argv = [openclaw, "infer", "image", "describe", "--file", str(image), "--prompt", prompt, "--json",
            "--timeout-ms", "90000"]
    if vision_model:
        argv += ["--model", vision_model]
    return argv


def _describe_text(stdout: str) -> str:
    raw = stdout or ""
    start = raw.find("{")
    if start >= 0:
        try:
            value = json.loads(raw[start:])
            if isinstance(value, dict):
                outputs = value.get("outputs")
                if isinstance(outputs, list) and outputs and isinstance(outputs[0], dict) and outputs[0].get("text"):
                    return str(outputs[0]["text"])
                for key in ("text", "description", "result"):
                    if isinstance(value.get(key), str):
                        return value[key]
        except (ValueError, json.JSONDecodeError):
            pass
    return raw


def check_image(image: Path, plan: dict[str, Any], openclaw: str, runner: Runner = subprocess.run,
                vision_model: str | None = DEFAULT_VISION_MODEL, reference: Path | None = None,
                deadline: float | None = None) -> dict[str, Any]:
    """The archetype's questions answered yes or no about one render."""
    arch = archetypes()[plan["archetype"]]
    questions = "\n".join(f"- {c['id']}: {c['question']}" for c in arch["checks"])
    prompt = (
        "You are checking a product rendering of custom jewelry for a jeweler. Answer each question with yes "
        "or no, strictly, and add one short note per no. "
        + ("A reference mark was supplied by the customer; compare the mark on the piece to it from memory of the "
           "description: " + "; ".join(plan.get("must_be_exact") or []) + ". " if plan.get("mark_source") == "artwork" else "")
        + 'Answer with one JSON object only: {"answers": {"<id>": "yes"|"no"}, "notes": {"<id>": "<why>"}}\n\n'
        f"QUESTIONS:\n{questions}"
    )
    # The vision call fails now and then on the pod (exit 1, the same command
    # succeeds a moment later; 6 September 2026). Try again with a pause; if it
    # keeps failing the view goes to the owner unchecked rather than the
    # whole rendering dying and the owner being asked.
    completed = None
    last_error: Exception | None = None
    for attempt in range(DESCRIBE_TRIES):
        try:
            completed = runner(describe_argv(image, prompt, openclaw, vision_model), check=True, capture_output=True, text=True, shell=False)
            break
        except (OSError, subprocess.CalledProcessError) as exc:
            last_error = exc
            left = remaining_seconds(deadline)
            if left is not None and left < RETRY_MIN_SECONDS:
                break  # no time for another try inside this tick: the view goes to the owner unchecked
            if attempt + 1 < DESCRIBE_TRIES:
                time.sleep(DESCRIBE_PAUSE_SECONDS * (attempt + 1))
    if completed is None:
        detail = ""
        if isinstance(last_error, subprocess.CalledProcessError):
            detail = (last_error.stderr or last_error.stdout or "").strip()[:160]
        return {"answers": {}, "failed": [], "unsure": [c["id"] for c in arch["checks"]], "notes": {},
                "unchecked": True, "error": f"vision check unavailable after {DESCRIBE_TRIES} tries" + (f": {detail}" if detail else "")}
    text = _describe_text(completed.stdout)
    try:
        value = judge.extract_json(text)
    except ValueError:
        value = {}
    answers_raw = value.get("answers") if isinstance(value, dict) else None
    answers: dict[str, str] = {}
    for check in arch["checks"]:
        raw = str((answers_raw or {}).get(check["id"], "")).strip().lower() if isinstance(answers_raw, dict) else ""
        answers[check["id"]] = "yes" if raw.startswith("y") else ("no" if raw.startswith("n") else "unsure")
    notes = value.get("notes") if isinstance(value, dict) and isinstance(value.get("notes"), dict) else {}
    failed = [c["id"] for c in arch["checks"] if answers[c["id"]] == "no"]
    unsure = [c["id"] for c in arch["checks"] if answers[c["id"]] == "unsure"]
    return {"answers": answers, "notes": {k: str(v)[:160] for k, v in notes.items()}, "failed": failed, "unsure": unsure,
            "raw": text[:600]}


def pieces_named(note: str, labels: list[str]) -> list[str]:
    """The piece labels the owner's words name ("the band", "ring"); none named means every piece."""
    words = " " + re.sub(r"[^a-z0-9 ]+", " ", (note or "").lower()) + " "
    named = []
    for label in labels:
        tokens = [t for t in re.sub(r"[^a-z0-9 ]+", " ", label.lower()).split() if len(t) > 2 and t not in {"the", "and", "with"}]
        if any(f" {t} " in words or f" {t}s " in words for t in tokens):
            named.append(label)
    return named


def _with_change(piece: dict[str, Any], change: str) -> dict[str, Any]:
    if not change:
        return piece
    notes = str(piece.get("notes") or "").strip()
    return {**piece, "notes": (notes + (" " if notes else "") + f"Owner's revision: {change.strip()}").strip()}


def run_pieces(
    specification: dict[str, Any],
    out_dir: Path,
    openclaw: str = "openclaw",
    artwork: Path | None = None,
    context: str = "",
    model: str | None = None,
    vision_model: str | None = DEFAULT_VISION_MODEL,
    image_model: str | None = None,
    runner: Runner = subprocess.run,
    change: str = "",
    only: list[str] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One plan and its views per piece (MULTI-PIECE-PLAN.md batch 3); one piece is `run` unchanged.

    Two pieces get two views each; three or four get one each, so a card never
    carries more than four images. A matching set tells the planner so the
    pieces share a design language. A revision (`change`, the owner's words)
    re-renders the pieces in `only` (every piece when empty) with the words
    in the prompt and keeps the other pieces' views from `previous`.
    """
    import estimate_record  # local import: keeps rendering usable from the lab without the desk

    pieces = estimate_record.pieces_of(specification)
    if len(pieces) <= 1:
        report = run(_with_change(specification, change), out_dir, openclaw, artwork=artwork, context=context,
                     model=model, vision_model=vision_model, image_model=image_model, runner=runner)
        for view in report["views"]:
            view.setdefault("piece", estimate_record.piece_label(specification, 0))
        report["pieces"] = [{"label": estimate_record.piece_label(specification, 0), "plan": report["plan"]}]
        if change:
            report["revision"] = change
        return report
    views_each = 2 if len(pieces) <= 2 else 1
    set_note = " The pieces are a matching set: one design language, the same metal finish and motifs, each piece its own size." \
        if estimate_record.is_set(specification) else ""
    kept = {}
    if change and only:
        for view in (previous or {}).get("views") or []:
            if view.get("piece") not in only and Path(str(view.get("image") or "")).exists():
                kept.setdefault(view["piece"], []).append(view)
    combined: dict[str, Any] = {"pieces": [], "views": [], "prompts": [], "references": []}
    slot = 1
    for index, piece in enumerate(pieces[:4]):
        label = estimate_record.piece_label(specification, index)
        if label in kept:
            previous_plan = next((pc.get("plan") for pc in (previous or {}).get("pieces") or [] if pc.get("label") == label), None)
            combined["pieces"].append({"label": label, "plan": previous_plan or {}, "kept": True})
            for view in kept[label]:
                combined["views"].append({**view, "slot": slot, "piece": label, "kept": True})
                slot += 1
            continue
        piece_spec = {**piece, "notes": (str(piece.get("notes") or "") + set_note).strip()}
        if change and (not only or label in only):
            piece_spec = _with_change(piece_spec, change)
        one = run(piece_spec, out_dir / f"piece-{index + 1}",
                  openclaw, artwork=artwork, context=context, model=model, vision_model=vision_model,
                  image_model=image_model, runner=runner, views=views_each)
        combined["pieces"].append({"label": label, "plan": one["plan"]})
        combined["prompts"].extend(one["prompts"])
        combined["references"] = one["references"]
        for view in one["views"]:
            combined["views"].append({**view, "slot": slot, "piece": label})
            slot += 1
    combined["plan"] = next((pc["plan"] for pc in combined["pieces"] if pc.get("plan")), {})
    combined["all_passed"] = all(v["passed"] for v in combined["views"])
    if change:
        combined["revision"] = change
    return combined


def plan_piece(
    specification: dict[str, Any] | str,
    out_dir: Path,
    openclaw: str = "openclaw",
    artwork: Path | None = None,
    archetype: str | None = None,
    context: str = "",
    model: str | None = None,
    runner: Runner = subprocess.run,
    views: int = 2,
) -> dict[str, Any]:
    """The plan for one piece and the views to render from it, nothing rendered yet.

    One model call. The result is plain data that can be written to disk and
    picked up by a later run: each view carries its slot, prompt, plan, and
    reference files, so the views can be rendered one at a time across ticks.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if archetype:
        known = archetypes()
        if archetype not in known:
            raise ValueError("unknown archetype: " + archetype)
        plan = {"archetype": archetype, "mark_source": "artwork" if artwork else "none", "must_be_exact": [],
                "fine_lettering": False, "notes": "archetype given by the operator"}
    else:
        plan = plan_render(specification, context, artwork is not None, model, runner, openclaw)
    arch = archetypes()[plan["archetype"]]
    refs: list[Path] = []
    if artwork:
        refs.append(Path(artwork))
    if arch.get("exemplar"):
        refs.append(Path(arch["exemplar"]))
    prompts = build_prompts(plan, specification, artwork is not None, bool(arch.get("exemplar")))[:max(1, views)]
    return {
        "plan": plan, "prompts": prompts, "references": [str(r) for r in refs], "out_dir": str(out_dir),
        "views": [{"slot": slot, "prompt": prompt, "out_dir": str(out_dir)} for slot, prompt in enumerate(prompts, start=1)],
    }


def render_view(
    view: dict[str, Any], plan: dict[str, Any], refs: list[Path], openclaw: str = "openclaw",
    artwork: Path | None = None, vision_model: str | None = DEFAULT_VISION_MODEL, image_model: str | None = None,
    runner: Runner = subprocess.run, max_regenerations: int = 1, deadline: float | None = None,
) -> dict[str, Any]:
    """Render one planned view, check it, regenerate a failing one once. One view, one tick's worth of work.

    With a `deadline` (monotonic seconds) the step keeps to the tick's clock:
    it returns `{"deferred": True}` without rendering when too little time is
    left to start, skips the regeneration when there is no room for one, and
    the vision check stops retrying. What it did is always recorded.
    """
    out_dir = Path(view["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    slot = int(view["slot"])
    prompt = str(view["prompt"])
    attempts = []
    current_prompt = prompt
    image = None
    check = None
    left = remaining_seconds(deadline)
    if left is not None and left < START_MIN_SECONDS:
        return {"slot": slot, "deferred": True, "seconds_left": round(left)}
    for attempt in range(max_regenerations + 1):
        if attempt:
            left = remaining_seconds(deadline)
            if left is not None and left < REGENERATE_MIN_SECONDS:
                break  # the failed view stands as it is; the owner sees the checker line and can ask for a revision
        image = render(current_prompt, refs, out_dir / f"view-{slot}-try-{attempt + 1}.png", openclaw, runner, image_model, deadline)
        with _CHECK_LOCK:
            check = check_image(image, plan, openclaw, runner, vision_model, artwork, deadline)
        attempts.append({"image": str(image), "check": check, "prompt": current_prompt})
        if not check["failed"]:
            break
        problems = "; ".join(f"{cid}: {check['notes'].get(cid, 'failed')}" for cid in check["failed"])
        current_prompt = prompt + f" Correct these problems from the previous attempt: {problems}."
    return {
        "slot": slot, "image": str(image), "passed": not check["failed"], "failed": check["failed"],
        "unsure": check["unsure"], "notes": check["notes"], "attempts": len(attempts), "history": attempts,
        **({"unchecked": True, "check_error": check.get("error")} if check.get("unchecked") else {}),
    }


def run(
    specification: dict[str, Any] | str,
    out_dir: Path,
    openclaw: str = "openclaw",
    artwork: Path | None = None,
    archetype: str | None = None,
    context: str = "",
    model: str | None = None,
    vision_model: str | None = DEFAULT_VISION_MODEL,
    image_model: str | None = None,
    runner: Runner = subprocess.run,
    max_regenerations: int = 1,
    views: int = 2,
) -> dict[str, Any]:
    """Plan, render the views, check each, regenerate a failing one once. Returns the report.

    The lab entry point (one call, side by side). The desk itself renders
    one view per tick through `plan_piece` and `render_view`.
    """
    planned = plan_piece(specification, out_dir, openclaw, artwork=artwork, archetype=archetype, context=context,
                         model=model, runner=runner, views=views)
    refs = [Path(r) for r in planned["references"]]
    report: dict[str, Any] = {"plan": planned["plan"], "prompts": planned["prompts"], "references": planned["references"], "views": []}
    with ThreadPoolExecutor(max_workers=max(1, len(planned["views"]))) as pool:
        futures = [pool.submit(render_view, view, planned["plan"], refs, openclaw, artwork, vision_model, image_model,
                               runner, max_regenerations) for view in planned["views"]]
        report["views"] = [f.result() for f in futures]
    report["all_passed"] = all(v["passed"] for v in report["views"])
    return report
