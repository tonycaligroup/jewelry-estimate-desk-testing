---
name: jewelry-estimate-desk-testing
description: Prepare and route custom-jewelry estimates from inbound customer inquiries through specification intake, owner price approval, customer reply, scheduling, rendering, and follow-up. Use for retail or wholesale custom-jewelry estimate workflows; do not use for appraisals, insurance valuations, payments, disputes, or unapproved outbound prices.
metadata:
  openclaw:
    requires:
      bins: [kolo, openclaw, python3]
---

# Jewelry Estimate Desk

Turn an inbound custom-jewelry inquiry into an owner-approved estimate and a
specific next step. Preserve the original customer channel and keep every price
and delivery commitment behind owner approval.

## Non-negotiable rules

1. Never send a price, discount, rush commitment, or delivery promise without
   the owner's approval of that exact estimate.
2. Never send a retail estimate until the required specification is complete.
3. Show the customer one all-in price. Never expose costs, markup, margin, or
   vendor identity.
4. Never take payments, cards, deposits, refunds, or payment-link actions.
5. Never interpolate inquiry-derived data into shell commands. Use the bundled
   Python helpers, which invoke the Kolo CLI with argument arrays and no shell.
6. Never send a customer message to `deliveryContext.to` or `kolo:<uuid>`.
   Those destinations are for the owner. Reply through the original customer
   connector, mailbox, recipient, and thread.

Run this skill through the dedicated Kolo agent pinned to
`litellm-fireworks/qwen-3-7-plus`, with no fallback. Monitoring crons must use
the same model and `--fallbacks ""`. If Kolo cannot verify the model, stop and
route to the pinned agent.

## Bundled resources

- `scripts/validate_profile.py`: validate runtime shop configuration.
- `scripts/approval_guard.py`: create opaque estimate IDs, bind approvals to
  route and specification, and reject changed execution state.
- `scripts/kolo_safe.py`: request approvals, notify the owner, upsert records,
  and write idempotent audit events without a shell.
- `scripts/inbox_claim.py`: prevent overlapping processing in a shared Kolo
  workspace using atomic directory creation.
- `templates/shop-profile.json`: runtime profile template.
- `templates/customer-emails.md`: customer message templates.
- `templates/spec-gate-email.md`: batched retail intake request.
- `templates/approved-estimate-note.md`: required estimate disclaimer.
- `references/rendering-standards.md`: read only when producing a rendering.
- `references/nudge-workflow.md`: read only when scheduling follow-ups.
- `references/OWNER-GUIDE.md`: owner-facing trust and safety explanation.

Use `{baseDir}` as the installed skill directory in commands below.

## Phase 0: validate the shop profile

The runtime profile is `estimate-desk/shop-profile.json` in the workspace, not
in this skill and not in `SKILL.md` frontmatter. Do not store or trust a manual
`ready` field.

On first setup, copy `{baseDir}/templates/shop-profile.json` to the runtime
location and collect:

1. Shop name, owner name, approver email, outbound mailbox, and signature.
2. Mode: `retailer`, `wholesale_middle_man`, or `both`.
3. Markup multiplier. Convert `25%` to `1.25` and confirm with the example
   `$1,000 cost → $1,250 quote` before saving.
4. Trust stage. Default to Stage 1.
5. Booking mode and IANA timezone.
6. Optional inbox-monitoring hours and timezone.

Before reading or processing an inquiry, run:

```bash
python3 {baseDir}/scripts/validate_profile.py estimate-desk/shop-profile.json
```

Proceed only when it exits 0 and returns `{"ready": true}`. If it fails, show
the owner the returned field errors and stop. Do not modify the installed skill
to store shop settings.

## Trust stages

| Stage | Autonomous work | Owner approval still required |
|---|---|---|
| 1 — Watch me | Read, calculate, and draft only | Every outbound message, price, and booking |
| 2 — Ask questions | Price-free specification requests | Every price and booking |
| 3 — Book me | Stage 2 plus booking inside declared windows | Every price |

Never advance the stage automatically. Missing or unreadable stage means
Stage 1.

## Inbox monitoring

Monitoring is optional and requires `inbox_monitoring.enabled: true`. Create
only one polling cron for this skill. Search Gmail through the path returned by
`kolo integration-routing`; when it returns `maton`, read
`/opt/kolo-skills/api-gateway/SKILL.md` before using Gmail.

