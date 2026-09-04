#!/usr/bin/env python3
"""The retail specification gate, decided by code rather than by the model.

WORKFLOW.md 6.2 lists what must be known before a price: the piece and how
many; metal, karat, and color; the stone's type, origin, carat, color,
clarity, and cut when the piece has a stone; the size or dimensions; and the
setting or style. The model's only job is to extract what the customer
said. Which of those fields are still missing is a rule, so it lives here,
and the shop-profile policies (ask-always origin, setting style) are applied
by the record helper on top.
"""

from __future__ import annotations

from typing import Any

import estimate_record

PLACEHOLDERS = {
    "", "n/a", "not applicable", "not specified", "tbd", "to be determined",
    "unknown", "unspecified", "none", "null",
}
NO_KARAT_METALS = {"platinum", "silver", "palladium", "titanium", "tungsten", "steel"}
RING_PIECES = {"ring", "band", "engagement ring", "wedding band", "signet ring", "eternity band"}
DIMENSION_PIECES = {"chain", "necklace", "bracelet", "pendant", "anklet", "cuff", "bangle", "earring", "earrings"}
STONE_KEYS = ("stone_type", "stone_origin", "stone_carat", "stone_color", "stone_clarity", "stone_cut")


def present(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        return value.strip().lower() not in PLACEHOLDERS
    if isinstance(value, (list, dict)):
        return bool(value)
    return False


def _text(spec: dict[str, Any], key: str) -> str:
    value = spec.get(key)
    return value.strip().lower() if isinstance(value, str) else ""


def has_stones(spec: dict[str, Any]) -> bool:
    if present(spec.get("stone_type")):
        return True
    count = spec.get("stone_count")
    if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0:
        return True
    return present(spec.get("accent_stones")) or estimate_record.stones_in_words(spec)


def missing_required_fields(spec: dict[str, Any], shop_profile: dict[str, Any] | None) -> list[str]:
    """Required keys the specification does not satisfy, plus profile policies."""
    if not isinstance(spec, dict):
        raise ValueError("specification must be an object")
    missing: set[str] = set()
    if not present(spec.get("piece_type")):
        missing.add("piece_type")
    metal = _text(spec, "metal")
    if not metal:
        missing.add("metal")
    else:
        karat_in_metal = any(token.rstrip("k").isdigit() for token in metal.replace("-", " ").split())
        needs_karat = not any(word in metal for word in NO_KARAT_METALS)
        if needs_karat and not present(spec.get("metal_karat")) and not karat_in_metal:
            missing.add("metal_karat")
        color_in_metal = any(word in metal for word in ("white", "yellow", "rose"))
        if needs_karat and not present(spec.get("metal_color")) and not color_in_metal:
            missing.add("metal_color")
    piece = _text(spec, "piece_type")
    if piece:
        if any(piece == p or piece.endswith(" " + p) for p in RING_PIECES) or "ring" in piece:
            if not present(spec.get("finger_size")):
                missing.add("finger_size")
        elif any(word in piece for word in DIMENSION_PIECES) and not present(spec.get("dimensions")):
            missing.add("dimensions")
    if has_stones(spec):
        for key in STONE_KEYS:
            if key == "stone_cut" and present(spec.get("stone_shape")):
                continue
            if not present(spec.get(key)):
                missing.add(key)
    # Profile policies: setting style when there are stones, ask-always origin.
    return estimate_record.enforce_specification_policies(spec, sorted(missing), shop_profile)
