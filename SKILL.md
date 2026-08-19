---
name: "jewelry-estimate-desk-testing"
description: "Custom-jewelry inquiry → complete spec → priced estimate → owner text approval → customer send. Quotes, briefs, bookings; never sends unapproved prices."
version: 1.1.0
tags: [jewelry, estimating, quoting, sales, custom-jewelry, retail, wholesale, scheduling, crm]
---

# Jewelry Estimate Desk Testing

Turn an inbound custom-jewelry inquiry into an **approved quote** and a
**booked appointment**, fast — without ever putting a number or a promise in
front of a customer that the owner hasn't seen.

> **Shop owners: read `references/OWNER-GUIDE.md` first.** It's one page, plain
> English, and it's the honest list of what this does and what it will never
> do. Everything below is instructions to the agent.

**The metric:** minutes from inbound to a decision sitting in the owner's
hands. Target under 15; under 5 on a familiar piece type. Logged every time.

**The rules that nothing overrides:**

1. **No price, discount, or delivery promise reaches a customer without the
   owner's approval.**
2. **No estimate goes to a retail customer until the spec is complete.**
3. **Never expose cost inputs, markup, margins, or vendor identities.** One
   all-in number.
4. **Never touch money.** No cards, deposits, refunds, or payment links.

```
inbound email → Phase 1 triage
      ↓
Phase 1.5  SPEC GATE — do I have everything?
      ├─ no  → ONE friendly batched ask, price internally anyway, wait
      └─ yes → Phase 2 price it
                    ↓
        Phase 3  brief + TEXT THE OWNER  ◄── customer send is blocked here
                    ↓
        owner reviews / edits / approves
                    ↓
        Phase 4  send approved number + high-end / pending-CAD note
```

---

## Trust Ladder — check this before every action

A shop is not handing its inbox to an agent on day one, and it shouldn't.
Read the shop profile → `Trust stage`. **Default is Stage 1.**

> **Where the profile lives:** `estimate-desk/shop-profile.md`, relative to the
> **workspace root** — *not* inside this skill's folder. Create it on the first
> job if absent. If the file is missing, that *is* the answer — run Phase 0.

| Stage | Agent may do on its own | Still needs the owner |
|---|---|---|
| **1 — Watch me** *(default)* | Nothing outbound. Draft everything, send nothing. | Every email, price, and booking |
| **2 — Ask questions** | Send price-free info requests, **including the spec-gate ask** | Every price and booking |
| **3 — Book me** | + Book/reschedule inside declared windows | Every price |

**Never advance a stage on your own.** The owner moves it, in writing, and can
drop it back to Stage 1 with one sentence. If the profile is missing or the
stage is unreadable, **assume Stage 1**.

**Owner approval is always required before sending any estimate to a retail
customer.** There is no autonomy ceiling — every price goes through the owner,
at every stage, with no exceptions. The spec gate is a hard block for retail;
wholesale/trade is advisory (state assumptions).

**Pause:** if the owner says stop, pause, or hold — **stop all outbound work
immediately**, keep the drafts, confirm in one line.

---

## The Clock

Round trips kill custom jobs — not price. Every wait costs a day, and in
wholesale it hands the job to whoever answered first.

```
inbound → triage (30s) → SPEC GATE + one batched ask (1m) → price it with
stated assumptions (5m) → ONE BRIEF + OWNER TEXT  ◄── FINISH LINE

  ┌ booking (3c) runs on its own track, in parallel, any time ┐
```

**Never block the brief on:**

| Don't wait for | Do this instead |
|---|---|
| A complete spec | Price the most likely build **internally**, state the assumption — the *customer-facing* estimate stays unsent until the gate clears |
| The customer's reply | Build estimate *and* the batched spec-gate email in parallel; both go in the brief |
| A rendering | Sales aid, not a gate. After approval or alongside |
| A vendor quote | Price from comparable jobs, label it an estimate. Vendor only for genuinely unfamiliar work |
| An invoice-derived rate card | Quick Start gets you pricing in 90 seconds |
| A slow CRM lookup | Note "history unchecked," check it after |
| The estimate, before booking | **Book the meeting the moment they ask.** The appointment converts, not the quote |

