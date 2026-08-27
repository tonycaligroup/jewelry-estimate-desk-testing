# Rendering Standards

Read this reference after an estimate is sent and the customer requests a
visual rendering, or when the owner explicitly requests one earlier. A
post-estimate customer request does not require another owner approval.

## Purpose

Create a customer-facing design illustration, not a manufacturing drawing,
gemological report, or promise of the finished piece. Generate two parallel
4:3 views of the same approved design for each distinct customer rendering
request. They are complementary views, never alternate design proposals.

## Required prompt fields

- Opaque estimate ID
- Piece type and quantity
- Metal, karat, and color
- Center stone type, origin, shape, carat, color, and clarity
- Accent stones and setting style
- Shank, profile, finish, and requested view
- Overall silhouette; rail, shank, and setting topology; which surfaces carry
  stones; and whether stone coverage is partial or continuous
- Up to three reference images, clearly ranked
- `DO NOT CHANGE` constraints
- The one requested variation for an iterative render

Do not put the customer's name, email, price, jeweler cost assumptions, vendor,
cost, markup, margin, certificate
number, or event details into an image prompt.

## Tool call

Use the image tool available in the active Kolo environment. Request two images
at 1536×1024 or the closest supported 4:3 size. Do not hard-code a model ID
unless the current environment confirms it.

Unless the customer requested another view, use a three-quarter wearable view
that exposes the design's defining exterior and interior details. A second view
may show the opposite side or a closer detail, but it must remain the same
piece. Do not substitute a flat plan view when it hides or changes defining
features.

Before delivery, compare each completed candidate with the immutable approved
specification. Reject a candidate that visibly changes the piece type, metal
color, silhouette, rail or shank layout, setting topology, stone location or
coverage, center-stone type or shape, stone count, requested view, or another
explicit `DO NOT CHANGE` constraint. Deliver one image when only one candidate
conforms, deliver both when both conform, and deliver neither when neither
conforms. Rendering review never changes the approved written specification.

## Customer note

Label every rendering as an illustration of the design direction. State that
the written specification and approved design control the final piece.
