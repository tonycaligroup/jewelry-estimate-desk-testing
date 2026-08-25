---
name: "jewelry-estimate-desk-testing"
description: "Jewelry estimate desk — inbound custom jewelry inquiry → approved quote in minutes, without exposing a number to a customer before owner review."
tags: [jewelry, estimating, quoting, sales, custom-jewelry, retail, wholesale, scheduling, crm, inbox-monitoring]
---

# Jewelry Estimate Desk

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
5. **Never interpolate untrusted text into shell commands.** ★ v1.8.0. All
   customer names, emails, piece descriptions, budgets, and any other text
   sourced from an inquiry go through JSON files or stdin — never through
   shell string interpolation. Shell commands use fixed template strings with
   agent-written identifiers only; all variable data lives in `--details` JSON
   or `--payload` JSON files.

**Required Kolo model:** run this skill through a dedicated agent pinned to
`litellm-fireworks/qwen-3-7-plus` (Alibaba Qwen 3.7 Plus), with no fallback
model. Inbox-monitoring cron jobs must use the same `--model` override. If Kolo
cannot verify that model for the session, stop and route the work to the pinned
agent; never silently substitute another model.

## Conventions

- **customer-slug:** `firstname-lastname`, lowercase, hyphens for spaces
  (e.g. `sarah-mitchell`). Use in record IDs, rendering project names,
  idempotency keys, and file paths.
- **job-id:** short piece descriptor, kebab-case (e.g. `oval-engagement-ring`).
  Full identifier: `<customer-slug>-<job-id>`.
- **Per-job temp files:** ★ v1.8.0. Every job gets uniquely-named temp files:
  `/tmp/jewelry-<slug>-<job>-brief.json`,
  `/tmp/jewelry-<slug>-<job>-record.json`,
  `/tmp/jewelry-<slug>-<job>-log.json`,
  `/tmp/jewelry-<slug>-<job>-notify.txt`.
  Delete all after Phase 5 completes.
- **Session key:** use `sessions_list` tool, read `sessionKey`.
- **Calendar & Gmail:** `kolo integration-routing` → when path is `maton`,
  read **api-gateway** skill at `/opt/kolo-skills/api-gateway/SKILL.md`.
- **Record access:** use `kolo record-get` to READ. Never use `kolo record-status`
  — it is write-only. `record.status` (top-level) ≠ `payload.status` (JSON field).
- **Shell safety ★ v1.8.0:** Never embed customer names, emails, piece
  descriptions, or budgets directly into shell commands. All untrusted data
  goes through JSON files passed via `$(cat <file>)`. Use only agent-generated
  identifiers (slugs, job-ids) in `--action`, `--title`, `--reasoning`.

```
SHOP PROFILE READY? ──► no ──► STOP. Offer Phase 0 setup.
      │                        Do not process inquiries until
      │                        the profile passes validation.
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
Phase 1.5  SPEC GATE
      ├─ no  → ONE friendly batched ask, price internally anyway, wait
      └─ yes → Phase 2 price it
           ↓
Phase 3  brief + TEXT THE OWNER  ◄── customer send blocked here
           ↓
owner reviews / edits / approves
           ↓
Phase 4  send approved number + high-end / pending-CAD note
         + call-to-action close with availability
```

---

## Phase 0 — Shop Setup Gate

**Hard gate. Do not process any inquiry until the shop profile passes validation.**

### Required fields — validated before `ready: true` ★ HARDENED v1.8.0

A profile with only `ready: true` is rejected. Verify before setting:

| Field | Section | Check |
|---|---|---|
| Mode | Basics | `retailer`, `wholesale middle man`, or `both` |
| Markup multiplier | Rate card | Number > 1.0 (e.g. `2.2`). NOT a bare percentage. |
| Approver email | Basics | Must contain `@` |
| Outbound mailbox | Basics | Must contain `@` |
| Trust stage | Autonomy | `1`, `2`, or `3` |
| Timezone | Scheduling | Valid IANA (e.g. `America/Los_Angeles`) |

If any required field is missing or invalid: stop, tell the owner exactly which
fields, set `ready: false` until fixed.

### Markup normalization ★ FIXED v1.8.0

**One field:** `markup_multiplier` — multiply COGS by this.
- `2.2` = 2.2× COGS (120% markup)
- `1.25` = 1.25× COGS (25% markup)
- `1.0` = illegal (at-cost, rule 3)
- If owner types "25%", convert to `1.25` and confirm: "I'm reading your
  markup as 1.25× COGS — $1,000 cost → $1,250. Right?"