> **Changed in v1.1.0:** finger size is no longer a "don't wait for" item. It is
> a required gate field on any ring — a resize after CAD is real money. Ask it
> in the same batched email as everything else; that costs zero extra round
> trips.

**The one thing worth slowing down for:** **lab vs natural.** It swings the
number several-fold. Unstated? Price *both* rather than guess — and it is a
mandatory gate field regardless.

---

## Who This Is For

| | **Retailer** | **Wholesale middle man** |
|---|---|---|
| Sells to | End customers | Retailers, resellers, trade |
| The quote is | Retail price the customer pays | A cost the buyer marks up again |
| Cost basis | Own bench + rate card | Vendor quotes, or comparable-job history |
| Markup | Full retail on COGS | Trade markup — thin |
| Wins on | Trust, design, **the appointment** | **Speed.** First credible quote takes it |
| **Spec gate** | **Hard block on sending** | Advisory — state assumptions instead |

**"Both" is a real answer.** Route by requester: trade contacts get wholesale
treatment, walk-ins and web leads get retail. Ambiguous? Ask.

### The middle man's speed unlock

No bench rate card, just vendor quotes — and a fresh vendor quote costs a day
they don't have. **So don't, by default.** Price from the comparable-job
index, apply trade markup, and say the honest thing:

> *"Estimate based on previous comparable jobs, to help you close the sale. On
> custom work the price can move once a CAD is produced, and the final cost is
> often lower than estimated — we estimate high and pass any savings along."*

Go to the vendor first only for genuinely unfamiliar construction, exotic
stones, or unusual scale — and say so in the brief.

**No customer to ask?** Group posts and forwarded photos are common in the
trade. Price it with every assumption stated and go straight to the brief.
**The spec gate does not block a trade quote** — it still blocks every retail
send.

---

## Phase 0 — Quick Start (90 seconds, once)

Read `estimate-desk/shop-profile.md` at the start of every job. Missing? Ask
**five** questions, write the answers immediately to that path, and start:

1. **Retailer, wholesale middle man, or both?**
2. **Your markup** — a percentage over cost, or a multiple?
3. **Who approves prices, and what mailbox do quotes go out from?**
4. **Booking:** standing hours to book inside, or bring times each time?
5. **Confirm Stage 1:** *"I'll draft everything and send nothing until you say
   so."*

Also capture once: **the owner's mobile number / preferred medium for approval
texts** (Phase 3a). Absent? The ping falls back to the Kolo chat and the brief
says so.

Full template: `templates/shop-profile.md`. Blanks become questions later, not
blockers now.

### The Upgrade (async, off the critical path)

> *"Send me your past invoices — as detailed as possible. I'll build your rate
> card from your real numbers instead of market averages."*

Extract: metal $/g by karat · stone $/ct by type, **natural vs lab**, melee vs
center by size band · CAD, casting, setting, finishing, plating, engraving ·
**bench labor $/hr** · **typical finished weights by piece type** (the biggest
source of estimate error) · their markup pattern · and the **comparable-job
index**: spec → what it cost → what it sold for.

---

## Phase 1 — Triage & Spec (under 3 minutes)

| Request | Route |
|---|---|
| New custom piece, replica, redesign | Continue here |
| Repair / resize / restring | Repair intake — no rendering |
| Appraisal or insurance valuation | **A valuation is not an estimate** |
| Job status check | Look it up, answer, done |
| Price on existing inventory | Sales question |
| Wants to come in / hop on a call | **Phase 3c — book it, then continue** |
| Angry, legal, insurance, chargeback, media | **Escalate. Draft nothing** |

**Read the entire message body** — not just the subject or attachment. The
spec is usually in the body. Pull: piece type and quantity · metal/karat/color
· stones (lab or natural, type, shape, carat, quality, count) · size, length,
dimensions · setting and style · engraving and finish · **event date** · budget
· reference images · customer-supplied stones or heirloom metal · certificate ·
**scheduling intent**.

