# Jewelry Estimate Desk — the workflow (source of truth)

This document describes **what the desk does for a jeweler**, independent of
how it is built. It is written from the customer's messages inward: what
arrives, what we do with it, what we must collect, what the owner sees, and
what we never do. Cron schedules, scripts, models, and platform mechanics are
deliberately absent. If an implementation and this document disagree, this
document wins and the implementation must be corrected.

Companion documents: `references/OWNER-GUIDE.md` (the owner's plain-language
promise), `templates/customer-emails.md` and `templates/spec-gate-email.md`
(exact customer wording), `SKILL.md` (the current implementation rules).

---

## 1. Purpose and scope

Turn an inbound custom-jewelry inquiry from a retail customer into an
owner-approved estimate and a specific next step, while keeping every price,
promise, and booking behind the owner. The desk serves retail customers only;
there is no wholesale or trade mode.

**In scope:** new custom pieces, replicas, redesigns and remounts, repair
intake (without rendering), scheduling a consultation, post-estimate
renderings, day-3 and day-7 follow-ups.

**Out of scope, always:** appraisals or insurance valuations, pricing existing
inventory, payments of any kind, negotiation or discounts, disputes, and any
statement about whether a stone or piece is real, ethically sourced, or worth
something.

---

## 2. Who is involved and where they talk

| Party | Channel | Role |
|---|---|---|
| Customer | Their original channel. Today that is email to the shop's mailbox, and every reply stays in the customer's own thread. | Asks for a piece, answers questions, accepts or declines, asks for a picture or a meeting. |
| The desk (assistant) | Works in the background. Writes to the customer only in the original thread, and only when the stage allows it. | Reads, extracts, prices, drafts, requests approval, sends approved messages, schedules. |
| Owner / approver | Kolo: approval briefs, alerts in the owner chat, and the review list. The person who activated the desk is the approver. | Approves, edits, or rejects every price. Decides escalations. Sets the trust stage. |
| Calendar | The shop's Google Calendar. | The only source of truth for whether a meeting exists or a slot is free. |
| Records | One private estimate record per inquiry, mirrored to Kolo for the owner. | Authoritative memory of the inquiry, its specification, price, evidence, and status. |

Customer identity is the normalized sender **email address**, never the
display name. Two senders with the same name and different addresses are two
customers with two records.

---

## 3. What we collect once, at setup (the shop profile)

Before the first inquiry is touched, the owner supplies:

1. Shop name, outbound mailbox, signature block, business address, website.
2. Pricing model: cost-plus multiplier or target margin, with a worked example
   the owner confirms.
3. Rate card: metal price per gram (or spot metal enabled with provider and
   refresh cadence), stone prices per carat, fee catalog (CAD, casting,
   setting, finishing, engraving, shipping), and the bench labor rate per hour.
4. Defaults the jeweler prefers when a customer delegates a choice (karat,
   color, finish, setting), and whether stone origin must always be asked.
5. Trust stage (defaults to Stage 1).
6. Scheduling: booking mode, timezone, declared availability windows,
   blackouts, meeting durations, minimum notice, and the near-term offer window
   (default 7 days so the first meeting is offered soon, never near delivery).
7. Terms: estimated lead time, deposit terms, tax handling, estimate validity
   (default 7 days), rendering policy.
8. Owner-alert preference (today only the Kolo chat is active).

Missing required settings block processing. Missing rates are never invented:
an estimate that needs a rate the owner has not set goes to the owner as a
review item asking for that rate.

---

## 4. Trust stages

| Stage | The desk may do on its own | Still needs the owner |
|---|---|---|
| 1 — Watch me | Read, extract, price internally, draft | Every outbound message, every booking, every rendering, every price |
| 2 — Ask questions | Stage 1 plus send price-free specification requests | Every booking, every rendering, every price |
| 3 — Offer times | Stage 2 plus offer real open times inside declared windows | Every booking, every rendering, every price |

