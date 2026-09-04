#!/usr/bin/env python3
"""Try the rendering pipeline on its own, without the desk.

    python3 scripts/render_lab.py --spec "signet ring, 14K yellow gold, oval face, engraved KOLO wordmark" \
        --artwork /tmp/kolo-logo.png --out /tmp/render-lab/signet --post

Prints the plan, the assembled prompts, the checker's answers per view, and
with --post sends the images to the owner's chat with the checker's verdict.
Touches nothing under estimate-desk.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import rendering


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, help="the piece in words, or a JSON specification object")
    parser.add_argument("--artwork", type=Path, default=None, help="customer artwork PNG/JPEG to carry into the render")
    parser.add_argument("--archetype", default=None, help="skip the planning call and use this archetype id")
    parser.add_argument("--context", default="", help="optional thread text for the planner")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--openclaw", default="openclaw")
    parser.add_argument("--model", default=None, help="planner model")
    parser.add_argument("--vision-model", default=None, help="checker model (text+image capable)")
    parser.add_argument("--image-model", default=None)
    parser.add_argument("--no-regenerate", action="store_true")
    parser.add_argument("--post", action="store_true", help="post the views to the owner's chat with the verdicts")
    parser.add_argument("--list", action="store_true", help="list archetypes and exit")
    args = parser.parse_args(argv)
    if args.list:
        for key, value in rendering.archetypes().items():
            print(f"{key}: {value['label']}" + (" (exemplar)" if value.get("exemplar") else ""))
        return 0
    spec: dict | str = args.spec
    try:
        parsed = json.loads(args.spec)
        if isinstance(parsed, dict):
            spec = parsed
    except ValueError:
        pass
    report = rendering.run(
        spec, args.out, args.openclaw, artwork=args.artwork, archetype=args.archetype, context=args.context,
        model=args.model, vision_model=args.vision_model, image_model=args.image_model,
        max_regenerations=0 if args.no_regenerate else 1,
    )
    (args.out / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("PLAN", json.dumps(report["plan"]))
    for i, prompt in enumerate(report["prompts"], start=1):
        print(f"PROMPT {i}", prompt[:400])
    for view in report["views"]:
        verdict = "PASS" if view["passed"] else "FAIL " + ",".join(view["failed"])
        print(f"VIEW {view['slot']} {verdict} attempts={view['attempts']} unsure={','.join(view['unsure']) or '-'} {view['image']}")
        for cid, note in view["notes"].items():
            print(f"   {cid}: {note}")
    print("ALL PASSED" if report["all_passed"] else "NOT ALL PASSED")
    if args.post:
        for view in report["views"]:
            verdict = "passed" if view["passed"] else "failed " + ", ".join(view["failed"])
            text = f"Render lab: {report['plan']['archetype']} view {view['slot']}, checker {verdict} after {view['attempts']} attempt(s)"
            subprocess.run(["kolo", "notify-owner", "-m", text, "--file", view["image"]], check=False, capture_output=True, text=True)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