From photos, read what's readable and **flag what a photo cannot prove**:
metal color, karat, stone origin, clarity, exact carat. Never call a lab-grown
stone natural. **A photo never satisfies a gate field** — "looks like a round
brilliant, maybe 1.5ct" is an assumption, not a spec.

Check the CRM while you work. Slow? Note "history unchecked" and keep moving.

**Surprise check:** engagement and gift work is often secret. Reply **only** on
the channel the customer used, **keep the subject line blank of detail** —
"Following up on your inquiry" beats "Your custom engagement ring" — and never
contact a shared address or household number.

---

## Phase 1.5 — The Spec Completeness Gate ★ NEW in v1.1.0

**This is the first thing you do when the owner points you at an inbound email
and says engage the customer.** Before drafting a single customer-facing
number, answer one question:

> **Do I have everything I need from this customer?**

Fill the checklist from the message, the attachments, the CRM, and the
customer's own words — **never from a guess and never from a photo**. Anything
blank is a field you must ask for.

### The checklist

Mark each **have it / assumed / missing**. Only "have it" clears the gate.

| Field | Applies to | Why it's load-bearing |
|---|---|---|
| **Piece type & quantity** | all | Everything keys off it |
| **Stone type** | with stones | Diamond vs sapphire vs moissanite is a different price universe |
| **Lab or natural** | with stones | Swings the number several-fold. Never assume |
| **Carat / ctw** | with stones | Center ct and total ctw are different numbers — capture both |
| **Color** | with stones | Grade band, not a guess |
| **Clarity** | with stones | Same |
| **Cut / shape** | with stones | Drives stone cost and setting labor |
| **Metal & karat** | all | 14K vs 18K vs platinum |
| **Metal color** | all | Yellow / white / rose — white implies rhodium plating |
| **Finger size** | rings | Required. A resize after CAD is real money |
| **Length / dimensions** | chains, bracelets, pendants | Weight is most of the number on stone-free work |
| **Setting & style** | all | Solitaire, halo, pavé, bezel, three-stone |
| **Engraving / finish** | all | Cheap to ask, annoying to retrofit |
| **Event date** | all | Feasibility, and the reason they're in a hurry |
| **Budget** | retail | Design guidance, not nosiness |
| **Customer-supplied stone or metal** | all | Changes the whole cost basis |

### The rule

- **Spec complete → price it and go to Phase 3.**
- **Spec incomplete → you may build the estimate internally, and you must NOT
  send it to a retail customer.** Send the batched ask instead. The internal
  estimate goes in the brief marked *provisional — pending specs*.
- **Minimum floor:** never send a retail estimate without, at bare minimum,
  **stone type, lab vs natural, color, clarity, cut, carat, metal + karat +
  color, and finger size on a ring.** Those eight are not negotiable.
- **Wholesale/trade is exempt from the block** — state assumptions and quote.

### How to ask — one email, no interrogation

**The point is to collect a lot without bogging the customer down.** A wall of
twenty questions gets no reply and reads like a form.

1. **One email, batched.** Never a drip of follow-ups.
2. **Never ask what you already know.** If the attachment says 18K rose gold,
   confirm it in a half-sentence instead.
3. **Group into three or four friendly clusters**, not a numbered list of
   sixteen: *the stone* · *the metal* · *the fit* · *the timing*.
4. **Offer defaults so answering is a pick, not homework.** "Most of our
   clients go 14K or 18K — any preference?" beats "Specify karat."
5. **Give an out on the technical ones.** "If you're not sure on color and
   clarity, no problem — tell me the look you're after and your budget and I'll
   recommend a grade."
6. **Make finger size easy.** "Do you know her ring size? If not, I can walk
   you through a couple of ways to get it without spoiling the surprise."
7. **No prices.** That's what makes it sendable at Stage 2+.
8. **Close with two real open slots.** A consult answers every field in ten
   minutes.