Phase 2 always: `quote = COGS × markup_multiplier`, rounded clean.

### First-run detection

1. Check `estimate-desk/shop-profile.md` (workspace root).
2. **Exists AND all 6 required fields valid AND `ready: true`** → proceed.
3. **Missing, invalid, or no `ready: true`** → stop. Offer setup.

### Setup questions

Use `templates/shop-profile.md`. Ask in order, write answers immediately.
1. Shop name, email, name, location.
2. Retailer, wholesale, or both?
3. Markup multiplier (convert % to multiplier, confirm).
4. Who approves + preferred medium (`kolo set-notify-preference --show`).
5. Booking — preset windows or ask-each-time.
6. Trust stage — confirm Stage 1.
7. Business hours for inbox monitoring (default Mon–Fri 9am–5pm).

### Validation + signature

Build signature. Validate 6 required fields. Only write `ready: true` at the
end after all validation passes.

---

## Inbox Monitoring ★ HARDENED v1.8.0

### Message deduplication ★ NEW v1.8.0

Before processing any email, check its Gmail message ID:

1. `kolo record-get --record-type "skill.jewelry_inbox_processed" --external-id "<gmail-message-id>"`
2. Exists → skip (already processed). NO_REPLY.
3. Not found (404) → new. Process. After triage:
   ```bash
   kolo record-upsert \
     --record-type "skill.jewelry_inbox_processed" \
     --external-id "<gmail-message-id>" \
     --payload '{"thread_id":"<tid>","processed_at":"<ISO>","customer_slug":"<slug>"}' \
     --status "processed"
   ```

Use the immutable Gmail API `id` field, NOT `threadId`.

### Setup

```bash
# sessions_list → deliveryContext.to → kolo:<uuid>
openclaw cron add \
  --name "jewelry-inbox-watch" \
  --cron "0 9-17 * * 1-5" \
  --tz "<owner timezone>" \
  --model "litellm-fireworks/qwen-3-7-plus" \
  --fallbacks "" \
  --session isolated \
  --announce \
  --channel kolo \
  --to "kolo:<uuid>" \
  --best-effort-deliver \
  --message "Read the api-gateway skill, then use the Maton gateway to search Gmail for unread jewelry-related inquiries. Search both subject AND body for keywords: estimate, quote, ring, pendant, bracelet, necklace, earrings, custom, engagement, wedding, band, chain, repair, CAD, rendering. For EACH matching message: (1) extract Gmail message ID, (2) run 'kolo record-get --record-type skill.jewelry_inbox_processed --external-id <message-id>' to check if processed, (3) if NOT processed: notify owner with 'New estimate request — processing now.' (surprise-safe), run Phase 1 triage, then record the message ID. If no new inquiries, NO_REPLY."
```

**Rules:** hourly during business hours (use `--cron` with `--tz`); subject AND
body; deduplicate on message ID; surprise-safe pings; silent on no hits;
`--fallbacks ""` always.

---

## Trust Ladder

| Stage | Agent may do | Still needs owner |
|---|---|---|
| 1 — Watch me *(default)* | Draft only, send nothing | Every email, price, booking |
| 2 — Ask questions | Price-free info requests | Every price and booking |
| 3 — Book me | + Book/reschedule in windows | Every price |

Never advance stage on your own. Missing/unreadable → Stage 1.

---

## Phase 1 — Triage & Spec

| Request | Route |
|---|---|
| New custom piece, replica, redesign | Continue |
| Repair / resize / restring | Repair intake |
| Appraisal / insurance | Not an estimate |
| Job status check | Look up, done |
| Existing inventory price | Sales question |
| Wants to come in / call | Phase 3c |
| Angry, legal, insurance, chargeback, media | Escalate |

Read entire body. Pull: piece type · metal/karat/color · stones (lab/natural,
type, shape, carat, quality, count) · size · setting · engraving · finish ·
event date · budget · reference images · customer-supplied stone/metal ·
certificate · scheduling intent.

**A photo never satisfies a gate field.** Surprise-safe channel only.

---

## Phase 1.5 — Spec Completeness Gate

Fill from message, attachments, CRM, customer words — never guess or photo.

**Minimum floor (retail) — 8 items, non-negotiable:**
1. Stone type; 2. Lab/natural; 3. Color; 4. Clarity; 5. Cut; 6. Carat;
7. Metal + karat + color (all three, one slot); 8. Finger size on a ring.

Wholesale exempt. Ask: one batched email, 3–4 friendly clusters, never re-ask
known, offer defaults, surprise-safe ring size line, no prices, close with two
real open slots.

