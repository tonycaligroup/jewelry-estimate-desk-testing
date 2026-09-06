#!/usr/bin/env python3
"""The installed version, read from SKILL.md's frontmatter, printed on every line that matters."""

from __future__ import annotations

import re
from pathlib import Path


def installed(base_dir: Path | None = None) -> str:
    root = base_dir or Path(__file__).resolve().parents[1]
    try:
        head = (root / "SKILL.md").read_text(encoding="utf-8")[:2000]
    except OSError:
        return "unknown"
    match = re.search(r"^version:\s*[\"']?(\d+\.\d+\.\d+)", head, re.M)
    return match.group(1) if match else "unknown"