> **The best version of this ask is an appointment.** If they're local, a
> 30-minute consult clears the entire checklist at once. Always offer it.

Template: `templates/spec-gate-email.md`.

**Partial reply?** Ask *once* more, only for what's still missing and still
load-bearing, and tell the owner in the brief. If the customer genuinely can't
answer, **the owner decides whether to quote around it.** Flag it, don't stall
forever.

**Log the gate state** on every job: `spec_complete: true|false` plus the
missing fields (Phase 5).

---

## Phase 2 — Price It Now

Don't wait for the customer to price *internally*. The gate governs
**sending**, not calculating.

| Line | Basis |
|---|---|
| Metal | finished grams × $/g for that karat |
| Center stone | carat × $/ct for that type/quality |
| Accent stones | total carat × $/ct, or per stone |
| CAD · casting | per profile |
| **Bench labor** | **estimated hours × $/hr** |
| Setting · finishing · engraving | per profile |
| Outside services | as quoted |
| **COGS** | sum |
| **Quote** | COGS × markup for that mode, rounded clean |

**The labor line is mandatory.** Metal + stones + CAD charges $0 for bench
time and systematically underquotes hand work. No labor rate set yet? Estimate
the hours anyway, show them as their own line, and make the owner price them.
**Never bury labor in markup.**

**Weight is the biggest error source.** Use invoice-derived typicals. Genuinely
unknown? Quote a **bracket**. An honest range beats a confident wrong number.

**Price both columns where a real choice exists** — lab vs natural, economy vs
traditional, AA vs AAA, hollow vs solid — and recommend one.

**Estimate on the high end, deliberately.** Custom work moves once CAD is
produced, and the final cost is often lower. Price the top of the plausible
range, disclose that you do, and pass savings down — exactly what the
customer-facing note in Phase 4 says.

Say the honest thing about the cheap option. If hollow tube dents at the
joints, the $65 saving isn't a saving.

**Metal spot exposure:** over ~40% of COGS in metal, shorten validity and print
the expiry. Under ~15%, standard window.

---

## Phase 3 — One Brief. This Is the Finish Line.

Everything the owner needs, in one message, decidable at a glance. **Don't
make the owner ask you anything.**

1. **Spec gate status** — ✅ complete, or ❌ with the exact missing fields
2. **The number**, and the recommendation if there are two
3. **Who and what** — customer, channel, piece, one-line spec
4. **Cost sheet** — line items, COGS, markup
5. **Assumptions**, flagged where load-bearing
6. **The draft customer email**, ready to send
7. **Event date** and whether it's feasible
8. **Appointment** booked or slots proposed
9. **What's unknown** and whether it can move the price

### The first job, before any rate card exists

No $/g, no labor rate, no markup — so there is **no number**. Don't stall and
don't invent market averages. Ship a **quantity skeleton**: every cost line
present with its *quantity* filled in (grams, carats, stone counts, **bench
hours**) and the dollar columns blank, plus the five Quick Start questions.

That skeleton **is** a valid brief — the clock stops there.

```bash
kolo request-approval \
  --agent-id main \
  --action "Custom estimate — <Customer> — <piece> — $<quote>" \
  --reasoning "<spec gate status, weights, stone spec, labor hours, option recommended, assumptions>" \
  --risk-level medium \
  --details "$(cat /tmp/brief-details.json)" \
  --session-key "<current session key>"
```

Write `--details` JSON to a **file** and pass `$(cat …)`. Inline JSON breaks on
shell quoting.

**Then message the chat.** The CLI call is not a notification. Every status
change gets a chat message.

**The number that goes out is the one the owner approved**, not the one the
formula produced. Owner hands you a different figure? Use theirs.

### Phase 3a — Text the owner for approval ★ NEW in v1.1.0

**The moment the spec gate clears and the estimate exists, text the owner.**
Not "when convenient" — this is the notification that unblocks the customer,
and the owner is usually on the bench, not in an inbox.

Send it when:

- **the spec is complete** *and* the estimate is priced → *"ready for your
  approval"*; or