Three things are approval-gated at every stage and never become autonomous:
the price, the booking of a meeting, and the sending of a rendering. The
stage only moves when the owner says so. "Pause" or "stop" from the owner
halts all outbound work immediately until that owner says to resume.

---

## 5. Non-negotiables in every customer interaction

1. No price, range, ballpark, discount, rush, or delivery promise reaches a
   customer without the owner approving that exact estimate.
2. No estimate before the required specification is complete.
3. The customer sees one all-in price. Costs, weights used for costing, rates,
   markup, margin, vendors, and assumptions are owner-only, always.
4. Every customer message is a reply in the customer's original thread. No new
   subjects, no new threads, no other addresses. Surprise-sensitive inquiries
   never get a revealing subject.
5. No payments, deposits, card details, or payment links.
6. Meeting state comes from the calendar, never from email wording, and no
   meeting is booked or confirmed without the owner approving that booking.
7. No rendering reaches a customer until the owner has looked at the images
   and approved them.
8. The desk never claims to be a person or the owner.

---

## 6. The lifecycle, message by message

### 6.1 A new inquiry arrives

**Triage the request.**

| The customer is asking for | We do |
|---|---|
| A new custom piece, replica, redesign, or remount | Continue to intake |
| A repair, resize, or restring | Repair intake, no rendering |
| An appraisal or insurance value | Stop; tell the owner; never value property |
| The price of an existing inventory item | Hand to the sales workflow |
| Job status | Look up status; do not estimate |
| A meeting | Scheduling flow, then continue intake |
| Anger, legal, chargeback, insurance, media, fraud, lost or damaged claim | Stop and escalate to the owner with no customer reply |

**Open a record.** One opaque estimate ID bound to the customer's email
identity and the original message and thread. This record is the memory for
everything that follows.

**Extract what the customer told us**, from the message and any attachments:

- Piece type and quantity; setting or style; finish; engraving.
- Metal, karat, and color.
- Stones: type, lab-grown or natural, shape, carat, color, clarity, cut, count,
  certificate, and whether the customer is supplying them.
- Size or dimensions: finger size for rings, length for chains and bracelets,
  dimensions for pendants.
- Event date and budget (useful, never required).
- Reference images (they inform a question; they never satisfy a required
  field or prove a stone).
- Scheduling intent.

Facts stated anywhere in the thread, in the first message or a later reply,
are known and are never asked again.

### 6.2 The specification gate

Before any estimate we must have, where applicable:

- Stone type, origin (lab or natural), carat, color, clarity, cut or shape.
- Metal, karat, and color.
- Finger size, or length or dimensions.
- Piece type, quantity, and setting or style.

Rules that decide whether a field counts as complete:

- A descriptive phrase ("classic band", "solitaire", "channel-set") or an
  explicit "you choose" satisfies setting or style. Placeholders like
  "unknown" or "TBD" do not.
- Quality choices (color, clarity, cut, finish) delegated to the jeweler are
  complete; the shop's defaults become owner-only pricing assumptions.
- Stone origin is never delegatable when the shop says "always ask". The
  customer must choose lab-grown or natural.
- Budget and event date are not prerequisites.
- Shop sourcing is assumed unless the customer says they are supplying.
- A piece with no stones has no stone fields; we never report a misleading
  completeness score.

**If something is missing:** one friendly, price-free, batched request in the
original thread, asking only for what is still unknown, at most four bullets,
combining related items. If the customer asked "roughly what would this run",
we explain honestly why a number now would mislead them and fold that into the
same request. If the shop has declared availability, the request offers two
real open slots with the timezone; otherwise it offers none.

- Stage 1: the request is drafted for the owner to send.
- Stage 2 or 3: the desk sends it.

After one partial reply we may ask once more, only for load-bearing gaps. After
that, the decision goes to the owner. The record status is `awaiting_specs`
while we wait.

### 6.3 Pricing (owner-only)

The gate blocks the customer message, not the math. Once the specification is
complete we price internally, deliberately on the high side:

| Line | Basis |
|---|---|
| Metal | finished grams × the shop's price per gram (or spot price × purity) |
| Center stone | carat × the shop's price per carat |
| Accent stones | total carat × the shop's price per carat |
| Fees | CAD, casting, setting, finishing, engraving, shipping from the fee catalog |
| Bench labor | hours × the shop's hourly rate, always its own line |
| Hard cost | sum of the above |
| Proposed price | hard cost through the shop's pricing model |

Quantities (finished weight, hours, a missing carat) are estimates made high.
Rates come only from the rate card. If finished weight is truly unknown, the
owner gets a bracket rather than false precision. A missing rate stops pricing
and asks the owner for it.

### 6.4 Owner approval

The owner receives one brief per estimate containing:

- Customer email, piece, and the customer-safe specification.
- The proposed price and the complete owner-only cost sheet with every
  assumption.
- The estimate ID and a binding hash tying the brief to this exact route,
  specification, and price.

The owner can **approve**, **edit the number**, or **reject**. A conversational
"yes" in chat is not approval. If anything material changed between the brief
and the send (recipient, thread, specification, price), the approval is stale
and a fresh brief is required. The record is `pending_approval` until the
owner acts.

### 6.5 The estimate goes to the customer

Only after approval, and only in the original thread:

- One general paragraph the customer will recognize as their piece, without a
  build sheet.
- The exact owner-approved price.
- The high-side note: estimated on the high end on purpose, pending final
  design approval, savings passed along, nothing locked in until they approve
  the final design.
- Estimated (never guaranteed) lead time, deposit terms, tax handling, and the
  validity date.
- Two or three live meeting options with timezone, when availability is
  declared.

Before sending, the text is checked for any owner-only material; a failure
blocks the send. The provider's message ID is stored as evidence. The record
becomes `estimate_sent`.

### 6.6 After the estimate: what the customer says next

| The customer replies with | We do |
|---|---|
| Acceptance, or "let's do it" | Alert the owner. No further price step is needed. |
| A request to see a picture | Produce two complementary illustrations of the same approved design and check each against the written specification, discarding any that changes the piece. Send the conforming images to the owner as a rendering approval. Only after the owner approves do the approved images go to the customer, in the thread, with the note that the written specification controls the final piece. Each distinct request is one iteration. |
| A request to meet | Check the calendar for an existing meeting with this customer first. Build two or three fresh near-term times that are actually free inside declared windows. Send them to the owner as a booking approval. At Stage 3 the times may be offered to the customer while the owner decides; at Stage 1 or 2 nothing goes to the customer until the owner approves. The event is written and the customer is told "you're confirmed" only after the owner approves and the calendar write succeeds. |
| A design change | Treat it as a changed specification: it returns to the gate and pricing, and the owner reviews it. |
| Price pushback or a discount request | Owner only. No customer reply is drafted. |
| Silence | Follow-up cadence in 6.8. |

Every customer reply also raises a "customer replied" alert to the owner so
nothing sits unseen.

### 6.7 Scheduling rules (all stages)

- Act on meeting intent immediately; never delay a meeting until near delivery.
- Always check the calendar first, at every stage, and never claim a meeting
  exists without finding it there.
- Every booking is approved by the owner before the event is written, at every
  stage. The approval names the customer, the candidate times, and the meeting
  type.
- Offer only times that are free on the calendar, inside declared windows,
  after minimum notice, outside blackouts, with the owner's timezone label.
- Re-check the slot immediately before writing the event.
- Confirm to the customer only after the event exists, with day, date, time,
  timezone, duration, and place.
- Never book two people in the same slot; tell the owner every booking.

### 6.8 Follow-ups

After an estimate or a specification request has gone out and the shop has
authorized follow-ups: one nudge on day 3, one on day 7, then mark the record
`dormant` and stop. Never more than two. Each nudge is skipped silently if the
estimate was approved, declined, marked dormant, or the customer has already
replied. Stage 1 drafts the nudge; Stage 2 or 3 sends when authorized.

### 6.9 Mail that is not a customer conversation

