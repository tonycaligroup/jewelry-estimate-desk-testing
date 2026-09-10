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
| Owner / approver | Kolo: approval briefs for actions, plain-English questions in the channel chosen at setup for missing information, and the estimate records. The person who activated the desk is the approver. | Approves or rejects every price, rendering, and booking. Answers the desk's questions. Decides escalations. Sets the trust stage. |
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

The desk works in one of two modes, chosen at setup (`desk.mode`), concierge
by default. In **concierge** mode the desk gets the conversation to a call
or a visit and the owner gathers the specification: the first email
acknowledges, confirms the vision from a photo when there is one, asks only
budget and timeframe, and offers times (one card); a reply that picks a time
gets the booking card; a reply that pushes for a number gets one
acknowledgement ("I will work up the estimate and get back to you") and the
owner a nudge; every other reply is read for facts and left alone. After the
visit the owner types the details in chat in reply to the desk's standing
question; the desk renders the design, shows the views, and files one price
card naming them; approving it sends the estimate with the renderings
attached, for guidance only. No specification question ever reaches the
customer in concierge mode, and nothing is priced until the owner says so
("price it" uses what the desk has). In **auto** mode the desk asks the
details by email and prices from the replies, as 6.2 to 6.5 describe.
Everything else (cards, the ledger, the sheet, bookings, renderings on
request, repeat pieces, "I don't know") is the same in both.

### 6.1 A new inquiry arrives

**Triage the request.**

| The customer is asking for | We do |
|---|---|
| A new custom piece, replica, redesign, or remount | Continue to intake |
| A repair, resize, or restring | Repair intake, no rendering |
| An appraisal or insurance value | Stop; tell the owner; never value property |
| A ready-made piece (in stock, ready to ship, "do you have any") | Offer a visit to see what is ready, through the appointment card; the time they name is booked when free. After two replies without a booking, tell the owner to open the email and handle it, and leave the thread to them. Never a quote, never a questionnaire |
| An appraisal, insurance value, or job status | Ask the owner: "quote it" reads it as a custom order, "handle myself" leaves it to them. A message naming a piece with two of its facts (karat, metal, stone, size, budget) is an estimate request whatever the reading called it, appraisals excepted |
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
owner gets a bracket rather than false precision. A missing rate is not an
error and not a review: the desk asks the owner which rate to use (6.10),
saves the answer to the rate card, and then prices.

### 6.4 Owner approval

The owner receives one brief per estimate containing:

- Customer email, piece, and the customer-safe specification.
- The proposed price and the complete owner-only cost sheet with every
  assumption.
- The estimate ID and a binding hash tying the brief to this exact route,
  specification, and price.

The owner can **approve** or **reject**; a card is binary, never edited. An
owner who wants a different number rejects the card, and the desk asks in
words what price to file; the answer becomes a fresh brief at that price
with the same cost sheet and the new margin shown. A conversational "yes"
in chat is not approval. If anything material changed between the brief
and the send (recipient, thread, specification, price), the approval is
stale and a fresh brief is required. The record is `pending_approval` until
the owner acts.

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
| A request to see a picture | Produce two complementary illustrations of the same approved design and check each against the written specification, discarding any that changes the piece. Send the conforming images to the owner as a rendering approval. Only after the owner approves do the approved images go to the customer, in the thread, with the note that the renderings are for guidance only (they show the direction of the design; a close rendering is still not the finished piece) and that the written specification and the approved final design control the final piece. Each distinct request is one iteration. |
| A request to meet | Check the calendar for an existing meeting with this customer first. Build two or three fresh near-term times that are actually free inside declared windows. Send them to the owner as a booking approval. At Stage 3 the times may be offered to the customer while the owner decides; at Stage 1 or 2 nothing goes to the customer until the owner approves. The event is written and the customer is told "you're confirmed" only after the owner approves and the calendar write succeeds. |
| A design change | Treat it as a changed specification: it returns to the gate and pricing, and the owner reviews it. |
| Price pushback or a discount request | Owner only. No customer reply is drafted. |
| Something the desk cannot read with confidence | Ask the owner what it is, quoting the customer's words (6.10). Nothing goes to the customer until the owner answers. |
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