- **the customer has gone quiet or can't answer a gate field** → *"here's what
  we're missing, want me to quote around it?"*

```bash
kolo set-notify-preference --show   # confirm the owner's 1:1 medium
kolo notify-owner -m "<the text>"
```

`notify-owner` routes to the owner's pinned medium — set it to `sms` once
(`kolo set-notify-preference --medium sms`) and every approval ping goes to
their phone. If SMS isn't connected it falls back to the Kolo chat; say so in
the brief rather than pretending a text went out.

**Keep it short — it's a text, not the brief.** Decidable from a lock screen:

> *"Sarah M. — custom oval engagement ring, 1.8ct lab, E/VS1, 18K rose, size
> 6.5. Full specs in hand. Estimate $6,850 (high side, pre-CAD). Ready for your
> review — approve, edit the number, or tell me to hold. Details in the Kolo
> chat."*

**Surprise rule applies to the text too.** Shared phone? Keep the piece type
out: *"new custom job, specs complete, estimate ready — details in chat."*

**Then wait.** Do not send the customer an estimate off the back of your own
math. The owner reviews, edits, or approves, and the approved number is the one
that goes out. Not optional at any trust stage. The owner can always say
"just send it" on a given job, but the agent never sends a price without that
explicit approval.

**Don't overclaim.** "I've texted you the estimate" ≠ approved. "Approved" ≠
sent.

### Phase 3b — The customer email

Templates: `templates/customer-emails.md`, `templates/spec-gate-email.md`.

- **One email, batched.** The gate checklist *is* the price-moving set — ask it
  all at once. Never ask what the attachment already answered.
- **Always ask lab vs natural** on anything with stones.
- **Budget is load-bearing and asking is not rude.** Frame it as design
  guidance.
- **Confirm your read of their reference photo in one sentence.**
- **No prices** — not even a range — **without owner approval or with an
  incomplete spec**.
- **Close with two real open slots**, never "let me know what works." No
  calendar configured? Leave `[OWNER: two times you can hold this week]` or ask
  for a general window — the only sanctioned substitutes.
- **SMS** replies work in existing threads only. Need an attachment? Email.

**Biggest single speed lever:** at **Stage 2+**, the price-free gate email goes
out the moment the request lands — the customer is answering while the brief
sits in the queue. Offer it; never assume it.

---

## Phase 3c — Scheduling & the Calendar

Fires on meeting intent **at any point**. Never waits on the estimate, the
approval, or the spec gate.

Triggers: *"can I come in"* · *"hop on a call"* · *"what times do you have"* ·
*"I'm free Thursday"* · a proposed time · a reschedule or cancellation.

**Access:** run `kolo integration-routing` and use exactly the path it returns
for the shop's calendar. Never assume a route.

**Two modes**, set in the profile:

- **Preset windows** — declared standing hours *are* the standing authorization
  to book inside them. Stage 3+ required.
- **Ask each time** — propose 2–3 slots to the owner, they pick, then the
  customer. Right answer for a single-bench shop.

**A window is *permission*, not *availability*.** The live calendar always wins.

**Offering:** read live free/busy → intersect with windows minus blackouts,
honoring notice and buffers → **offer 2–3 specific times with the timezone
stated** → duration by meeting type → ask where if ambiguous.

> *"I have Thursday at 2:00 PM or Friday at 11:00 AM Pacific for a design
> consultation — about an hour, here at the showroom. Either work?"*

**Booking:** re-check free/busy **immediately before writing** → create the
event with spec, budget, event date, job number and reference links in the
description → **confirm only after the API returns success** → confirm to the
customer with date, time **and timezone**, duration, place, and what to bring →
**tell the owner in one line**.

**A consult is the fastest way to clear the spec gate** — finger size, metal
color, stone preference and budget all get answered in the room. When gate
fields are missing and the customer is local, lead with the appointment.

**Reschedule** updates the existing event. **Cancellation** removes it, notes
why, and keeps the estimate alive. **No-show** gets one friendly re-offer.

