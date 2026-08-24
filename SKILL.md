---
name: "jewelry-estimate-desk-testing"
description: "Custom-jewelry inquiry → spec → estimate → owner text approval → send. v1.3: inbox polling cron + Phase 4 call-to-action close."
tags: [jewelry, estimating, quoting, sales, custom-jewelry, retail, wholesale, scheduling, crm, inbox-monitoring]
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

**Required Kolo model:** run this skill through a dedicated agent pinned to
`litellm-fireworks/qwen-3-7-plus` (Alibaba Qwen 3.7 Plus), with no fallback
model. Inbox-monitoring cron jobs must use the same `--model` override. If Kolo
cannot verify that model for the session, stop and route the work to the pinned
agent; never silently substitute another model.

```
SHOP PROFILE READY? ──► no ──► STOP. Offer Phase 0 setup.
      │                        Do not process inquiries until
      │                        the profile exists and has ready: true.
      yes
      │
      ▼
INBOX POLLING ACTIVE? ──► no ──► Offer to set up (Phase 0 question 7).
      │                          Process manually for now.
      yes
      │
      ▼
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
                  + call-to-action close with availability
```

---

## Phase 0 — Shop Setup Gate

**This is a hard gate. Do not process any inquiry, draft any email, or read
any customer message until the shop profile exists and has `ready: true`.**

A profile created mid-inquiry forces the owner to answer setup questions while
a customer waits. That is the wrong time. The skill must be configured before
the first email lands.

### First-run detection

1. Check `estimate-desk/shop-profile.md` (workspace root).
2. **Exists with `ready: true`** → proceed to Phase 1.
3. **Missing, or exists without `ready: true`** → stop. Offer setup:

> "I see the jewelry estimate desk hasn't been set up yet. Let's fix that
> now — 90 seconds and you'll never answer these questions mid-inquiry. Ready?"

### Setup questions

Ask in order, write answers immediately. Use `templates/shop-profile.md`.

1. **Shop name, your email, your name, location** — for the signature block.
2. **Retailer, wholesale middle man, or both?**
3. **Markup** — percentage over cost, or multiple (e.g. 2.5× on COGS).
4. **Who approves prices** + preferred approval medium (`kolo set-notify-preference --show`).
5. **Booking** — preset windows or ask-each-time. If preset: day-of-week windows, blackouts, minimum notice, durations by meeting type, timezone.
6. **Trust stage** — confirm Stage 1 (drafts only, nothing sent without approval).
7. **Business hours for inbox monitoring** ★ NEW in v1.3.0 — what hours should the agent poll for new inquiries? Default: Mon–Fri 9am–5pm in the shop's timezone, or whatever the owner specifies. "All day" = hourly round the clock. "None" = no automatic polling, owner prompts manually. Also capture: monitoring timezone (usually same as booking timezone).

### Signature confirmation

Build the signature block from answers 1–6 and confirm. Then write the profile with `ready: true`.

### The Upgrade (async)

Send past invoices to build the rate card from real numbers.

---

## Inbox Monitoring ★ NEW in v1.3.0

**The agent watches the inbox so the owner doesn't have to.**

Once the profile has `ready: true` and business hours are set (question 7), the
agent creates a cron job that polls Gmail for new jewelry inquiries. The owner
gets notified, not surprised.

### Setup

```bash
# 1) Get the delivery target for the owner's chat
#    sessions_list → deliveryContext.to → kolo:<uuid>

# 2) Create the cron
openclaw cron add \
  --name "jewelry-inbox-watch" \
  --cron "0 9-17 * * 1-5" \
  --tz "<owner timezone>" \
  --model "litellm-fireworks/qwen-3-7-plus" \
  --session isolated \
  --announce \
  --channel kolo \
  --to "kolo:<uuid>" \
  --best-effort-deliver \
  --message "Check Gmail for unread jewelry-related inquiries (subject keywords: estimate, quote, ring, pendant, bracelet, necklace, earrings, custom, engagement, wedding, band, chain, repair, CAD, rendering). If found: notify the owner with a one-line summary per inquiry, then run Phase 1 triage on each. If none found, NO_REPLY."
```

