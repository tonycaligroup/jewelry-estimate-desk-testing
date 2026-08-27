# Rendering Standards

Read this reference after an estimate is sent and the customer requests a
visual rendering, or when the owner explicitly requests one earlier. A
post-estimate customer request does not require another owner approval.

## Purpose

Create a customer-facing design illustration, not a manufacturing drawing,
gemological report, or promise of the finished piece. Generate two 4:3 options.

## Required prompt fields

- Opaque estimate ID
- Piece type and quantity
- Metal, karat, and color
- Center stone type, origin, shape, carat, color, and clarity
- Accent stones and setting style
- Shank, profile, finish, and requested view
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

## Customer note

Label every rendering as an illustration of the design direction. State that
the written specification and approved design control the final piece.