### 6.10 When the desk needs the owner: questions versus approvals

The desk has exactly two ways to put something in front of the owner.

| It needs | It sends | The owner answers |
|---|---|---|
| Permission to act (send a price, send a rendering, book a meeting, any send) | An approval brief in the Kolo approval queue | Approve or reject, nothing else |
| A fact only the owner has (a rate, what a customer meant, whether two threads are one piece) | A plain-English question in the channel chosen at setup | In plain words, in the same channel |

A review list is not one of the ways. An owner should never have to go
somewhere else first, do something, and come back. An approval must be
answerable from the card; a question must be answerable in one reply.

**What a question contains.** Who the customer is and what they asked, in
one line. What the desk is missing and why it cannot guess. What answer it
needs, with an example of an acceptable reply. A short reference when more
than one question is open, so the owner can say which one they mean.

> Tony Lomelino asked for a quote on a synthetic sapphire ring. I do not have
> a per-carat price for synthetic sapphire on your rate card. What price per
> carat should I use? For example: "use 450".

> Tony Lomelino replied to the pendant estimate: "Could you also do a matching
> band?" Is this a second piece to quote separately, or a change to the
> pendant?

**What happens with the answer.** The desk records it against the estimate
with provenance (answered by the owner, when, for which question). A fact
that will be needed again is saved where it belongs: a rate goes on the rate
card, so the same question is never asked twice. The inquiry then resumes
from where it stopped, and its result still passes through the normal
approval. A rate answered by the owner produces a price brief; the owner's
second touch is the approval they would have had anyway.

**Unanswered questions.** The inquiry waits. The customer is told nothing
about the delay beyond the normal acknowledgement they already received. The
desk reminds the owner once, after a working day, and then leaves it.

**What stays a notice.** A failure of the desk itself (mailbox, credentials,
a send whose outcome is uncertain) is reported once, in the channel, as a
statement of what failed. Those are the only items that may sit on a review
list, and the list is for the desk's own recovery, not a place the owner is
expected to check.

---

## 7. Escalations: hand to the owner, draft nothing

Anger or dissatisfaction, price pushback after a sent quote, discount
requests, legal threats or lawyers, insurance or chargeback matters, lost,
damaged, or "not what I ordered" claims, estate or heirloom disputes, press,
fraud or stolen-goods concerns, requests outside estimating a piece,
"what is my old ring worth", "is this ethically sourced", "can you have it by
Saturday" when the bench has not confirmed, any request for a payment link or
card details, and any prior send whose outcome is uncertain.

A first-contact price question is not pushback; a missing setting or an
incomplete specification is an intake matter, not an escalation. A reply the
desk cannot read with confidence is a question to the owner (6.10), not an
escalation: the desk keeps the conversation once the owner answers.

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

The owner's channel may be a phone. Only finalized, important messages and
questions the desk cannot proceed without go to the channel chosen at setup;
the desk never narrates its progress, never repeats an open item, and never
sends a message a brief already covers.

| Moment | The owner gets |
|---|---|
| A customer replies on an existing inquiry | One alert naming the estimate |
| Specification is complete and priced | An approval brief with the price, the cost sheet, and the exact customer email |
| The desk needs a fact only the owner has | One plain-English question in the channel: who asked, what is missing, what answer is needed (6.10). The owner replies in words and the desk continues |
| A meeting is requested | A booking approval with the calendar-checked candidate times, at every stage |
| A customer asks for a rendering | A rendering approval showing the conforming images before anything is sent |
| A rate is missing | A question in the channel asking which rate to use; the answer is saved to the rate card and the price brief follows |
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
- **Question**: a plain-English request for one fact only the owner has,
  sent to the owner's channel and answered there in words (6.10).
- **Manual review**: an item on the desk's own recovery list for a failure of
  the desk itself; never a substitute for a question or an approval.

---

## 12. Change control

