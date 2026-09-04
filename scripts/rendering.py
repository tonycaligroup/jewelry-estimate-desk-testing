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
from pathlib import Path
from typing import Any, Callable

import judge

Runner = Callable[..., subprocess.CompletedProcess[str]]
ARCHETYPE_DIR = Path(__file__).resolve().parent.parent / "templates" / "render"
DEFAULT_VISION_MODEL = None  # the pod default (text+image capable); override with --vision-model
IMAGE_TIMEOUT_MS = 180_000
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


def image_argv(prompt: str, refs: list[Path], output: Path, openclaw: str, model: str | None = None) -> list[str]:
    argv = [openclaw, "infer", "image"]
    if refs:
        argv.append("edit")
        for ref in refs:
            argv += ["--file", str(ref)]
    else:
        argv.append("generate")
    argv += ["--prompt", prompt, "--size", "1024x1024", "--output", str(output), "--timeout-ms", str(IMAGE_TIMEOUT_MS), "--json"]
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
           model: str | None = None) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = runner(image_argv(prompt, refs, output, openclaw, model), check=True, capture_output=True, text=True, shell=False)
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
                vision_model: str | None = DEFAULT_VISION_MODEL, reference: Path | None = None) -> dict[str, Any]:
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
    completed = runner(describe_argv(image, prompt, openclaw, vision_model), check=True, capture_output=True, text=True, shell=False)
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
) -> dict[str, Any]:
    """Plan, render two views, check each, regenerate a failing one once. Returns the report."""
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
    prompts = build_prompts(plan, specification, artwork is not None, bool(arch.get("exemplar")))
    report: dict[str, Any] = {"plan": plan, "prompts": prompts, "references": [str(r) for r in refs], "views": []}
    for slot, prompt in enumerate(prompts, start=1):
        attempts = []
        current_prompt = prompt
        image = None
        check = None
        for attempt in range(max_regenerations + 1):
            image = render(current_prompt, refs, out_dir / f"view-{slot}-try-{attempt + 1}.png", openclaw, runner, image_model)
            check = check_image(image, plan, openclaw, runner, vision_model, artwork)
            attempts.append({"image": str(image), "check": check, "prompt": current_prompt})
            if not check["failed"]:
                break
            problems = "; ".join(f"{cid}: {check['notes'].get(cid, 'failed')}" for cid in check["failed"])
            current_prompt = prompt + f" Correct these problems from the previous attempt: {problems}."
        report["views"].append({
            "slot": slot, "image": str(image), "passed": not check["failed"], "failed": check["failed"],
            "unsure": check["unsure"], "notes": check["notes"], "attempts": len(attempts), "history": attempts,
        })
    report["all_passed"] = all(v["passed"] for v in report["views"])
    return report