### Rules

- **Frequency:** hourly during business hours (configurable; use `--cron` with the owner's `--tz`). The owner can say "every 30 minutes" or "Mon–Sat 8–8" — adjust the cron expression accordingly.
- **Scope:** searches subject + sender for jewelry keywords. Does not poll non-jewelry email or read unrelated threads.
- **On hit:** one-line ping to owner per new inquiry ("New estimate request from Sarah M. — engagement ring. Processing now."), then Phase 1 triage begins immediately.
- **On existing threads:** detects replies on open estimates. Customer replied → re-run the spec gate, update the brief, ping the owner.
- **No hits:** silent. No NO_REPLY noise.
- **If the owner says "pause monitoring"** → disable the cron; re-enable on request.
- **If business hours weren't set in Phase 0** → the agent processes manually when prompted. Offer to set up monitoring on the next interaction.

### What the owner sees

Instead of *"Hey Kolo, check for estimate emails"* every time, the owner gets:
> "New estimate request from Sarah M. — oval diamond engagement ring. Spec gate running now. Brief in your chat in ~5 minutes."

The skill runs proactively; the owner only sees the brief.

---

## Before the First Inquiry — Proactive Triggers

**The agent offers setup before any inquiry is processed.** Three triggers:

1. **Jewelry keywords** in conversation + no profile → offer setup first.
2. **Skill just installed** → missing profile is the trigger. First jewelry-related message, offer setup.
3. **Owner asks about the skill's capabilities** → answer, then offer setup.
4. **Business hours not set** ★ v1.3.0 → after profile is written with `ready: true`, if question 7 wasn't answered, offer inbox monitoring setup.

**What NOT to do:** wait for an inquiry to discover the missing profile.

---

## Trust Ladder

| Stage | Agent may do on its own | Still needs the owner |
|---|---|---|
| **1 — Watch me** *(default)* | Nothing outbound. Draft everything, send nothing. | Every email, price, and booking |
| **2 — Ask questions** | Send price-free info requests, **including the spec-gate ask** | Every price and booking |
| **3 — Book me** | + Book/reschedule inside declared windows | Every price |

**Never advance a stage on your own.** Missing or unreadable stage → assume Stage 1.

---

## Phase 1 — Triage & Spec

| Request | Route |
|---|---|
| New custom piece, replica, redesign | Continue here |
| Repair / resize / restring | Repair intake — no rendering |
| Appraisal or insurance valuation | A valuation is not an estimate |
| Job status check | Look it up, answer, done |
| Price on existing inventory | Sales question |
| Wants to come in / hop on a call | Phase 3c — book it, then continue |
| Angry, legal, insurance, chargeback, media | Escalate. Draft nothing |

Read the entire message body. Pull: piece type and quantity · metal/karat/color · stones (lab or natural, type, shape, carat, quality, count) · size · setting and style · engraving and finish · event date · budget · reference images · customer-supplied stones or heirloom metal · certificate · scheduling intent.

**A photo never satisfies a gate field.** "Looks like ~1.5ct" is an assumption.

**Surprise check:** reply only on the channel used, keep subject detail-free, never contact shared addresses.

---

## Phase 1.5 — The Spec Completeness Gate

Fill the checklist from the message, attachments, CRM, and the customer's own words — **never from a guess or a photo**.

| Field | Applies to |
|---|---|
| Piece type & quantity | all |
| Stone type | with stones |
| Lab or natural | with stones |
| Carat / ctw (center vs total) | with stones |
| Color (grade band) | with stones |
| Clarity | with stones |
| Cut / shape | with stones |
| Metal & karat | all |
| Metal color | all |
| Finger size | rings (required) |
| Length / dimensions | chains, bracelets, pendants |
| Setting & style | all |
| Engraving / finish | all |
| Event date | all |
| Budget | retail |
| Customer-supplied stone/metal | all |

**Minimum floor (retail):** stone type, lab vs natural, color, clarity, cut, carat, metal + karat + color, and finger size on a ring. Those eight are not negotiable. Wholesale is exempt — state assumptions.

**How to ask:** one batched email, 3–4 friendly clusters, never re-ask what's known, offer defaults, give an out on technical fields, surprise-safe ring size line, no prices, close with two real open slots.

**Partial reply:** ask once more for only load-bearing missing fields, then owner decides.

---

## Phase 2 — Price It Now

Don't wait for the customer to price internally. Gate governs **sending**, not calculating.

| Line | Basis |
|---|---|
| Metal | finished grams × $/g |
| Center stone | carat × $/ct |
| Accent stones | total carat × $/ct |
| CAD · casting | per profile |
| Bench labor | estimated hours × $/hr (mandatory, never buried) |
| Setting · finishing · engraving | per profile |
| COGS | sum |
| Quote | COGS × markup, rounded clean |

**Estimate on the high end, deliberately.** Price the top of the plausible range.

**First job with no rate card:** quantity skeleton (quantities filled, dollars blank) + the Quick Start questions.

**Price both columns where a real choice exists** (lab vs natural) and recommend one.

Use the shop's dated rate card and invoice-derived comparable jobs before
market defaults. Finished weight is often the largest error source; if it is
genuinely unknown, show the owner a bracket instead of false precision. Keep
production cost, retail price, and replacement value separate—this workflow
never supplies an appraisal. Print a validity date and shorten it when metal is
a large or volatile share of the quote.

---

## Phase 3 — One Brief

Everything decidable at a glance: gate status · number + recommendation · customer + channel + piece · cost sheet · assumptions · draft customer email · event date feasibility · appointment slots · unknowns and whether they move the price.

```bash
kolo request-approval \
  --agent-id main \
  --action "Custom estimate — <Customer> — <piece> — $<quote>" \
  --reasoning "<spec gate status, weights, stone spec, labor hours, option recommended, assumptions>" \
  --risk-level medium \
  --details "$(cat /tmp/brief-details.json)" \
  --session-key "<current session key>"
```

Write --details JSON to a file, pass `$(cat …)`. Inline JSON breaks on shell quoting. Also message the chat — the CLI call is not a notification.

Keep the computed quote and the owner-approved price separate. The number that
goes to the customer is always the one the owner approved, even when it differs
from the formula.

### Phase 3a — Text the owner

The moment the spec gate clears and the estimate exists. Also when customer goes quiet or can't answer a gate field.

```bash
kolo set-notify-preference --show
kolo notify-owner -m "<short, decidable from a lock screen>"
```

Confirm the owner's pinned medium. If SMS is unavailable and Kolo falls back to
chat, say so in the brief rather than claiming a text was sent. Surprise rule
applies to texts too. Shared phone → omit piece type. Then wait—never send off
your own math.

---

## Phase 3b — The customer email

One batched email. Always ask lab vs natural. Budget is design guidance. Confirm photo read in one sentence. No prices without approval. Close with two real open slots. Stage 2+ auto-sends the price-free gate email.

---

## Phase 3c — Scheduling

Fires on meeting intent at any point. Never waits on the estimate.

**Access:** `kolo integration-routing` → use exactly the returned path.

**Two modes:** preset windows (Stage 3+, standing authorization) or ask-each-time.

**Offering:** live free/busy → intersect with windows → 2–3 specific times with timezone and duration.

**Booking:** re-check free/busy immediately before writing → create event → confirm to customer with date/time/timezone/duration/place → one-line heads-up to owner.

Reschedule by updating the existing event, not creating a duplicate.
Cancellation removes the event but keeps the estimate alive. A no-show gets
one friendly re-offer. A declared window is permission, not availability; the
live calendar always wins.

Timezone is critical. Pod clock is UTC—resolve everything against the owner's
IANA zone. If it is missing, use the stored owner timezone only for internal
date math and confirm it before stating it to a customer. Never guess.

---

## Phase 4 — After Approval

1. **Send the approved quote** — one all-in number, with the high-end / pending-CAD note.

2. **Close with a call-to-action** ★ NEW in v1.3.0. Every approved estimate email ends with a specific invitation to talk, paired with real availability:

> "I'm happy to jump on a call to discuss more and dial in a tighter estimate — I'm available [X], [Y], or [Z]. Let me know what works best."

Fill X, Y, Z from live free/busy (or preset windows). Never "let me know what works" — always 2–3 specific times with timezone. The point: the quote is a conversation starter, not a take-it-or-leave-it number.

3. **Generate the rendering** — `image_generate`, count 2, aspect 4:3. Illustration, not a shop drawing.

### The high-end / pending-CAD note

Every approved estimate carries this near the number:

> "Just so you know how to read this: we estimate on the high end on purpose. This figure is pending CAD and rendering approval, and once your design is finalized the final price is often a little lower — if it comes in under, we pass that straight along to you. Nothing is locked in until you've seen and approved the CAD."

Adapt the wording, never the substance: high-end estimate, pending CAD, savings passed along, nothing committed until CAD approval.

**Non-negotiables:** one number, no line items or vendor names, restate the priced spec in one line, lead time is an estimate not a promise, attach via `message` tool.

---

## Phase 5 — Close the Loop

```bash
kolo record-upsert \
  --record-type "skill.jewelry_estimate" \
  --external-id "<customer-slug>-<job-number-or-date>" \
  --payload "$(cat /tmp/estimate-record.json)" \
  --status "estimate_sent"
```

Statuses: `awaiting_specs` → `pending_approval` → `estimate_sent` → `appointment_booked` → `approved` / `declined` / `dormant`.

The record retains the inbound timestamp; spec and missing fields; assumptions;
cost lines, COGS, markup, computed quote, and owner-approved price; notification
time and medium; lead-time feasibility; rendering paths; appointment event ID,
timezone, duration, type, location, and booking mode; trust stage; and next
action date. Check for an existing record before writing so retries update it
instead of creating a duplicate.

```bash
kolo log-action --agent-id main \
  --title "Custom estimate sent — <Customer>, <piece>" \
  --description "$<price> · <lead time> · <x> min to pending approval · spec complete <yes/no> · <appointment or none>" \
  --event-type "estimate_completed" \
  --idempotency-key "<customer-slug>-estimate-<YYYY-MM-DD>"
```

**Memory:** one line in `memory/YYYY-MM-DD.md`. **Nudge cadence:** once at 3 days, once at 7, then dormant. `awaiting_specs` nudges name only 2–3 missing fields.

---

## Guardrails

**Money and commitments:** never take payment, never negotiate/discount, never promise unconfirmed delivery dates or rush feasibility, never say a piece is ready/started/shipped without confirmation.

**Specs and estimates:** never send retail estimate on incomplete spec (eight-field floor), never fill gate field by assumption or photo, never send unapproved estimate, never omit the high-end/CAD note, never omit the call-to-action close ★ v1.3.0.

**Valuation and claims:** never appraise, never quote heirloom/gold/diamond value, never claim origin/clarity/grade from a photo, never invent report numbers/vendors/weights.

**Custody and people:** never accept custody of jewelry, never discuss one customer with another, never spoil a surprise, identify as shop's assistant if required.

**Process:** state assumptions always, customer-supplied specs override photo reads, never resolve dates against pod UTC, don't overclaim.

**Standing exceptions:** booking inside declared windows at Stage 3+. Price-free spec-gate asks at Stage 2+. Inbox polling at any stage once configured ★ v1.3.0.

---

## Escalate — stop and get the owner

Angry customer · pushback on a sent price · legal threat · insurance claim/chargeback · lost/damaged claim · estate/heirloom dispute · unclear request · press/media · document alteration/backdating · fraud/stolen-goods.

**NOT escalations:** first-contact price question (pivot to budget), incomplete spec (ask, don't halt), missing shop profile (offer setup).

---

## Worked Examples

**A. Retailer, Stage 3, preset windows.** Web lead: custom engagement ring, oval, yellow gold, October wedding. No budget, no size, no stone detail. Spec gate ❌. Free/busy → Thursday 2 PM and Friday 11 AM open. Batched gate email goes out (Stage 2+ auto-send). Priced internally — both lab and natural, 6.5 hrs bench. Brief in 6 minutes, provisional. Thursday consult clears every field. Owner approves edited number. Quote sent with high-end note + call-to-action close. Rendering follows.

**B. Wholesale, no contact.** Photo from trade group — 14K barbed-wire chain link, 16–18". Spec gate advisory. Priced from comparable index with hand-assembly labor. Bracket quoted. Brief in under 10. Owner still gets the approval text. One number, estimate-high disclaimer, no line items, no manufacturer named.

**C. First inquiry arrives via inbox monitoring ★ v1.3.0.** Cron fires at 10am PT. Gmail search finds unread email from Sarah M. — "custom engagement ring estimate." Agent pings owner: "New estimate request from Sarah M. — oval diamond engagement ring. Processing now." Profile is ready, Stage 2. Agent triages, runs spec gate (❌ — missing carat, color, clarity, size, budget), prices internally with stated assumptions, drafts the batched spec-gate email, submits the brief. Owner sees the brief in chat ~5 minutes after the original ping — never had to say "check email."

---

## Failure Modes

| Symptom | Cause | Fix |
|---|---|---|
| Owner answered setup questions while customer waited | Profile created reactively mid-inquiry | Offer Phase 0 setup proactively |
| Agent processed inquiry without shop signature | Phase 0 skipped | Phase 0 is a hard gate; check `ready: true` |
| Quoted, then price moved when specs arrived | Sent estimate on incomplete spec | Eight-field floor before any retail send |
| Number embarrassed the shop | Guessed lab vs natural or read carat off photo | A photo never satisfies a gate field |
| Customer ghosted after intake | Twenty ungrouped questions | 3–4 friendly clusters with defaults |
| Owner found out after customer did | Skipped the approval text | Text owner the moment gate clears |
| Owner missed approval ping | Preference not pinned | `kolo set-notify-preference --medium sms` |
| Customer expected final price | Note omitted or buried | High-end/CAD note near the number |
| Agent stalled waiting on specs | Read gate as "stop working" | Gate blocks sending; price internally, brief anyway |
| Agent refused to act on day one | Read Stage 1 as "do nothing" | Stage 1 = draft only; still triage, price, brief |
| First job stalled with no rate card | Waited for prices that don't exist | Quantity skeleton, dollars blank |
| Surprise ruined | Detail in subject or approval text | Detail-free on anything gift-shaped |
| Escalated harmless "how much?" | Read price question as pushback | Pushback = reaction to a sent number |
| Lost to faster competitor | Went to vendor first | Comparables, labeled an estimate |
| Brief came back with questions | Not decidable at a glance | Gate status, cost sheet, assumptions, draft, slots |
| Estimate far under cost | No labor line | Bench hours × rate, visible |
| Off on plain metal piece | Guessed finished weight | Invoice typicals; bracket when unknown |
| "Let me know what works" died | Open-ended scheduling | 2–3 specific times with timezone |
| Double-booked the owner | Stale slot | Re-check free/busy immediately before write |
| Appointment at 2 AM | Resolved against pod UTC | Owner's IANA zone, stated in every offer |
| Owner ambushed by booking | Booked silently | One-line heads-up every time |
| Discount given away | Answered "can you do better?" | Never negotiate — escalate |
| Wrong price sent | Sent computed, not approved | Approved number wins |
| Invalid JSON in --details | Inline JSON mangled | Write to file, pass `$(cat file)` |
| Attachment missing in chat | Used inline `MEDIA:` | `message` tool, `kolo:<chat-uuid>` |
| Quote went stale | No expiry on metal-heavy piece | Print validity; shorten when metal >40% of COGS |
| Owner had to prompt "check email" every time ★ v1.3.0 | No inbox monitoring | Set up hourly cron during business hours |
| Quote went out as dead-end number ★ v1.3.0 | No call-to-action close | Close every approved estimate with specific availability |
