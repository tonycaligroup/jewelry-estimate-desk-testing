#!/usr/bin/env python3
"""The installed scripts, checked file by file against the version that claims them.

Live (8 September 2026): a pod ran the 4.14.2 pipeline.py and
estimate_record.py under a SKILL.md that said 4.14.5, so three releases of
appointment fixes never ran and every retest "recurred". A version number
names a folder; it does not prove the files. `scripts/manifest.json` holds
the md5 of every script for the version in SKILL.md; the test suite writes
it, a test checks it, readiness verifies the installed folder against it,
and the publish checklist verifies the publishing folder the same way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"


def digests(base_dir: Path) -> dict[str, str]:
    """md5 of every scripts/*.py, by file name."""
    folder = Path(base_dir) / "scripts"
    return {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.py"))}


def read(base_dir: Path) -> dict[str, Any] | None:
    path = Path(base_dir) / "scripts" / MANIFEST
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("files"), dict) else None


def write(base_dir: Path) -> dict[str, Any]:
    import skill_version

    value = {"version": skill_version.installed(Path(base_dir)), "files": digests(base_dir)}
    path = Path(base_dir) / "scripts" / MANIFEST
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return value


def verify(base_dir: Path) -> dict[str, Any]:
    """What differs between the folder and its manifest: stale, missing, or extra scripts, or a version mismatch."""
    import skill_version

    manifest = read(base_dir)
    if manifest is None:
        return {"ok": False, "version": None, "stale": [], "missing": [], "extra": [], "error": "no manifest"}
    actual = digests(base_dir)
    expected = manifest["files"]
    stale = sorted(name for name, digest in expected.items() if name in actual and actual[name] != digest)
    missing = sorted(name for name in expected if name not in actual)
    extra = sorted(name for name in actual if name not in expected)
    version = skill_version.installed(Path(base_dir))
    version_ok = str(manifest.get("version")) == str(version)
    return {"ok": not (stale or missing or extra) and version_ok, "version": manifest.get("version"),
            "installed_version": version, "stale": stale, "missing": missing, "extra": extra, "count": len(expected)}


def describe(result: dict[str, Any]) -> str:
    if result.get("error"):
        return result["error"]
    if result["ok"]:
        return f"{result['count']} scripts match manifest {result['version']}"
    parts = []
    if result["stale"]:
        parts.append("STALE (not the " + str(result["version"]) + " files): " + ", ".join(result["stale"]))
    if result["missing"]:
        parts.append("missing: " + ", ".join(result["missing"]))
    if result["extra"]:
        parts.append("extra: " + ", ".join(result["extra"]))
    if str(result.get("installed_version")) != str(result.get("version")):
        parts.append(f"SKILL.md says {result.get('installed_version')}, manifest says {result.get('version')}")
    return "; ".join(parts) + "; re-install the whole folder"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--write", action="store_true", help="write scripts/manifest.json for the version in SKILL.md")
    args = parser.parse_args(argv)
    if args.write:
        value = write(args.base_dir)
        print(f"manifest written: {len(value['files'])} scripts, version {value['version']}")
        return 0
    result = verify(args.base_dir)
    print(("OK " if result["ok"] else "FAIL ") + describe(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