Changes to the workflow are made here first, by pull request, and only then
in the implementation.

Known gaps between this document and the current implementation, to be
closed by follow-up changes:

- Cards are binary (6 September 2026): approve or reject, the edit option
  is withdrawn. Every approval is executed by the desk from the audit
  trail, price cards included; the chat session runs nothing on an
  approval. A rejected price is followed by the desk's question for the
  price to file and a fresh brief at that price (built 6 September 2026).
- Built 10 September 2026 (unpublished): a piece on file outranks the
  reading's guesses. "The exact same thing, but with yellow diamonds" keeps
  the halo and its diamonds from the earlier estimate even when the reading
  guessed "prong" for the new studs; only the customer's own new words
  replace a fact on file (live: the rendering of a look-alike lost its
  diamond halo) (6.1, 6.6).
- Built 9 September 2026 (unpublished): a phone call is booked as a call
  ("are you available for a call tomorrow at 3pm?"): the card, the calendar
  event, and the confirmation say so; the customer's number is read from
  anything they wrote (a signature counts) and put on the invitation, and
  when the desk has none the confirmation asks for the best number; a reply
  carrying it goes onto the invitation, the record, the Customers tab, and
  one line to the owner, with nothing sent back (6.7). The customer's own
  scheduling sentences ride along with the reading's quotes, so "a call
  tomorrow at 3pm" books 3pm rather than offering times (live: the reading
  returned "tomorrow").
- Built 9 September 2026 (unpublished): the rate card is the desk's fields
  with the jeweler's numbers, blank until they fill it: a "Rates" tab (the
  Value column theirs, read back every tick, journaled, a bad cell named
  once in chat), their own pricing notes read in at setup by
  `rates_intake.py` with nothing invented, a rate the card lacks asked in
  chat with "use 450 once" for this estimate only. The rules those numbers
  switch on, each silent without its number: setting labor for the center
  by carat band (with fancy, bezel, and fragile extras) and per melee stone
  by style, melee sized by count and millimetres against the chart and the
  size band, metal and melee waste, contingency by complexity, the minimum
  job charge, and a live-quote line for lab-grown diamond centers above the
  jeweler's threshold (asked, never guessed). Every added line says what it
  was computed from and the provenance check redoes the arithmetic (6.3,
  3). The Customers tab takes the owner's corrections (name, phone, notes)
  and the corrected name greets the customer. A carat range ("2 to 3 ct")
  is an answer priced at its top and shown as the assumption; a follow-up
  never opens like the last one (6.2).
- Built 9 September 2026 (unpublished): the cost sheet is where the owner
  works. One block per estimate (a header row with a Status dropdown and a
  Details cell, one row per cost line, two spare lines) shows what the desk
  can already fill for an open estimate, rates from the card with the
  quantities blank; the owner's Quantity, Unit cost, Details, and Status
  cells are read back every tick and kept on the record as a draft, so
  nothing typed is lost; `ready` prices the estimate from those numbers
  (the owner's grams, hours, carat, and unit costs outrank the model's) and,
  in concierge mode, renders it, the same as the chat answer; the block is
  the desk's again once acted on and reads `pending approval`, then
  `quoted`. The other tabs stay the desk's own views (7).
- Built 9 September 2026 (unpublished): "the emerald earrings we talked
  about earlier, the same but with sapphires" carries the earlier estimate
  (quoted or not) for the same customer beneath the new words, asks nobody,
  and prices; a known customer pointing at an earlier conversation or a
  piece the shop made is a new inquiry, never the "same piece or new?"
  question (6.1, 6.2). The follow-up email asks the desk's own questions,
  one bullet each, the metal as one question, never three.
- Built 9 September 2026 (unpublished): the spreadsheet's "Cost sheet" tab
  carries the full breakdown of every price card, one row per cost line
  (metal by the gram, stones by the carat, labor by the hour, fees), then
  the hard cost total and the quote with its markup; a tab added after
  setup is created on the existing spreadsheet at the next push (7).