- Automatic replies (out of office): closed without action.
- Delivery failures and bounces: raised to the owner for manual review.
- Messages the desk cannot classify or that do not belong to any known
  inquiry: manual review, never a guess.

---

## 7. Escalations: hand to the owner, draft nothing

Anger or dissatisfaction, price pushback after a sent quote, discount
requests, legal threats or lawyers, insurance or chargeback matters, lost,
damaged, or "not what I ordered" claims, estate or heirloom disputes, press,
fraud or stolen-goods concerns, requests the desk does not fully understand,
"what is my old ring worth", "is this ethically sourced", "can you have it by
Saturday" when the bench has not confirmed, any request for a payment link or
card details, and any prior send whose outcome is uncertain.

A first-contact price question is not pushback; a missing setting or an
incomplete specification is an intake matter, not an escalation.

---

## 8. What we keep, per inquiry

**The route:** channel, shop mailbox, customer email, email-derived identity
key, the original message and thread identifiers, and the original subject.

**The specification:** every field in section 6.2 as the customer stated or
delegated it, merged across the whole thread, plus which required fields are
still missing.

**Owner-only pricing:** the cost sheet lines, quantities, rates, spot-price
evidence, hard cost, proposed price, and the owner-approved price.

**Evidence:** approval binding hash and approval event, outbound message IDs
for every send, calendar receipts, booking approvals and event IDs, rendering
images with their approval and send evidence, follow-up sends.

**Timeline:** inbound time, each phase, the next action date, and the trust
stage in effect.

**Audit:** one event per meaningful action (estimate requested, approved,
sent, appointment booked, retired, escalated), with an idempotent key.

---

## 9. Record statuses

`awaiting_specs` → `pending_approval` → `estimate_sent` →
`appointment_booked` or `approved`; at any point `declined`, `manual_review`,
or `dormant`.

A record is retired to `dormant` (opened in error, duplicate, superseded,
withdrawn before any price, or a test) only while no price has been sent. Once
a customer has been told a price, the matter is resolved with the customer,
not by editing the record.

---

## 10. What the owner sees, and when

The owner's channel may be a phone. Only finalized, important messages go
to the channel chosen at setup; the desk never narrates its progress, never
repeats an open item, and never sends a message a brief already covers.

| Moment | The owner gets |
|---|---|
| A customer replies on an existing inquiry | One alert naming the estimate |
| Specification is complete and priced | An approval brief with the price, the cost sheet, and the exact customer email |
| Something needs a human | An approval brief in the same queue: why it needs the owner in plain words, who wrote, the subject, and when. Approving it closes the review; rejecting leaves it open |
| A meeting is requested | A booking approval with the calendar-checked candidate times, at every stage |
| A customer asks for a rendering | A rendering approval showing the conforming images before anything is sent |
| A rate is missing | A brief asking for that rate |
| The desk itself fails | One message naming the failure |
| Nothing new happened, or work is in progress | Nothing |

Expectation set with the owner: an inquiry becomes a priced decision in the
owner's hands in about ten minutes.

---

## 11. Glossary

- **Estimate ID**: opaque identifier for one inquiry's record; never a
  customer name.
- **Route**: the exact reply path back to the customer's original message.
- **Specification gate**: the required-fields check before any price.
- **Brief**: the owner's approval request for one estimate.
- **Binding**: the hash tying an approval to the route, specification, and
  price it approved.
- **Trust stage**: how much the desk may send on its own (section 4).
- **Manual review**: a queued item the owner must resolve.

---

## 12. Change control

Changes to the workflow are made here first, by pull request, and only then
in the implementation.

Known gaps between this document and the current implementation, to be
closed by follow-up changes:

- Renderings are currently sent to the customer without an owner approval
  step. This document requires a rendering approval first.
- Stage 3 currently books meetings autonomously inside declared windows. This
  document requires owner approval for every booking at every stage; Stage 3
  may only offer times.
- The implementation still carries a wholesale mode, wholesale email wording,
  and a trade markup setting. This document is retail only; those are to be
  removed. The tests that pin customer wording and phase order
exist to keep the implementation faithful to this document.