For each matching Gmail message:

1. Extract the immutable Gmail `id`; do not use `threadId` as the message ID.
2. Claim it before any notification, draft, send, or record mutation:

   ```bash
   python3 {baseDir}/scripts/inbox_claim.py claim --message-id '<gmail-id>'
   ```

   Gmail IDs are provider-generated, not customer text. Save the returned
   `claim_token` in private job state. Exit 4 means another run already owns or
   completed the message; skip it without side effects.
3. Immediately write a `processing` record keyed by the Gmail message ID using
   `scripts/kolo_safe.py record-upsert` and a JSON payload file.
4. Process messages sequentially in the same monitor run. Do not create a
   second monitoring cron or manually start a concurrent monitor.
5. After successful triage and any authorized action, mark both the Kolo record
   and local claim `processed`. If failure occurs after an external send might
   have happened, mark `manual_review` and never retry automatically. Retry once
   only when failure is proven to have occurred before any external side effect.

Kolo records do not provide compare-and-swap. The local claim protects a shared
workspace, but cannot guarantee exclusion across independent hosts. Treat a
stale `processing` claim as manual review, not permission to repeat a send.

The cron's message must contain only fixed instructions and opaque/provider IDs.
Owner notifications use the opaque estimate ID, never a customer name or piece
type. Stay silent when no new messages exist.

## Phase 1: triage

Read the full inbound message and attachments. Route requests as follows:

| Request | Route |
|---|---|
| New custom piece, replica, or redesign | Continue |
| Repair, resize, or restring | Repair intake; no rendering |
| Appraisal or insurance valuation | Stop; this workflow does not value property |
| Existing inventory price | Sales workflow |
| Job status | Look up status; do not estimate |
| Meeting intent | Scheduling flow, then continue intake |
| Angry, legal, chargeback, insurance, media, fraud, or lost/damaged claim | Stop and escalate |

Extract piece and quantity; metal, karat, and color; stone type, lab/natural,
shape, carat, color, clarity, cut, and count; size or dimensions; setting,
finish, engraving; event date and budget; customer-supplied materials;
certificate; reference images; and scheduling intent.

A photo can inform a question but never satisfies a required field. Use only
the original channel and keep surprise-related subjects detail-free.

## Phase 1.5: retail specification gate

Before sending a retail estimate, require all applicable fields:

- Stone type, lab/natural origin, carat, color, clarity, and cut/shape.
- Metal, karat, and color.
- Finger size for rings; length/dimensions for chains, bracelets, or pendants.
- Piece type and quantity, setting/style, event date, budget, and whether the
  customer supplies stone or metal.

For a piece without stones, stone fields are not applicable; do not report a
misleading `x/8` score. Wholesale estimates may use explicitly labeled
assumptions.

When fields are missing, use `templates/spec-gate-email.md`: one friendly,
price-free, batched request; do not re-ask known facts; offer two real open
slots with timezone. At Stage 1, draft it. At Stage 2 or 3, it may be sent.
After one partial reply, ask once more only for load-bearing gaps; then escalate
the decision to the owner.

## Phase 2: price internally

The gate blocks customer sending, not internal calculation. Use the dated shop
rate card and comparable jobs before any market default.

| Cost line | Basis |
|---|---|
| Metal | finished grams × profile $/g |
| Center stone | carat × profile $/ct |
| Accent stones | total carat × profile $/ct |
| CAD, casting, setting, finishing, engraving | profile fees |
| Bench labor | hours × profile $/hr; always separate internally |
| COGS | sum of costs |
| Proposed quote | COGS × `pricing.markup_multiplier` |

Estimate the high side deliberately. If finished weight is unknown, give the
owner a bracket instead of false precision. On a first job without a rate card,
prepare quantities with dollars blank and ask for the missing rates. Never turn
an estimate into an appraisal or replacement value.

## Phase 3: bind and request owner approval

Create a private temporary directory with Python `tempfile.mkdtemp()` or
`mktemp -d`; permissions must be `0700`, contained files `0600`, and the name
must not contain customer data. Generate an opaque ID:

```bash
python3 {baseDir}/scripts/approval_guard.py new-id
```

Write `current-state.json` with:

- `estimate_id`
- `route`: original channel, mailbox, recipient, Gmail message ID, and thread ID
- `specification`: the exact priced written specification
- `proposed_price`
- internal pricing, assumptions, feasibility, appointment options, and draft

Create the immutable approval request:

```bash
python3 {baseDir}/scripts/approval_guard.py create \
  "$WORK/current-state.json" "$WORK/approval-request.json"
python3 {baseDir}/scripts/kolo_safe.py request-approval \
  --estimate-id '<jed-id>' \
  --details "$WORK/approval-request.json" \
  --session-key '<session-key>'
python3 {baseDir}/scripts/kolo_safe.py notify-owner --estimate-id '<jed-id>'
```

Get the session key from `sessions_list`. The owner notification contains only
the opaque estimate ID. Never insert customer text into CLI arguments.

Wait for a Kolo approval event. Copy only values actually returned by that
event into `approved.json`: the submitted `estimate_id` and binding hash,
`approval_status`, and `owner_approved_price`. Never infer approval from chat
sentiment or silence.
If the request is rejected, stop without sending. If the owner edits the
request, treat the returned values as a new candidate approval and verify its
binding and price against the current state; any missing or changed binding
requires a new approval request.

Immediately before customer send, re-read the current route and specification,
then run:

```bash
python3 {baseDir}/scripts/approval_guard.py verify \
  "$WORK/approved.json" "$WORK/current-state.json"
```

Exit 3 means recipient, route, specification, or approval state changed. Stop
and request a new approval. Kolo stores the structured approval payload but
does not enforce this binding; this verification is mandatory.

## Phase 4: send through the customer route

After successful verification:

1. Fill `templates/customer-emails.md` using the exact owner-approved price.
2. Include the canonical high-end/pending-CAD substance from
   `templates/approved-estimate-note.md`, estimated—not guaranteed—lead time,
   validity date, and two or three live appointment options with timezone.
3. Call `kolo integration-routing`. For Gmail through Maton, read the
   api-gateway skill and send a base64url-encoded RFC 5322 reply through
   `gateway.maton.ai/google-mail/gmail/v1/users/me/messages/send`. Preserve the
   stored outbound mailbox, recipient, `threadId`, `In-Reply-To`, and
   `References` headers.
4. Never use the Kolo `message` tool, `deliveryContext.to`, or `kolo:<uuid>` for
   the customer. Use those only for an owner-facing copy or notification.
5. Store the provider's outbound message ID. If the response is uncertain or
   lacks a message ID, do not retry automatically; inspect the Gmail thread or
   escalate to the owner first.

For scheduling, query live free/busy, intersect with declared windows, offer
specific times, then re-check immediately before creating an event. Confirm to
the customer only after the calendar write succeeds. Use the owner's IANA
timezone, never the pod's UTC clock.

If a rendering is authorized, read `references/rendering-standards.md` first.

## Phase 5: records, follow-up, and cleanup

Record the estimate under the opaque estimate ID. The record should retain the
inbound message ID and timestamp; approval binding hash and approved price;
specification and assumptions; internal cost sheet; outbound provider message
ID; trust stage; appointment data; and next action date. Use
`scripts/kolo_safe.py record-upsert` with JSON files rather than generated shell
commands.

After the record is durable, write the corresponding audit event through
`scripts/kolo_safe.py log-action`. Use an idempotency key composed only of the
opaque estimate ID and fixed event type, and pass event details through a JSON
file. The record remains the workflow source of truth; the audit event does not
replace the inbox claim or record.

Statuses are `awaiting_specs`, `pending_approval`, `estimate_sent`,
`appointment_booked`, `approved`, `declined`, `manual_review`, and `dormant`.

For day-3 and day-7 follow-ups, read `references/nudge-workflow.md`. After day 7,
stop and mark dormant.

Delete the private temporary directory after records are durably written. Keep
workspace inbox-claim sentinels as deduplication state; do not delete or
auto-steal them.

## Escalate without drafting a customer response

Escalate anger, complaints, price pushback after a sent quote, discount
requests, legal threats, insurance or chargeback matters, lost/damaged claims,
estate or heirloom disputes, press, fraud or stolen-goods concerns, unclear
requests, and any uncertain prior send.

A first-contact price question is not pushback: collect specifications and
budget without quoting. A missing profile or incomplete specification is a
setup/intake issue, not a customer escalation.