- Built 9 September 2026 (unpublished): cards born from one customer email
  travel together. When a booking card and a rendering card (or a price
  card) come from the same message, the first approved holds its email (a
  booking still lands on the calendar at once, so the invitation reaches
  the customer) and the second sends one email carrying both, whichever
  order the owner approves them in; a rejected partner releases the held
  send alone, and so does a partner left undecided for thirty minutes. An
  offer of times is never part of a pair. Any command that reports a card
  executed marks the desk's own registry, so a line the session pasted
  never leaves a partner waiting (6.4, 6.5, 6.6). Live: a confirmation and
  the renderings from one reply went out as two emails.
- Built 9 September 2026 (unpublished): a clock bound in the customer's
  words ("after 1pm any day next week", "before 3", "2pm or later") narrows
  the offered times inside the days they named; "after 1pm on Monday" is a
  bound, not a pick of 1pm (6.7). Live: "next week; after 1pm any day next
  week" was offered 9:00 AM.
- Built 9 September 2026 (unpublished): the spreadsheet mirror never lists
  a record that triage closed as not an inquiry (vendor, personal, or
  unrelated mail opens a record before it is read), nor a test, a mistake,
  or a duplicate; a customer who withdrew stays (7).
- Built 9 September 2026 (4.15.4): concierge mode, the default, as
  described at the head of section 6 (the jeweler, 9 September 2026). One
  profile setting (`desk.mode`), the standing details question per
  estimate, the acknowledgement email, one price card that carries the
  renderings and one estimate email that attaches them. Auto mode is the
  behaviour built before this date, unchanged.
- Built 9 September 2026 (unpublished), the jeweler's rule from the Blue
  Topaz thread: a customer who says the shop made the piece ("an exact
  replica of the pendant you made for me") is never asked about it. The
  desk looks for the piece in its own records (the same customer, the same
  kind of piece, quoted before): found, its facts ride beneath the new
  words (a new size replaces the old stone), nothing on it is asked, and
  the estimate says it follows the piece made before; not found, the owner
  is asked once before anything is sent: the original's details (which
  price the new piece as owner facts), "not on file" (the desk asks the
  customer what it still needs, with or without a photo), or "handle
  myself". A repeat customer with an open estimate elsewhere is not asked
  "same piece or new?" when their words say it is a piece made before (6.1,
  6.2, 6.10).
