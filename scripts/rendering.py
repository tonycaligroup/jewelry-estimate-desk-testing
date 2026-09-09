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

import cli
import image_provider
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


# Kolo's probe, 7 September 2026: the CLI's --timeout-ms is not honoured (one
# generate ran 351 s) and two openclaw commands at once fail at once with
# "database is locked" (the CLI's SQLite state). So the desk cuts a call off
# itself at the tick's deadline (the tick is never killed), and a locked call
# is tried again a few seconds later inside the same tick.
LOCK_TEXT, LOCK_TRIES, LOCK_PAUSE_SECONDS = cli.LOCK_TEXT, cli.LOCK_TRIES, cli.LOCK_PAUSE_SECONDS
CUTOFF_MARGIN_SECONDS, CUTOFF_MIN_SECONDS = cli.CUTOFF_MARGIN_SECONDS, cli.CUTOFF_MIN_SECONDS


def run_cli(argv: list[str], runner: Runner, deadline: float | None, what: str) -> subprocess.CompletedProcess:
    """One openclaw command through the shared runner (cli.py): cutoff at the deadline, retry on the lock."""
    return cli.run(argv, runner, deadline, what)
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


STONE_COLOR_WORDS = {"emerald": "green", "sapphire": "blue", "ruby": "red", "diamond": "white, colorless", "amethyst": "purple",
                     "aquamarine": "pale blue", "tanzanite": "violet-blue", "garnet": "deep red", "morganite": "peach-pink",
                     "peridot": "yellow-green", "citrine": "yellow", "topaz": "blue", "opal": "iridescent white", "pearl": "white",
                     "moissanite": "white, colorless", "tourmaline": "green", "spinel": "red", "onyx": "black", "turquoise": "turquoise"}
ARCHETYPE_WORDS = (
    ("stud", "stud_earrings"), ("hoop", "hoop_earrings"), ("huggie", "hoop_earrings"), ("drop earring", "drop_earrings"),
    ("dangle", "drop_earrings"), ("tennis", "tennis_bracelet"), ("signet", "signet"), ("eternity", "eternity_band"),
    ("three stone", "three_stone_ring"), ("three-stone", "three_stone_ring"), ("solitaire", "solitaire_ring"),
    ("locket", "locket"), ("cufflink", "cufflinks"), ("cuff link", "cufflinks"), ("brooch", "brooch"), ("tie bar", "tie_bar"),
)


def archetype_for(specification: dict[str, Any]) -> str | None:
    """An archetype the piece's own words settle ("stud earrings" is never drops); None leaves it to the planner."""
    if not isinstance(specification, dict):
        return None
    # The piece's name and setting only: notes are prose ("not hoops, please") and never settle an archetype.
    words = " ".join(str(specification.get(k) or "") for k in ("piece_type", "setting_style")).lower()
    known = archetypes()
    for word, archetype in ARCHETYPE_WORDS:
        if word in words and archetype in known:
            return archetype
    return None