**Timezone:** the pod clock is UTC and is **not** the shop's clock. Resolve
every "today" against the owner's IANA zone from the profile. Blank? Fall back
to `kolo owner-bio get --json-output` → `localization.timezone` — fine for
internal date math, **not** fine to state to a customer until confirmed.
Neither available? Ask; never guess.

---

## Phase 4 — After Approval

1. **Send the approved quote** in the existing thread — one all-in number,
   **with the high-end / pending-CAD note below.**
2. **Generate the rendering now** if the shop offers them: `image_generate`,
   `count: 2`, aspect `4:3`. Catalog framing, exact metal color and finish,
   stone shape and setting, neutral backdrop, soft diffused key light, shallow
   depth of field, negatives ("no props, no text, no watermark"). A rendering
   is an **illustration, not a shop drawing** — say so every time.

### The high-end / pending-CAD note ★ NEW in v1.1.0

**Every approved estimate that goes to a customer carries this, warmly and in
the shop's voice** — near the number, not buried at the bottom:

> *"Just so you know how to read this: we estimate on the high end on purpose.
> This figure is pending CAD and rendering approval, and once your design is
> finalized the final price is often a little lower — if it comes in under, we
> pass that straight along to you. Nothing is locked in until you've seen and
> approved the CAD."*

Adapt the wording, never the substance. What must survive any rewrite:

1. **The estimate is on the high end**, deliberately.
2. **It is pending CAD and rendering approval.**
3. **The final number can move**, and savings get passed to the customer.
4. **Nothing is committed until the customer approves the CAD.**

Never let this read as a hedge or a bait-and-switch setup. It is a promise that
the number won't surprise them upward.

Templates: `templates/approved-estimate-note.md`, `templates/customer-emails.md`.
Non-negotiables:

- **One number.** No line items, per-gram, per-carat, component costs, vendor
  names, or margin.
- **Description stays general** — enough that they recognize their piece, not
  enough to hand a competitor a build sheet.
- **Restate the spec you priced** in one line, so a wrong assumption surfaces
  now rather than at CAD.
- **Lead time is an estimate, not a promise**, and never a guarantee against a
  wedding date unless the owner said so in writing.
- **Middle man:** include the estimate-high/pass-savings-down disclaimer.
- **Close with two open slots.**
- Attach via the `message` tool. Inline `MEDIA:` doesn't reliably attach on the
  Kolo channel.

---

## Phase 5 — Close the Loop

```bash
kolo record-upsert \
  --record-type "skill.jewelry_estimate" \
  --external-id "<customer-slug>-<job-number-or-date>" \
  --payload "$(cat /tmp/estimate-record.json)" \
  --status "estimate_sent"
```

Payload: customer and contact · channel · retail/wholesale · inbound timestamp
· **`time_to_pending_approval_minutes`** · spec · **`spec_complete`** and
**`missing_spec_fields`** · **`owner_notified_at`** and the medium used · cost
lines · COGS · markup · computed quote · **owner-approved price** · lead time ·
event date · assumptions · rendering paths · thread id · next action date ·
**`appointment`** (event id, start with timezone, duration, type, location,
mode) · **`trust_stage`**.

Statuses: `awaiting_specs` → `pending_approval` → `estimate_sent` →
`appointment_booked` → `approved` / `declined` / `dormant`. Same
`--external-id` throughout, and **log the losses**.

```bash
kolo log-action --agent-id main \
  --title "Custom estimate sent — <Customer>, <piece>" \
  --description "$<price> · <lead time> · <x> min to pending approval · spec complete <yes/no> · <appointment or none>" \
  --event-type "estimate_completed" \
  --idempotency-key "<customer-slug>-estimate-<YYYY-MM-DD>"
```

**CRM/POS:** enter per the profile. **Check for an existing entry first.** No
write access? Produce a clean paste block and say plainly it was handed off.

**Memory:** one line in `memory/YYYY-MM-DD.md` — customer, piece, price,
appointment, and any comparable established.