- Built 9 September 2026 (unpublished), from the Blue Topaz thread: the
  address line says who is writing, so a gift-giver is greeted by their own
  name, never by the person the piece is for (a draft that greets someone
  else is refused); a stone sized in millimetres by the customer ("15mm x
  12mm oval") is sized, the carat is derived by the jeweler and shown as an
  assumption on the card, never asked (6.2).
- Built 9 September 2026 (unpublished): with a reference photo, the desk
  confirms rather than questions. What the photo shows (the kind of earring,
  the setting) fills the reading, and the first email says the customer's
  vision back the way a jeweler would ("Just so I have your vision right:
  you are after sapphire stud earrings with a diamond halo, round lab-grown
  sapphires at 2.5 ct each, in 14K white gold. Tell me if any of that is
  off.") before the details it still needs; a draft that skips the
  confirmation is refused. Nothing the photo shows is asked (6.2).
- Built 9 September 2026 (unpublished): "earrings" alone are studs,
  hoops, or drops. The customer's words settle it ("studs please", "hoops");
  a photo that shows which is read and stated; otherwise the desk asks the
  one plain question ("what style of earrings: studs, hoops, or drops?")
  with the other details. The style names the piece on the card and in the
  emails and settles the render's construction (6.2, 6.6). Live: "a pair
  of earrings" with a halo and no photo rendered as leverback drops.
- Built 9 September 2026 (unpublished): a stretch of days the customer
  names without a clock time ("next week", "early next week", "Friday
  afternoon", "tomorrow morning", "the 15th") is where the offered times
  come from; only when nothing is free there do the nearest days stand in,
  and the card says so. Live: "times next week" was offered today and
  tomorrow (6.7). After the shop has offered times, a reply that names a
  day without a clock time ("Monday the 21st would be best") answers that
  offer: it keeps the meeting and the card is filed for that day (live: such
  a reply, with the details, was priced with no meeting card at all).
- Built 9 September 2026 (4.15.3), from the ruby earrings thread: when
  a reply completes the details and picks a time, the meeting card is filed
  before the review, so a missing rate (the owner's question) never skips
  it; while that card is pending the estimate says the visit is being
  confirmed separately and does not ask for a time, and once booked it says
  so (6.3, 6.7). A setting the customer names in their own words ("not sure
  the halo size") is theirs, never the jeweler's choice (6.2). The metal is
  one question (which metal: yellow, white, or rose gold, 14K or 18K), not
  three bullets. A pair's stones are named in the plural with the carat
  basis ("lab-grown rubies, 2.5 ct each"). The rendering email says the
  pictures are for guidance only, and a render from the customer's example
  photograph is an edit of that photograph: the prompt names only what
  changes (rubies instead of diamonds, the metal), the first view is the
  photograph's own, the edit asks the provider to keep the reference's
  features, and no archetype exemplar rides beside their photo (6.6).
- Built 9 September 2026 (4.15.0): an optional spreadsheet
  mirror built for the counter: "Customers" first (one row per customer,
  status in plain words, next meeting, what is still open, a link to the
  thread), "This week", "Price cards" (quote, cost, profit, assumptions,
  owner-only figures off the first tab), and "Facts" (every fact with its
  source). Created at setup in the shop's Google account through the same
  gateway token, or adopted by URL; rewritten after a tick that changed
  something; never read back (7).
- Built 9 September 2026 (4.15.0): renderings from the ledger.
  The facts that must be exact open the prompt in order (the piece, each
  stone with its colour, the metal, the setting), the checker asks about
  each by name, the archetype follows the piece's own words ("stud
  earrings" is never drops), and when the customer sent an example piece
  the render is that photograph changed only as specified, not a logo to
  reproduce (6.6).
- Built 9 September 2026 (4.15.1): for a pair (earrings,
  cufflinks, studs, hoops) a stated carat is either each stone's or the
  pair's total, and the price differs by half. The customer's words settle
  it ("per earring", "each", "total", "tcw"); with no such word the desk
  asks the one question a customer can answer ("is the carat weight you
  gave the weight of each stone, or the total for both?"); "each" prices
  two stones and the card and the render say which. Pairs only (6.2, 6.3).
- Built 9 September 2026 (4.15.1), from two simulation
  runs of the desk against the fake world: a reply after the estimate that
  needs no card (thanks, an acceptance, a cancellation) now completes its
  claim instead of retrying until it stuck; an acceptance and a cancellation
  each tell the owner in one sentence, and a cancellation takes the booked
  time off the calendar with nothing sent to the customer; a customer who
  writes twice before the first tick is one inquiry, and a message written
  before the desk's question went out is never a non-answer; the shop's own
  lines pasted under a reply are not the customer's words; a reading that
  called an order "inventory" is rescued like the other kinds; a stated
  fact is attributed to the piece its clause names ("14k for the band");
  a quoted grade the customer releases to the jeweler moves; a boolean
  "no center stone" renders as one; notes never pick a render archetype;
  the sheet is written in one batch so it is never half new (6.6, 6.10).
- Built 9 September 2026 (4.15.0): the estimate ledger. Every
  fact the desk holds carries its source (the customer's own words with the
  span, the photo, the jeweler's choice, the owner, a quoted estimate, or a
  bare reading), in `estimate-desk/ledger.sqlite`; the record's
  specification is derived from the rows that stand. The customer's written
  word is never overwritten by a photo or a re-read; a quoted fact moves
  only for a change the customer named. A stone's color and clarity are
  never asked: the jeweler chooses, the price card says "jeweler's choice:
  color, clarity" and what came from the photo, and the estimate email says
  the jeweler chose them and invites a preference; a grade the customer
  gives for the halo or accent stones is kept apart from the center stone's
  (6.2, 6.3).
- Built 8 September 2026 (4.15.0): when a customer asks
  to come in and for a price in one email, nothing reaches them before the
  owner's approval: the offer card carries the questions the estimate needs,
  and the one email the approval sends offers the times and asks them
  (6.6, 6.10). This replaces the 4.14.5 rule that sent the questions in the
  same tick as the card.
- Built 8 September 2026 (4.15.0): an owner's reply
  without a code goes to the one open question its words fit ("skip" fits
  only a stalled follow-up), or to the code the owner put in the reply
  ("skip 036BAF"); when the words fit several, the desk refuses and names
  the codes. The session never reads the questions folder to choose (6.10).
- Built 8 September 2026 (4.14.7): the desk asks a
  customer only what they can answer about what they want, and interprets
  the reference and the description for the rest. A size is required only
  where a customer can name one (a chain or bracelet length, a wrist, a
  ring size, the length of hoops or drops); studs, pendants, and any
  earring with a stated stone never need one. The follow-up never asks a
  technical question (millimetres, diameters, drop lengths, weights, prong
  or stone counts, band widths): the guard rejects such a draft and a plain
  question goes instead. A reply that leaves an asked detail to the jeweler
  ("I don't know", "you decide", "just a reference") makes it the jeweler's
  choice in code and the desk prices, with no question to the owner. The
  customer's words always outrank the photo (6.2).
- Built 8 September 2026 (4.14.6): every script's
  checksum is recorded in `scripts/manifest.json` for the version in
  SKILL.md; readiness fails, naming the files, when the installed folder
  does not match it (a pod ran three releases of stale scripts under the
  right version number).
- Built 8 September 2026 (4.14.5): when a customer
  mentions an estimate, a price, or a cost, the desk pursues it whether or
  not they also ask to come in: the appointment card offers the times and
  the questions for the estimate go out in the same tick (the follow-up
  then says the times are coming separately); a reply's meeting request
  stands only on the reply's own words (asking to come in, a day and time,
  or a pick of an offered time), never on the re-read of the thread (6.2,
  6.6).
- Built 8 September 2026 (4.14.4): example photos a
  customer attaches are read at intake by the vision model, once per
  message; what is visible (piece, metal color, stones, setting, design)
  fills the reading as if written, marked "from the photo"; a carat,
  karat, size, or length is never taken from a photo; the follow-up says
  in one sentence what was taken from the photo so the customer can
  correct it (6.2). An appointment offered or confirmed before any
  estimate never mentions an estimate: the visit is to design their
  perfect piece together (6.6).
- Built 8 September 2026 (4.14.4): a meeting asked for
  in an earlier email is not asked for again by a reply about something
  else; "before I come in, can I get a ballpark estimate?" after an offer
  of times goes to the estimate (the gate, the follow-up, the price), and
  only a reply that itself asks for a meeting or picks a time gets an
  appointment card (6.6).
- Built 8 September 2026 (4.14.3): a ready-made
  inquiry (in stock, ready to ship, "do you have any") is offered a visit
  through the appointment card, never a quote or a questionnaire; the time
  the customer names is booked when free; after two replies without a
  booking the desk tells the owner to open the email and handle it and
  leaves the thread to them. An appraisal or job-status message is a
  question to the owner ("quote it" or "handle myself"), never a silent
  manual review; a message naming a piece with two of its facts is an
  estimate request whatever the reading called it (triage table, 6.10).
- Built 8 September 2026 (4.14.3): a day and clock
  time in the customer's own words ("would Friday at 3pm work for you?")
  is resolved to a date in code, whatever the reading resolved; a free
  time inside the windows is a booking card, never an offer of other days
  (6.6).
- Built 8 September 2026 (4.14.3): whether a message
  asks for a meeting is decided in code from the customer's own words: a
  named meeting (appointment, come by the shop, in person) or a proposed
  day and time ("any chance we can do Friday at 4pm?") is handled as a
  meeting request even when the reading missed it, and a reschedule of a
  meeting booked before the estimate goes to the appointment card, never
  to the questionnaire; a deadline ("ready by Friday at 5pm") is not a
  visit (6.6).
- Built 8 September 2026 (4.14.2): a piece is a ring
  only when "ring" or "band" is a whole word of its name; earrings are
  asked for their size, never for a finger size (6.2).
- Built 8 September 2026 (4.14.2): a rendering's
  reference images come only from the customer's own messages, never from
  the desk's earlier rendering emails on the thread; the vision check
  reaches the provider with thinking off and a model it knows (6.6).
- Built 8 September 2026 (4.14.1): an eternity,
  channel-set, pave, or all-around band has no center stone whatever the
  reading says; its stated carat is the total of the small stones; the
  desk never asks such a customer for a center stone's carat or cut (6.2).
- Built 8 September 2026 (4.14.0; RELEASE-PLAN-4.14.md):
  every judgement the desk makes goes to the model provider directly when
  its address and key are in the desk's environment (about a second a
  call against thirteen through the platform CLI, thinking off, the same
  prompts and checks), the CLI remaining the fallback; the tick may take
  up to sixteen claims and, when the profile says so, run different
  customers' claims side by side while one customer's claims stay in
  order; the platform commands that file cards and notices retry a busy
  CLI and are cut off if they hang (10).
- Built 8 September 2026 (4.13.11): renderings call the
  image provider directly when its address and key are in the desk's
  environment (the platform CLI took 40 to 351 seconds per image and ran
  one command at a time; the provider answers in about ten seconds and
  takes several calls at once), so every view of a rendering is made in
  the tick that plans it, all at once; the CLI remains the fallback. New
  mail is always handled before a rendering under way. The profile may
  turn the vision check off (the owner is the check) and set the views per
  piece, the image size and quality, and how many run at once (6.6, 10).
- Built 8 September 2026 (4.13.10): discovery asks Gmail
  for messages since the watermark minus two hours, so a message the
  search index lists late is still found; messages already queued are
  skipped, so nothing is handled twice (10).
- Built 8 September 2026 (4.13.9): what the desk knows
  about a reopened piece is the quoted estimate first (the customer
  confirmed it), the later review only for what the estimate never had,
  and the newest reading on top of both; a requeue closes the question
  that parked the message, since the desk starts it again (6.2, 6.8).
- Built 8 September 2026 (4.13.7): the reading of a
  customer's new message is handed the specification the record already
  holds and merges the new words into it; a change to a quoted piece from
  a new thread keeps every quoted fact, the named piece takes the change,
  an unnamed change applies to every piece, and nothing on the record is
  asked again (6.1, 6.2, 6.8).
- Built 8 September 2026 (4.13.6): a rendering run that
  dies while filing its card resumes to the card; it never renders again.
  The finished report is the rendering; the progress file outlives the card
  step; a card binding no card was ever filed against (the run died before
  filing) is kept as history and the images the owner will see are bound;
  a card already in Kolo's audit trail is found, not filed twice (6.6).
- Built 7 September 2026 (4.13.5): a rendering view keeps
  to the watcher tick's clock. The desk cuts an image or vision call off
  itself at the tick's deadline (the platform's own timeout flag is not
  honoured; one generation ran 351 s and killed the tick), so a tick is
  never killed and every attempt is logged; a call refused because the
  platform CLI was busy ("database is locked": two commands at once) is
  tried again seconds later; no regeneration or vision retry when time is
  short; a view killed three ticks running becomes a question to the owner.
  The doctor names a tick that started and never finished. The profile may
  set `rendering.views_per_piece` to 1 (default 2) for speed over angles
  (6.6, 10).
- Built 7 September 2026 (4.13.5): a new piece that is a
  quoted piece in another colour or finish (every fact that drives weight,
  stones and labour the same; colour, finish, engraving and stone grade may
  differ) takes the quoted piece's grams, hours, stones and fees; the model
  is not asked. A changed size, width, metal, karat, stone or setting is a
  different piece and is estimated (6.8).
- Built 7 September 2026 (4.13.5): two pieces of one
  kind are told apart on the card by what differs ("men's wedding band,
  rose gold", or "size 5"; a still-identical pair is numbered), so every
  assumption line names its piece (6.3).
- Built 7 September 2026 (4.13.4): a sent estimate is a
  commitment; when a second piece is added, every piece already quoted keeps
  the grams, hours, center carat, fees and accent stones it was quoted on
  (from the archived cost sheet) and only the new piece is estimated; rates
  come from today's rate card. A fee named shipping, postage or courier is
  charged once per order, not per piece. A price card's title keeps the
  piece words whole and shortens the assumptions instead (6.8, 6.3).
- Built 7 September 2026 (4.13.3): a fact written at the
  top level of a multi-piece reading applies to every piece that lacks it,
  stone facts included, not only metal; after "second piece" the first
  piece keeps every fact it was priced with, and a reading that came back
  as one flat piece is taken as the new piece beside the priced one; a
  multi-piece reading is gated per piece only, with no bare top-level
  requirement. "Reuse", "reset", "remount", "my own" count as the customer's
  own stone only with a stone word near them ("reuse the wedding band" is a
  band, not a stone). Nothing already on the record is asked again (6.2).
- Built 7 September 2026 (4.13.2): a customer who asks for a time outside
  the declared consultation hours is told the hours and offered free times
  inside them; nothing is booked outside the hours (6.7).
- Built 6 September 2026 (4.12.0): a rejected rendering card asks what
  should change and only the named piece is rendered again (6.6); the
  model's reading is cross-checked against the customer's words before any
  price and a disagreement is confirmed in the follow-up (6.2); after an
  estimate, "change" reopens the gate on the same thread and "second piece"
  adds a line with one total (6.8); a known customer's "same" on a new
  thread carries the estimate on there (6.1); rehearsal mode handles one
  named address and holds everyone else's mail untouched (10, 6.5).

- Renderings are approval-gated since 3 September 2026: the owner sees the
  views in chat and approves a card before anything is emailed.
- Bookings are card-gated at every stage (verified 6 September 2026): the
  implementation has no autonomous booking at any trust stage; every booking
  and every offer of times is a card the owner approves.
- Questions (6.10) are built for a missing rate, a known sender writing on a
  new thread (same piece or new), and an unclear reply after an estimate
  (second piece, change, accepts, or the owner handles it), 3 September 2026.
  Failures of the desk itself are a plain notice. An owner's "change" or
  "second piece" answer closes the thread to the owner for now; the desk does
  not yet reopen the gate or open a second estimate on the same thread.
- Setup no longer asks retail or wholesale (4 September 2026); the profile
  validator accepts `retailer` only. The wholesale email wording and the
  trade markup setting were removed on 6 September 2026. The tests that pin
  customer wording and phase order exist to keep the implementation faithful
  to this document.
- The rules that read a customer's own words (meeting sentences, 'I don't know', a piece the shop made, an earlier conversation, pair carats, millimetre sizes, phone numbers, call or visit, periods of days, technical questions, greeting the right person) are listed in one table, `scripts/words.py`, each with the live phrases from tester threads it must keep reading right; `tests/test_words.py` runs every phrase. A tester's new phrasing becomes a row there before a rule changes (10 September 2026). The estimate record itself is described key by key in `RECORD-SCHEMA.md`, checked against the code by `tests/test_record_schema.py`.
- A meeting is a visit unless the customer asks for a call in words (phone, call, zoom, video); a phone number in a signature is kept for the Customers tab and the invitation but never turns a visit into a call, and a visit's confirmation may never say the shop will call them (the guard rejects the draft). 'Bring them in' and 'show you' are visit words. After a meeting is booked, a courtesy note ('See you tomorrow!', 'Thanks, looking forward to it', with or without a signature) sends nothing: the claim is done, the facts stay on the record, the rest is settled at the meeting. When a later message does need questions, the questions email says the rest can be settled when you meet and does not invite them to come by. Live, 9 September 2026 (David): a visit was confirmed as 'I'll call you at (310) 810-3004', then 'See you tomorrow!' got a questionnaire that invited him to come by.
