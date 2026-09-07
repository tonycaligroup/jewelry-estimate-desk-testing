#!/usr/bin/env python3
"""One command for everything local: the suite, then the fault-injection harness.

    python3 tests/run_all.py                    # all of it
    python3 tests/run_all.py --results out.json # also write machine-readable results
    python3 tests/run_all.py -k second_piece    # only the tests whose names match

The results file records the commit, the Python version, counts, and
timings, so a result can always be tied to the source it ran on. Exit
status is non-zero when anything failed.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _commit() -> str:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", help="write a JSON results file here")
    parser.add_argument("-k", dest="pattern", help="run only tests whose names contain this text (skips the harness)")
    args = parser.parse_args(argv)

    loader = unittest.TestLoader()
    if args.pattern:
        loader.testNamePatterns = [f"*{args.pattern}*"]
    suite = loader.discover(str(ROOT / "tests"))
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    suite_seconds = round(time.monotonic() - started, 2)

    harness: dict = {"ran": False}
    if not args.pattern:
        started = time.monotonic()
        run = subprocess.run([sys.executable, str(ROOT / "tests" / "test_fault_injection.py")], capture_output=True, text=True)
        harness = {"ran": True, "ok": run.returncode == 0, "seconds": round(time.monotonic() - started, 2),
                   "tail": (run.stdout + run.stderr).strip().splitlines()[-3:]}
        print("\n".join(harness["tail"]))

    ok = result.wasSuccessful() and harness.get("ok", True)
    summary = {
        "commit": _commit(), "python": platform.python_version(), "pattern": args.pattern,
        "suite": {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
                  "skipped": len(result.skipped), "seconds": suite_seconds},
        "harness": harness, "ok": ok,
    }
    if args.results:
        Path(args.results).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"{'OK' if ok else 'FAILED'}: {result.testsRun} tests in {suite_seconds}s"
          + (f"; harness {'ok' if harness['ok'] else 'FAILED'} in {harness['seconds']}s" if harness.get("ran") else "")
          + f"; commit {summary['commit'][:9]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