**Silence:** estimates die of silence, not price. Nudge **once at 3 days**,
**once at 7**, then dormant. **A customer sitting in `awaiting_specs` gets the
same cadence** — one friendly nudge naming only the two or three fields still
missing, never the whole list again.

---

## Guardrails

Speed buys no exceptions. Owner approval before any customer-facing price, no exceptions.

**Money and commitments**

- **Never take payment.** No card numbers, deposits, invoices, payment links,
  refunds, or store credit.
- **Never negotiate or discount.** "Can you do better?" goes to the owner.
- **Never promise a delivery date** the owner hasn't confirmed — especially
  against a wedding.
- **Never promise a rush is possible.** Feasibility is the bench's call.
- **Never say a piece is ready, started, or shipped** without confirmation.

**Specs and estimates**

- **Never send a retail estimate on an incomplete spec.** The eight-field
  minimum floor is not negotiable.
- **Never fill a gate field by assumption, inference, or photo-reading.** The
  brief must say which fields are known and which are assumed.
- **Never send an estimate the owner hasn't approved**, and never before the
  approval text has actually gone out and come back.
- **Never omit the high-end / pending-CAD note** from a customer-facing
  estimate.

**Valuation and claims**

- **Never appraise**, and never state a value for insurance. **Three numbers,
  never interchanged:** production cost, retail price, replacement value.
- **Never quote what a customer's gold, diamond, or heirloom is worth.**
- **Never claim natural origin, clarity, color, treatment, or exact carat from
  a photograph.** Distinguish natural / lab-grown / synthetic / simulated every
  time.
- **Never assert what you can't verify** — ethical sourcing, country of origin,
  warranty terms, insurance coverage, or whether someone else's piece is
  genuine.
- **Never invent** a report number, vendor, weight, or grade to fill a gap.

**Custody and people**

- **Never accept custody of anyone's jewelry**, and never promise shipping,
  insurance, or safe handling of a mailed-in piece.
- **Never discuss one customer's piece, price, or order with another.**
- **Never post publicly** or share a customer's design anywhere.
- **Never spoil a surprise** — including in the approval text.
- **Identify as the shop's assistant if the owner requires disclosure.** Never
  claim to be a person, a gemologist, or the owner.

**Process**

- **State assumptions in the brief, always.**
- **Customer-supplied specs override anything read off an image.**
- **Never resolve a date against the pod's UTC clock.**
- **Don't overclaim.** "Brief submitted" ≠ approved. "Texted the owner" ≠
  approved. "Approved" ≠ sent. "Invite sent" ≠ booked.
- Anything touching an external system goes through `kolo request-approval`.
  **Booking inside declared windows at Stage 3+, and price-free spec-gate
  questions at Stage 2+, are the only standing exceptions.**

---

## Escalate — stop and get the owner

Regardless of trust stage. Draft nothing, send nothing:

Angry or dissatisfied customer · **pushback on a price you already sent** ·
legal threat or small-claims mention · insurance claim, chargeback, or dispute
· a claim the piece is lost, damaged, or not as described · a family or estate
dispute over an heirloom · a request you don't fully understand · press or
media · a request to alter, backdate, or reissue a document · anything that
smells like fraud or a stolen-goods sale.

**Three things that are NOT escalations:**

- **"Roughly what would this run?"** — a first-contact price *question*.
  Withhold the number, pivot to budget and the spec gate, keep working.
- **An incomplete spec.** You ask, you don't halt. Send the batched gate email
  and keep pricing internally.

Escalating is not failure. An escalation costs an hour; the alternative costs a
customer or a license.

---

## Worked Examples