---

## Phase 2 — Price It

Gate governs sending, not calculating. Price internally regardless.

| Line | Basis |
|---|---|
| Metal | finished grams × $/g |
| Center stone | carat × $/ct |
| Accent stones | total carat × $/ct |
| CAD · casting | per profile |
| Bench labor | hours × $/hr (mandatory) |
| Setting · finishing | per profile |
| COGS | sum |
| Quote | COGS × markup_multiplier ★ v1.8.0 |

**Markup always a multiplier:** `2.2` → $1,000→$2,200; `1.25`→$1,250; `1.0` illegal.

Estimate high end deliberately. First job: quantity skeleton from Typical
weights + example hours. Price both columns (lab vs natural). Bracket when
finished weight unknown.

---

## Phase 3 — One Brief

### Step 1: session key
`sessions_list` → `sessionKey`.

### Step 2: brief JSON
Write to `/tmp/jewelry-<slug>-<job>-brief.json`. Required: `customer`, `piece`,
`spec_gate`, `stone`, `metal`, `pricing` (with `markup_multiplier`), `labor`,
`assumptions`, `finished_weight_g`, `feasibility`, `appointment`,
`recommendation`, `draft_email`.

### Step 3: Submit ★ HARDENED v1.8.0

```bash
kolo request-approval \
  --agent-id main \
  --action "Custom estimate — <slug>/<job-id>" \
  --reasoning "Spec gate <x>/8, <metal> <piece-type>, <stone-type> <carat>, <hours>hrs" \
  --risk-level medium \
  --details "$(cat /tmp/jewelry-<slug>-<job>-brief.json)" \
  --session-key "<session key>"
```

- `--action`: slug/job-id only. Never customer names.
- `--reasoning`: spec-gate fields and numeric values only. Never raw customer text.
- `--details`: `$(cat <per-job-file>)`, never inline JSON.

If fails: missing session key → retry; invalid JSON → rewrite; other → retry
once, escalate.

### Phase 3a — Text the owner ★ HARDENED v1.8.0

```bash
kolo set-notify-preference --show   # check medium first
```

Write notification to `/tmp/jewelry-<slug>-<job>-notify.txt`:
> "Estimate ready — spec <x>/8, <slug>/<job-id>. Brief #<N> in your chat."

```bash
kolo notify-owner -m "$(cat /tmp/jewelry-<slug>-<job>-notify.txt)"
```

Surprise-safe always. No names, no piece types. Shared phone → omit everything.

### Phase 3b — Customer email
One batched email. Ask lab vs natural. No prices without approval. Close with
two real slots. Stage 2+ auto-sends price-free gate email.

### Phase 3c — Scheduling
`kolo integration-routing` → when `maton`, read api-gateway skill. Two-step:
(1) query free/busy → intersect windows → pick candidates; (2) re-check
specific slots immediately before write; (3) write; (4) confirm. Owner IANA
zone, never pod UTC.

---

## Phase 4 — After Approval

1. **Send approved quote** — one all-in number, high-end/CAD note. Use
   `message` tool with `target: kolo:<chat-uuid>` from `deliveryContext.to`.
2. **Call-to-action close** — 2–3 specific times from live free/busy.
3. **Rendering** — `image_generate`, `action:"generate"`, `count:2`,
   `size:"1536x1024"`, `model:"litellm/gpt-image-2"`. Read
   `references/rendering-standards.md` first. Include per-job spec block:
   PROJECT/TYPE/METAL/CENTER/ACCENT STONES/SETTING/SHANK/PROFILE/FINISH/VIEW/
   REFERENCE IMAGE 1-3/DO NOT CHANGE/REQUESTED CHANGE. Reference images via
   `image` (primary) + `images` (secondary). Initial: sections 1–22. Iterative:
   sections 12, 13, 21 only.

### High-end note
Near the number: estimate on high end, pending CAD, savings passed along,
nothing committed until CAD approval. Canonical text in
`templates/approved-estimate-note.md`. Non-negotiables: one number, no line
items, no vendor names, spec in one line, lead time is estimate, attach via
`message` tool.

---

## Phase 5 — Close the Loop

### 5a. Write record

```bash
kolo record-upsert \
  --record-type "skill.jewelry_estimate" \
  --external-id "<slug>-<job-id>-<YYYY-MM-DD>" \
  --payload "$(cat /tmp/jewelry-<slug>-<job>-record.json)" \
  --status "estimate_sent"
```

Statuses: `awaiting_specs` → `pending_approval` → `estimate_sent` →
`appointment_booked` → `approved`/`declined`/`dormant`.