def exact_facts(specification: dict[str, Any] | str) -> list[str]:
    """What the render must get right, from the ledger's facts, in the order that matters: the piece, each stone, the metal, the setting.

    Live (8 September 2026): emerald halo studs rendered as diamond drops.
    The stone's colour and the piece's kind open the prompt from now on and
    the checker asks about them by name.
    """
    if not isinstance(specification, dict):
        return []
    spec = specification
    facts: list[str] = []
    piece = str(spec.get("piece_type") or "").strip()
    if piece:
        facts.append(f"the piece is {piece}")
    stone = str(spec.get("stone_type") or "").strip().lower()
    center_raw = spec.get("center_stone")
    has_center = not (center_raw is False or str(center_raw if center_raw is not None else "").strip().lower() in ("no", "none", "false"))
    if stone and has_center:
        colour = str(spec.get("stone_color") or "").strip()
        colour_words = colour if colour and colour.lower() != "jeweler's choice" and len(colour) > 2 else STONE_COLOR_WORDS.get(stone, "")
        shape = str(spec.get("stone_shape") or spec.get("stone_cut") or "").strip()
        carat = spec.get("stone_carat")
        parts = [w for w in (colour_words, stone) if w]
        basis = str(spec.get("stone_carat_basis") or "").lower()
        carat_words = "" if not carat else (f", {carat} ct each" if basis == "each" else f", {carat} ct total for the pair" if basis == "total"
                                            else f", {carat} ct")
        desc = " ".join(parts) + " center stone" + (f", {shape}" if shape else "") + carat_words
        origin = str(spec.get("stone_origin") or "").strip()
        facts.append(desc + (f" ({origin})" if origin else ""))
    accents = str(spec.get("accent_stones") or "").strip()
    accent_type = str(spec.get("accent_stone_type") or "").strip().lower()
    if accents:
        facts.append(f"accent stones: {accents}")
    elif accent_type:
        facts.append(f"accent stones: {STONE_COLOR_WORDS.get(accent_type, '')} {accent_type}".strip())
    metal = " ".join(str(spec.get(k) or "").strip() for k in ("metal_karat", "metal_color", "metal") if spec.get(k)).strip()
    metal_color = str(spec.get("metal_color") or "").strip()
    if metal_color and metal_color.lower() not in metal.lower():
        metal = f"{metal_color} {metal}".strip()
    if metal:
        facts.append(f"metal: {metal}")
    setting = str(spec.get("setting_style") or "").strip()
    if setting and setting.lower() != "jeweler's choice":
        facts.append(f"setting: {setting}")
    return [re.sub(r"\s+", " ", f) for f in facts][:6]


def exact_checks(facts: list[str]) -> list[dict[str, str]]:
    """One yes-or-no question per must-be-exact fact, so the checker asks about the stone and the piece by name."""
    checks = []
    for fact in facts:
        ident = "exact_" + re.sub(r"[^a-z0-9]+", "_", fact.lower()).strip("_")[:40]
        checks.append({"id": ident, "question": f"Does the render show exactly this: {fact}? Answer no if the stone colour, the kind of piece, or the metal differs."})
    return checks


def all_checks(plan: dict[str, Any]) -> list[dict[str, str]]:
    return list(archetypes()[plan["archetype"]]["checks"]) + [c for c in (plan.get("exact_checks") or []) if isinstance(c, dict)]


def build_prompts(plan: dict[str, Any], specification: dict[str, Any] | str, has_artwork: bool, has_exemplar: bool) -> list[str]:
    """Two prompts assembled from the archetype's clauses; the model never writes them."""
    arch = archetypes()[plan["archetype"]]
    refs = []
    example = has_artwork and plan.get("reference_kind") == "example"
    if has_artwork and not example:
        refs.append("Image one is the customer's mark: reproduce it exactly, letter for letter and shape for shape, do not restyle it.")
    if has_exemplar:
        refs.append(f"Image {'two' if has_artwork else 'one'} shows the construction to follow; copy how it holds together, not its design.")
    exact = plan.get("must_be_exact") or []
    exact_clause = (" Must be exact: " + "; ".join(exact) + ".") if exact else ""
    if example:
        # The photograph is the design (the owner, 9 September 2026: renders drifted from the reference). The
        # prompt names only what changes, never the whole specification or an archetype's own views, so the
        # model edits the picture instead of re-imagining the piece; the first view is the photograph's own.
        changes = "; ".join(exact) if exact else f"the piece: {arch['label']}; specification: {spec_text(specification)}"
        base = (
            f"{arch['photo']} Image one is the customer's example piece: this is an edit of that photograph. Keep the "
            f"same design, construction, proportions, stone layout, and finish as the photograph, changed only as "
            f"follows: {changes}. " + " ".join(refs) + " Change nothing else, one design, no alternates, no text in the image."
        )
        return [f"{base} View: {view}." for view in EXAMPLE_VIEWS]
    base = (
        f"{arch['photo']}{exact_clause} The piece: {arch['label']}. {arch['construction']} "
        f"Specification: {spec_text(specification)}. "
        + " ".join(refs) + " Exactly as specified, one design, no alternates, no text in the image."
    )
    return [f"{base} View: {view}." for view in arch["views"][:2]]


# An example photograph keeps its own view; the second view turns the same piece a little.
EXAMPLE_VIEWS = ("the same view and framing as the photograph",
                 "the same piece turned to a three-quarter view, everything else as in the photograph")
