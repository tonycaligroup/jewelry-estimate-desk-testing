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

import re
from typing import Any

import estimate_record

PLACEHOLDERS = {
    "", "n/a", "not applicable", "not specified", "tbd", "to be determined",
    "unknown", "unspecified", "none", "null",
}
NO_KARAT_METALS = {"platinum", "silver", "palladium", "titanium", "tungsten", "steel"}
RING_PIECES = {"ring", "band", "engagement ring", "wedding band", "signet ring", "eternity band"}
DIMENSION_PIECES = {"chain", "necklace", "bracelet", "pendant", "anklet", "cuff", "bangle", "earring", "earrings"}
# A whole word: "earrings" and "keyring" contain "ring" and are not rings.
_RING_WORD_RE = re.compile(r"\b(?:ring|rings|band|bands)\b")
STONE_KEYS = ("stone_type", "stone_origin", "stone_carat", "stone_color", "stone_clarity", "stone_cut")
# Pieces that carry a center stone by definition: a carat or a stone is implied even when no stone is named.
STONE_PIECES = ("engagement ring", "solitaire", "halo", "three stone", "three-stone", "tennis", "eternity", "cocktail ring")


def is_ring_piece(piece: str) -> bool:
    """A finger-sized piece: a ring or a band named as a whole word ("earrings" is not)."""
    piece = (piece or "").strip().lower()
    if any(piece == p or piece.endswith(" " + p) for p in RING_PIECES):
        return True
    return bool(_RING_WORD_RE.search(piece))


# Earrings whose length a customer can name ("about an inch"); studs and anything with a stated stone never need one.
LENGTH_EARRING_WORDS = ("hoop", "drop", "dangle", "dangling", "chandelier", "threader", "huggie", "linear")
# Pieces whose size the jeweler works out from the design: never asked (the owner's rule, 8 September 2026).
JEWELER_SIZED_PIECES = ("pendant", "charm", "locket", "stud")


def needs_dimensions(spec: dict[str, Any], piece: str) -> bool:
    """Whether the customer is asked for a size: only one they can answer (a length, a wrist), never a millimetre.

    Live (8 September 2026): stud earrings with a stated 1.5 ct center stone
    were asked for "the exact dimensions for the halo and overall size",
    then for millimetre diameters and a drop length. A customer does not
    know those; the jeweler works them out from the stone and the design.
    """
    piece = (piece or "").strip().lower()
    if any(word in piece for word in JEWELER_SIZED_PIECES):
        return False
    if "earring" in piece or any(word in piece for word in LENGTH_EARRING_WORDS):
        if present(spec.get("stone_carat")):
            return False  # the stone sets the size
        return any(word in piece for word in LENGTH_EARRING_WORDS)
    return any(word in piece for word in DIMENSION_PIECES)


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
    if present(spec.get("stone_carat")) or present(spec.get("center_stone")) and _text(spec, "center_stone") not in ("no", "none", "false"):
        # "a 1 ct engagement ring": the carat says there is a stone.
        return True
    piece = _text(spec, "piece_type")
    if piece and any(word in piece for word in STONE_PIECES) and not estimate_record.customer_supplies_stone(spec):
        return True
    return present(spec.get("accent_stones")) or estimate_record.stones_in_words(spec)


def missing_required_fields(spec: dict[str, Any], shop_profile: dict[str, Any] | None) -> list[str]:
    """Required keys the specification does not satisfy, plus profile policies.

    A multi-piece specification (MULTI-PIECE-PLAN.md) is gated piece by
    piece; its missing names are `pieces.<i>.<field>`. One piece keeps bare
    names and the code path it always had.
    """
    if not isinstance(spec, dict):
        raise ValueError("specification must be an object")
    pieces = estimate_record.pieces_of(spec)
    if len(pieces) > 1:
        names: list[str] = []
        for index, piece in enumerate(pieces):
            names.extend(f"{estimate_record.PIECE_PREFIX}{index}.{field}" for field in _missing_for_piece(piece, shop_profile))
        return names
    return _missing_for_piece(spec, shop_profile)


def _missing_for_piece(spec: dict[str, Any], shop_profile: dict[str, Any] | None) -> list[str]:
    missing: set[str] = set()
    if not present(spec.get("piece_type")):
        missing.add("piece_type")
    metal = _text(spec, "metal")
    if not metal:
        # Ask the whole metal question at once: which metal, which karat,
        # which color. One email, not three.
        missing.update({"metal", "metal_karat", "metal_color"})
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
        if is_ring_piece(piece):
            if not present(spec.get("finger_size")):
                missing.add("finger_size")
        elif needs_dimensions(spec, piece) and not present(spec.get("dimensions")):
            missing.add("dimensions")
    if has_stones(spec) and estimate_record.customer_supplies_stone(spec):
        # The customer's own stone: the bench needs its shape and size to
        # build the setting; grade and origin are theirs, not the shop's.
        if not (present(spec.get("stone_shape")) or present(spec.get("stone_cut"))):
            missing.add("stone_shape")
        if not present(spec.get("stone_carat")):
            missing.add("stone_carat")
    elif has_stones(spec):
        import cost_components  # local import: cost_components does not import this module

        center = cost_components.has_center_stone(spec)
        for key in STONE_KEYS:
            if key in ("stone_color", "stone_clarity"):
                continue  # grades are the jeweler's choice unless the customer states one (the owner's decision, 9 Sep 2026)
            if key == "stone_cut" and present(spec.get("stone_shape")):
                continue
            if key in ("stone_carat", "stone_cut") and not center:
                # Pave, melee, and accent stones: the carat weight follows from
                # the design and the stones are round; nothing to ask.
                continue
            if key == "stone_carat" and present(spec.get("stone_dimensions")):
                continue  # sized in millimetres by the customer: the jeweler derives the weight (9 September 2026)
            if not present(spec.get(key)):
                missing.add(key)
    if has_stones(spec) and not present(spec.get("setting_style")):
        missing.add("setting_style")
    if "earring" in piece and not present(spec.get("earring_style")) and not estimate_record.earring_style_in_words(piece) \
            and not estimate_record.earring_style_in_words(str(spec.get("reference_images") or "")):
        # Studs, hoops, or drops: a preference the customer can give (the owner, 9 September 2026); a photo that
        # shows which is read and stated, never asked again.
        missing.add("earring_style")
    # Profile policies: setting style when there are stones, ask-always origin.
    return estimate_record.enforce_specification_policies(spec, sorted(missing), shop_profile)