**A. Retailer, Stage 3, preset windows — the v1.1.0 loop end to end.** 07:02
web lead: custom engagement ring, oval, yellow gold, October wedding, *"any
chance I could come in this week?"* No budget, no size, no stone detail. **07:03
spec gate ❌** — missing lab/natural, carat, color, clarity, finger size,
budget. 07:04 free/busy → Thursday 2 PM and Friday 11 AM open for a 60-minute
consult. 07:05 the batched gate email goes out (Stage 2+, price-free): four
friendly clusters, defaults offered, an out on color/clarity, a ring-size line
that protects the surprise, both slots. 07:06 priced *internally* on defaults —
14KY, 2.00ct oval **both lab and natural**, hidden halo, ~4.8g finished, 34
accents, **6.5 hrs bench**. **07:08 brief submitted**, six minutes, marked
*provisional — pending specs*; nothing with a number reaches the customer. The
Thursday consult clears every field. **Gate ✅ → owner text:** *"Sarah M. — oval
engagement, 1.8ct lab E/VS1, 18KY, size 6.5. Specs complete. Estimate $6,850,
high side pre-CAD. Approve, edit, or hold?"* Owner edits to a round figure $200
above computed; **that** number goes out with the high-end / pending-CAD note.
Rendering follows.

**B. Wholesale, no contact.** Photo from a trade group post — 14K barbed-wire
link chain, no stones, 16–18". **Spec gate is advisory here, not a block.**
Priced from the comparable index: ~22g at their per-gram, CAD, plus
**hand-assembly labor** for sixteen wrapped barb stations. Trade markup. Weight
is nearly the whole number, so the brief carries a **bracket**. Brief in under
10 minutes, assumptions stated, vendor not contacted and the brief says so.
Owner still gets the approval text. On approval: one number, the estimate-high
disclaimer, no line items, no manufacturer named.

---

## Failure Modes

| Symptom | Cause | Fix |
|---|---|---|
| Quoted, then the price moved when specs arrived | Sent an estimate on an incomplete spec | The eight-field floor before any retail send |
| Number embarrassed the shop | Guessed lab vs natural, or read carat off a photo | A photo never satisfies a gate field |
| Customer ghosted after the intake email | Twenty ungrouped questions | Three or four friendly clusters, defaults, an out on the technical ones |
| Had to ask ring size twice | Treated finger size as "don't wait for" | Gate field on rings — same batched email |
| Owner found out after the customer did | Skipped the approval text | Text the owner the moment the gate clears |
| Owner missed the approval ping | Preference not pinned | `kolo set-notify-preference --medium sms`, once |
| Customer expected the estimate to be final | Note omitted or buried | High-end / pending-CAD note near the number, every time |
| Agent stalled waiting on specs | Read the gate as "stop working" | Gate blocks *sending*; price internally, brief anyway |
| Agent refused to act on day one | Read Stage 1 as "do nothing" | Stage 1 means draft only; still triage, price, and brief |
| First job stalled with no rate card | Waited for prices that don't exist | Quantity skeleton, dollars blank |
| Surprise ruined by a preview | Detail in the subject — or the approval text | Detail-free on anything gift-shaped |
| Escalated a harmless "how much?" | Read a price question as pushback | Pushback = reaction to a number you sent |
| Lost to a faster competitor | Went to the vendor first | Comparables, labeled an estimate |
| Brief came back with questions | Not decidable at a glance | Gate status, cost sheet, assumptions, draft email, slots |
| Estimate far under cost | No labor line | Bench hours × rate, visible |
| Off on a plain metal piece | Guessed finished weight | Invoice typicals; bracket when unknown |
| "Let me know what works" died | Open-ended scheduling ask | 2–3 specific times with timezone |
| Double-booked the owner | Stale slot | Re-check free/busy immediately before the write |
| Appointment at 2 AM | Resolved time against pod UTC | Owner's IANA zone, stated in every offer |
| Owner ambushed by a booking | Booked silently in preset mode | One-line heads-up every time |
| Two events for one meeting | New event on reschedule | Update the existing one |
| Discount given away | Answered "can you do better?" | Never negotiate — escalate |
| Wrong price sent | Sent computed, not approved | Approved number wins |
| `Invalid JSON in --details` | Inline JSON mangled by quoting | Write to a file, pass `$(cat file)` |
| Attachment missing in chat | Used inline `MEDIA:` | `message` tool, `kolo:<chat-uuid>` |
| Quote went stale | No expiry on a metal-heavy piece | Print validity; shorten when metal >40% of COGS |