# Edits keep the reference's own features (a reported gpt-image-1 option; a provider that does not know it gets the
# edit again without it).
EDIT_INPUT_FIDELITY = "high"


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


PROVIDER_MODE = "auto"  # set from the profile by the desk (rendering.provider); "cli" forces the platform CLI
IMAGE_SIZE = "1024x1024"  # rendering.size; size costs nothing (probe, 8 September 2026)
IMAGE_QUALITY = "auto"  # rendering.quality; "high" costs 80 to 130 s per image against 15 to 25 for auto


def render(prompt: str, refs: list[Path], output: Path, openclaw: str, runner: Runner = subprocess.run,
           model: str | None = None, deadline: float | None = None) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if image_provider.available(PROVIDER_MODE):
        seconds = image_provider.timed(remaining_seconds(deadline), IMAGE_TIMEOUT_MS / 1000)
        return image_provider.generate(prompt, output, model=model, refs=refs or None, timeout=seconds,
                                       size=IMAGE_SIZE, quality=IMAGE_QUALITY,
                                       input_fidelity=EDIT_INPUT_FIDELITY if refs else None)
    timeout_ms = IMAGE_TIMEOUT_MS
    left = remaining_seconds(deadline)
    if left is not None:
        timeout_ms = int(min(IMAGE_TIMEOUT_MS, max(IMAGE_MIN_TIMEOUT_MS, (left - 30) * 1000)))
    completed = run_cli(image_argv(prompt, refs, output, openclaw, model, timeout_ms), runner, deadline, "image generation")
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


def describe_example(image: Path, prompt: str, openclaw: str, runner: Runner = subprocess.run,
                     vision_model: str | None = DEFAULT_VISION_MODEL, deadline: float | None = None) -> str:
    """Plain text about one customer photo (intake, 8 September 2026); empty when the vision model cannot be reached."""
    if image_provider.available(PROVIDER_MODE):
        try:
            text = image_provider.describe(image, prompt, model=vision_model,
                                           timeout=image_provider.timed(remaining_seconds(deadline), 90))
        except OSError:
            return ""
        return " ".join(str(text or "").split())[:1200]
    for attempt in range(DESCRIBE_TRIES):
        try:
            completed = run_cli(describe_argv(image, prompt, openclaw, vision_model), runner, deadline, "photo reading")
            return " ".join(_describe_text(completed.stdout).split())[:1200]
        except (OSError, subprocess.CalledProcessError):
            left = remaining_seconds(deadline)
            if left is not None and left < RETRY_MIN_SECONDS:
                break
            if attempt + 1 < DESCRIBE_TRIES:
                time.sleep(DESCRIBE_PAUSE_SECONDS * (attempt + 1))
    return ""


def check_image(image: Path, plan: dict[str, Any], openclaw: str, runner: Runner = subprocess.run,
                vision_model: str | None = DEFAULT_VISION_MODEL, reference: Path | None = None,
                deadline: float | None = None) -> dict[str, Any]:
    """The archetype's questions answered yes or no about one render."""
    arch = {"checks": all_checks(plan)}
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
    if image_provider.available(PROVIDER_MODE):
        try:
            text = image_provider.describe(image, prompt, model=vision_model,
                                           timeout=image_provider.timed(remaining_seconds(deadline), 90))
        except OSError as exc:
            return {"answers": {}, "failed": [], "unsure": [c["id"] for c in arch["checks"]], "notes": {},
                    "unchecked": True, "error": f"vision check unavailable: {str(exc)[:160]}"}
        return _judge_answers(text, arch)
    completed = None
    last_error: Exception | None = None
    for attempt in range(DESCRIBE_TRIES):
        try:
            completed = run_cli(describe_argv(image, prompt, openclaw, vision_model), runner, deadline, "vision check")
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
    return _judge_answers(_describe_text(completed.stdout), arch)