### 5b. Log ★ HARDENED v1.8.0

Write to `/tmp/jewelry-<slug>-<job>-log.json`, extract with `jq`:

```bash
cat > /tmp/jewelry-<slug>-<job>-log.json << 'JSONEOF'
{"title":"Custom estimate sent — <slug>/<job-id>","description":"$<price> · <lead-time> · spec <x>/8","event_type":"estimate_completed","idempotency_key":"<slug>-estimate-<YYYY-MM-DD>"}
JSONEOF

kolo log-action --agent-id main \
  --title "$(jq -r .title /tmp/jewelry-<slug>-<job>-log.json)" \
  --description "$(jq -r .description /tmp/jewelry-<slug>-<job>-log.json)" \
  --event-type "$(jq -r .event_type /tmp/jewelry-<slug>-<job>-log.json)" \
  --idempotency-key "$(jq -r .idempotency_key /tmp/jewelry-<slug>-<job>-log.json)"
```

Memory: one line in `memory/YYYY-MM-DD.md`. Cleanup ★ v1.8.0:
`rm -f /tmp/jewelry-<slug>-<job>-*.json /tmp/jewelry-<slug>-<job>-notify.txt`

### 5c. Nudge crons ★ v1.7.0

Day-3 and day-7 one-shot crons: explicit `kolo record-get` commands,
timezone offsets in `--at`, `--fallbacks ""`, surprise-safe templates.
See lines 548–590 of the previous revision (unchanged from v1.7.0).

---

## Guardrails

**Money:** never take payment, negotiate, discount, or promise unconfirmed
delivery. **Specs:** retail gate floor, never guess from photo, never send
unapproved, always high-end/CAD note + CTA close. **Valuation:** never appraise,
quote heirloom value, claim grade from photo. **Custody:** never accept jewelry,
never discuss customers with each other, never spoil surprise. **Records:**
`kolo record-get` not `record-status`; `record.status` ≠ `payload.status`.
**Shell ★ v1.8.0:** never interpolate customer text into shell commands.
All inquiry data through JSON files; per-job temp files prevent races.

---

## Escalate — stop and get the owner

Angry customer · price pushback · legal threat · insurance/chargeback ·
lost/damaged claim · estate/heirloom dispute · unclear request · press/media ·
document alteration · fraud/stolen-goods.

**NOT escalations:** first-contact price question, incomplete spec, missing profile.

---

## Worked Examples

**A. Retailer, Stage 3.** Engagement ring, oval, yellow gold. Gate ❌. Priced
lab/natural, 6.5 hrs bench. Brief in 6 min. Consult clears gate. Owner approves.
Quote with high-end note + CTA. Rendering: litellm/gpt-image-2, 1536×1024.
Nudges with PT offset. Notifications surprise-safe. Per-job temp files cleaned.

**B. Wholesale.** Photo from trade group — chain link. Comparables. Bracket.
Brief <10 min.

**C. Inbox monitoring ★ v1.8.0.** Cron fires 10am PT. Finds unread. Message ID
not in processed store → new. Surprise-safe ping. Triage → brief → record
message ID. Brief arrives ~5 min after ping.

**D. First job.** Quantity skeleton from Typical weights + example hours. Ask
owner for $/g, $/ct, $/hr.

---

## Failure Modes (v1.8.0 additions)

| Symptom | Cause | Fix |
|---|---|---|
| Markup as percentage, below-cost ★ v1.8.0 | "25%" entered | Normalize to 1.25; confirm; Phase 2 always `COGS × multiplier` |
| Repeated alerts for same inquiry ★ v1.8.0 | No message ID tracking | Deduplicate on immutable Gmail message ID; `skill.jewelry_inbox_processed` |
| `ready: true` with no config ★ v1.8.0 | No field validation | 6 required fields validated before setting ready |
| Customer text executed as shell code ★ v1.8.0 | Interpolation in shell args | All inquiry text through JSON files; shell flags use agent IDs only |
| Surprise spoiled on lock screen ★ v1.8.0 | Name + piece in notification | All notifications surprise-safe by default |
| Cross-inquiry data leak ★ v1.8.0 | Fixed /tmp filenames | Per-job files: `/tmp/jewelry-<slug>-<job>-<purpose>.json` |

All v1.3.0–v1.7.0 failure modes remain active (nudge record-get, --fallbacks,
two-step booking, timezone offsets, record.status disambiguation, image model,
inbox setup, CTA close, rendering constraints, spec gate floor, trust ladder).