def _judge_answers(text: str, arch: dict[str, Any]) -> dict[str, Any]:
    """The vision model's text as yes/no per check, however it was obtained."""
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
        settled = archetype_for(specification if isinstance(specification, dict) else {})
        if settled and settled != plan.get("archetype"):
            plan = {**plan, "archetype": settled, "notes": f"archetype from the piece's own words; the planner said {plan.get('archetype')}"}
    facts = exact_facts(specification)
    plan = {**plan, "must_be_exact": facts + [x for x in (plan.get("must_be_exact") or []) if x not in facts][: max(0, 8 - len(facts))],
            "exact_checks": exact_checks(facts)}
    if artwork:
        # The customer's photo is an example piece (the reading said so, or the planner saw no mark in it): the
        # render is that photograph, changed as specified. A logo or drawing stays a mark to reproduce.
        reference = str(specification.get("reference_images") or "").lower() if isinstance(specification, dict) else ""
        example = reference.startswith("from the photo") or "example" in reference or plan.get("mark_source") != "artwork"
        plan["reference_kind"] = "example" if example else "mark"
    arch = archetypes()[plan["archetype"]]
    refs: list[Path] = []
    if artwork:
        refs.append(Path(artwork))
    # An archetype's exemplar shows construction; beside the customer's example photograph it would compete
    # with the design they sent, so an example render carries their photograph alone.
    with_exemplar = bool(arch.get("exemplar")) and plan.get("reference_kind") != "example"
    if with_exemplar:
        refs.append(Path(arch["exemplar"]))
    prompts = build_prompts(plan, specification, artwork is not None, with_exemplar)[:max(1, views)]
    return {
        "plan": plan, "prompts": prompts, "references": [str(r) for r in refs], "out_dir": str(out_dir),
        "views": [{"slot": slot, "prompt": prompt, "out_dir": str(out_dir)} for slot, prompt in enumerate(prompts, start=1)],
    }


def render_view(
    view: dict[str, Any], plan: dict[str, Any], refs: list[Path], openclaw: str = "openclaw",
    artwork: Path | None = None, vision_model: str | None = DEFAULT_VISION_MODEL, image_model: str | None = None,
    runner: Runner = subprocess.run, max_regenerations: int = 1, deadline: float | None = None, check: bool = True,
) -> dict[str, Any]:
    """Render one planned view, check it, regenerate a failing one once. One view, one tick's worth of work.

    With `check` off (the profile's rendering.vision_check false) the view is
    rendered once and carded as not machine-checked: the owner is the check.

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
    verdict: dict[str, Any] | None = None
    left = remaining_seconds(deadline)
    if left is not None and left < START_MIN_SECONDS:
        return {"slot": slot, "deferred": True, "seconds_left": round(left)}
    for attempt in range(max_regenerations + 1):
        if attempt:
            left = remaining_seconds(deadline)
            if left is not None and left < REGENERATE_MIN_SECONDS:
                break  # the failed view stands as it is; the owner sees the checker line and can ask for a revision
        image = render(current_prompt, refs, out_dir / f"view-{slot}-try-{attempt + 1}.png", openclaw, runner, image_model, deadline)
        if not check:
            verdict = {"answers": {}, "failed": [], "unsure": [], "notes": {}, "unchecked": True,
                       "error": "vision check off in the profile"}
            attempts.append({"image": str(image), "check": verdict, "prompt": current_prompt})
            break
        if image_provider.available(PROVIDER_MODE):
            verdict = check_image(image, plan, openclaw, runner, vision_model, artwork, deadline)
        else:
            with _CHECK_LOCK:  # the CLI's describe call was flaky when overlapped
                verdict = check_image(image, plan, openclaw, runner, vision_model, artwork, deadline)
        attempts.append({"image": str(image), "check": verdict, "prompt": current_prompt})
        if not verdict["failed"]:
            break
        problems = "; ".join(f"{cid}: {verdict['notes'].get(cid, 'failed')}" for cid in verdict["failed"])
        current_prompt = prompt + f" Correct these problems from the previous attempt: {problems}."
    return {
        "slot": slot, "image": str(image), "passed": not verdict["failed"], "failed": verdict["failed"],
        "unsure": verdict["unsure"], "notes": verdict["notes"], "attempts": len(attempts), "history": attempts,
        **({"unchecked": True, "check_error": verdict.get("error")} if verdict.get("unchecked") else {}),
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
