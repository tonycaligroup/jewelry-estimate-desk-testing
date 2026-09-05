---
name: jewelry-estimate-desk-testing
version: 4.8.4
description: Prepare and route custom-jewelry estimates from inbound customer inquiries through specification intake, owner price approval, customer reply, scheduling, rendering, and follow-up. Use for retail custom-jewelry estimate workflows; do not use for wholesale or trade pricing, appraisals, insurance valuations, payments, disputes, or unapproved outbound prices.
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
3. Show the customer one all-in owner-approved price. Every jeweler cost and
   pricing assumption is confidential owner-only information. Never expose or
   summarize assumptions, COGS, component costs, rates, quantities used only
   for costing, markup, margin, or vendor/manufacturer identity in any
   customer-visible email, estimate, attachment, rendering, calendar event, or
   explanation.
4. Never take payments, cards, deposits, refunds, or payment-link actions.
5. Never interpolate inquiry-derived data into shell commands. Use the bundled
   Python helpers, which invoke the Kolo CLI with argument arrays and no shell.
6. Never send a customer message to `deliveryContext.to` or `kolo:<uuid>`.
   Those destinations are for the owner. Reply through the original customer
   connector, mailbox, recipient, and thread.
7. An estimate originating from email must be a reply to the original inbound
   message, never a new email or thread. If the original Gmail thread ID or RFC
   `Message-ID` is unavailable, stop and recover the original message first.
8. For Gmail, the normalized sender email address is the customer identity.
   Never identify, merge, select, or address a customer by display name. Two
   senders with the same name but different email addresses are different
   customers and must have separate estimates and routes.
9. The calendar is the source of truth for meeting state. Never infer that a
   meeting exists from email content, subject lines, or scheduling language.
   Before claiming a meeting is already scheduled, query the calendar for
   events with that customer's email as an attendee. If no event is found, no
   meeting exists regardless of what any email says.
10. Never use direct `curl`, raw Gmail/Maton send calls, `python -c`, direct
   record/claim JSON edits, or a claim token copied from chat or prior context.
   Customer sends and approval requests must use `scripts/workflow_safe.py`,
   which resolves the token from the authoritative claim by Gmail message ID.
11. A conversational "yes" is not approval. Only a structured Kolo approval
   event that passes `approval_guard.py verify` authorizes a customer price.
12. If the activating Kolo user says stop, pause, or hold, stop all outbound
   work immediately. Preserve durable state and send nothing until that same
   user explicitly resumes the workflow.

Run this skill through the dedicated Kolo agent pinned to
`litellm-fireworks/qwen-3-7-plus`, no fallback; worker jobs use the same
model with thinking off. If Kolo cannot verify the model, stop.

## Bundled resources

- `scripts/validate_profile.py`: validate runtime shop configuration.
- `scripts/activation_binding.py`: privately bind approvals to the Kolo user
  who installs and activates the skill.
- `scripts/customer_state_reset.py`: clear prior customer/job state for a fresh
  test while preserving shop, pricing, approval, cron, and watermark state.
- `scripts/approval_guard.py`: create opaque estimate IDs, bind approvals to
  route and specification, and reject changed execution state.
- `scripts/cost_components.py`: build the approval cost sheet skeleton from the
  record and rate card, and finalize it into the exact state the approval
  helper accepts, so the model only supplies quantities.
- `scripts/kolo_safe.py`: request approvals, notify the owner, upsert records,
  and write idempotent audit events without a shell.
- `scripts/inbox_claim.py`: prevent overlapping processing in a shared Kolo
  workspace and persist crash-safe phases and external-action state.
- `scripts/inbox_monitor.py`: manage two-phase activation, validated monitor
  state, and a durable provider-ID-only discovery queue.
- `scripts/estimate_record.py`: create, update, and find the private local
  estimate records used as the authoritative inbox-routing index.
- `scripts/inbox_watcher.py`: the model-free scheduled tick: validate,
  reconcile, discover, claim, fetch, intake, close mail no customer wrote,
  and start one worker job per claim that needs judgment.
- `scripts/owner_questions.py`: plain-English owner questions (a missing
  rate today), one reminder, the answer saved to the rate card.
- `scripts/cron_config.py`: render the watcher command and the per-claim
  worker prompt, and bind durable monitor state to the complete
  behavior-bearing live Kolo cron configuration.
- `templates/worker-*.txt`: a worker's whole instruction set, the common
  preamble plus one branch by record status; workers never read SKILL.md.
- `scripts/gmail_classify.py`: from deterministic Gmail headers alone, set
  aside mail no customer wrote (bounces, automatic replies, calendar
  invitations and RSVPs, automated notifications, mailing-list mail, and
  mail from the shop's own domain) so it never becomes an estimate.
- `scripts/customer_content_guard.py`: reject owner-only jeweler cost and
  pricing assumptions from any customer-facing text before sending.
- `scripts/route_ownership.py`: prove thread ownership from an exact route,
  one schema-valid estimate record, and its initiating inbox claim.
- `scripts/gmail_reply.py`: construct a Gmail reply payload bound to the
  original thread and RFC message headers.
- `scripts/gmail_safe.py`: send a claimed Gmail reply with write-ahead,
  ambiguity handling, and a durable same-thread provider receipt.
- `scripts/gmail_fetch.py`: perform paginated Gmail discovery and fetch claimed
  messages/threads through fixed Maton requests without model-built commands.
- `scripts/gmail_route.py`: derive the recipient and private customer identity
  key from the exact inbound Gmail message rather than a display name.
- `scripts/rendering_materialize.py`: copy a PNG the desk rendered into its
  own work folder (or one the Kolo image tool put in the managed media
  directory) into the claimed canonical rendering path.
- `scripts/doctor.py`: read-only scan of the desk's state, one line per
  inconsistency with the exact command that repairs it; `--requeue <gmail-id>`
  hands the desk an email again through the normal path. This is what the
  main session runs instead of reading files.
- `scripts/rendering_wait.py`: keep an asynchronous rendering claim active for
  at most eight fixed 30-second intervals while awaiting its completion event.
- `scripts/workflow_safe.py`: execute complete spec-follow-up, approval-request,
  and approved-estimate actions without exposing claim tokens or partial state
  transitions to the model.
- `scripts/pricing_model.py`: calculate the customer price from the configured
  cost-plus or target-margin model.
- `scripts/spot_price.py`: fetch and privately cache precious-metal spot prices
  at the configured per-estimate, daily, or weekly cadence.
- `scripts/appointment_options.py`: validate recent live calendar availability
  and derive correct localized weekday/date labels.
- `scripts/calendar_query.py`: query Google Calendar free/busy through the
  routed Maton gateway and persist provider request/hash evidence.
- `templates/shop-profile.json`: runtime profile template.
- `templates/customer-emails.md`: customer message templates.
- `templates/spec-gate-email.md`: batched retail intake request.
- `templates/approved-estimate-note.md`: required estimate disclaimer.
- `references/rendering-standards.md`: read only when producing a rendering.
- `references/nudge-workflow.md`: read only when scheduling follow-ups.
- `references/spot-metal-pricing.md`: read only when spot metal pricing is enabled.
- `references/OWNER-GUIDE.md`: owner-facing trust and safety explanation.

Use `{baseDir}` as the installed skill directory in commands below.

## Phase 0: validate the shop profile

The runtime profile is `estimate-desk/shop-profile.json` in the workspace, not
in this skill and not in `SKILL.md` frontmatter. Do not store or trust a manual
`ready` field.

On first setup, copy `{baseDir}/templates/shop-profile.json` to the runtime
location and collect:

1. Business/shop name, outbound mailbox, and signature. The Kolo user who
   installs and activates the skill is automatically the approver; never ask
   for or configure a separate approver.
2. Business address (street, city, state, zip) — used for calendar invites and
   email communications.
3. Business website (if available) — used for calendar invites and email
   signatures.
3a. Voice, one or two sentences in the owner's words ("warm, short, first
   names, sign as Cali Jewelers"). Stored as `shop.voice`; every customer
   email is written from the whole thread in that voice, then checked for
   the exact figures. Optional; the default is warm and plain.
4. Nothing to ask here: the desk is retail only. Leave `shop.mode` as
   `retailer`; never offer a wholesale or trade mode.
5. Pricing model: cost-plus multiplier or target margin. For cost-plus, convert
   `25%` to `1.25` and confirm `$1,000 cost → $1,250 quote`. For target margin,
   store the decimal margin and confirm the resulting example price.
6. Whether spot metal pricing is enabled; provider (`stackerscan` or
   `gold-api`), refresh frequency (`per_estimate`, `daily`, or `weekly`), and
   unit. StackerScan is the default and supports grams; gold-api uses troy oz.
7. The owner's channel for questions, notices, and rendering previews. The
   default is this setup thread, where the approval cards also appear, so
   the owner has one place to look; nothing needs to be stored for that.
   If the owner would rather use another Kolo chat, or an SMS or Slack chat
   they already have with Kolo, run `kolo list-chats`, let them pick, and
   store its session key as
   `owner_channel.session_key` (kind under `owner_channel.kind`) in the shop
   profile. Approval cards always go to the approval queue. This never
   changes the customer's original-channel routing.
8. Trust stage. Default to Stage 1.
9. Scheduling. The calendar is the owner's primary Google Calendar, stored
   as `primary`; never ask for a calendar id. Only if the owner wants a
   different calendar, run `python3 {baseDir}/scripts/calendar_query.py
   --list-calendars` and let them pick by name. Ask for the days and hours
   they take design consultations (stored as `scheduling.windows`, for
   example weekdays 10:00 to 17:00) and how long one takes (default 30
   minutes). IANA timezone from their address. Keep the meeting-offer window
   at 7 days so the first meeting is offered soon, never near delivery.
10. Inbox monitoring hours: ask when the desk should watch the inbox, in the
    owner's words ("Mon-Fri 8am-6pm", "daily 7-23"), and store them under
    `inbox_monitoring.business_hours` with the timezone. Suggest the
    consultation days with an hour either side. Render the cron schedule
    with `python3 {baseDir}/scripts/cron_config.py schedule-from-hours
    --hours '<their words>'` and use that expression when creating the
    watcher job. Outside those hours nothing is read and nothing is sent.
11. Readiness. Before enabling the cron, and after any platform change, run
    `python3 {baseDir}/scripts/readiness.py --workspace '<absolute-workspace>'
    --base-dir '{baseDir}'` and fix every FAIL: profile, calendar and windows,
    activation binding, monitor state, the inline judgment model, audit-trail
    access (rejections are read from it), the Kolo backend, and the watcher
    job. It changes nothing and contacts no customer.

Before reading or processing an inquiry, run:

```bash
python3 {baseDir}/scripts/validate_profile.py estimate-desk/shop-profile.json
```

The output is `{"ready": true|false, "errors": [...], "missing_fields": [...]}`.

- **ready: true, missing_fields is empty** — proceed normally.
- **ready: true, missing_fields is non-empty** — the profile is valid but has
  optional fields not yet collected (e.g. `shop.website`). Prompt the owner for
  those fields and update the profile. Do not block on them.
- **ready: false** — show the owner the returned `errors` and stop. Do not
  proceed until all errors are resolved. Common case: existing profile from a
  prior version missing `shop.address` — collect the business address and
  update the profile.

Do not modify the installed skill to store shop settings.

## Trust stages

The profile records a trust stage (1, 2, or 3; missing or unreadable means
1), but today every stage behaves the same, by the owner's decision
(WORKFLOW.md 6.6): every price, every rendering, every offer of meeting
times, and every booking goes to the owner as an approval card, at every
stage. The one customer email the desk sends on its own is the price-free
specification follow-up. Act on scheduling intent immediately at every
stage; never delay a meeting offer until near the desired delivery date.
Never advance the stage automatically.

## Inbox monitoring

Monitoring is optional and requires `inbox_monitoring.enabled: true`. Search
Gmail through the path returned by `kolo integration-routing`; when it returns
`maton`, read `/opt/kolo-skills/api-gateway/SKILL.md` before using Gmail.

### Operator procedures

One-time setup and activation, updating an active monitor (the two-phase
reconfiguration), and the customer-state and business-state resets are
interactive-session procedures. They live in
`references/monitor-operations.md`; read that file only when performing one of
them. A cron run never performs them. Two rules from it that every session
must keep: never replace the cron or reset its activation timestamp or
discovery watermark, and the Kolo user who installs and activates the skill is
automatically the approver.

### Watcher and workers

The scheduled Kolo job is a command, not a model turn. Every tick it runs
`inbox_watcher.py`, which performs the discovery phase and the deterministic
front of the queue phase below, closes mail no customer wrote, and leases
each remaining claim to a one-shot worker job whose prompt is
`templates/worker-common.txt` plus one branch prompt (intake or
post-estimate, by record status), with a 900-second clock, the pinned
model, thinking off, and the safe tool allowlist. The worker begins with
`workflow_safe.py worker-start` (lease proof, intake result, thread as
text, `work_paths`), makes one judgment, runs `review-thread` (the review
plus every deterministic step after it) and at most `price`; it never
discovers, claims, or reports. If a worker
dies, its lease lapses, the stale reconciler resumes the claim once, and the
next tick starts a new worker; `worker-start` says where to resume (a
`resume` object: send the recorded follow-up, review nothing again;
`next_action: done`: the send already happened). Watcher stdout is the run
report or `NO_REPLY`.

### Cron discovery phase

Performed by the watcher on every tick; a worker never runs these steps.
Discovery and processing are separate. A processing failure must not prevent a
later discovery window from being durably recorded.

1. Validate the shop profile, then read monitor status. Exit successfully and
   silently with `NO_REPLY` when state is `prepared` or `reconfiguring`. Missing,
   corrupt, or unsupported state is an error and fails closed without Gmail or
   customer side effects. Never call goal tools from the isolated cron.
   Never call `inbox_monitor.py prepare-run` directly. The deterministic Gmail
   discovery helper owns its private run directory and batch; never create or
   substitute a platform `/tmp` or hidden workspace folder.
2. Run `inbox_claim.py notification-reconcile-stale
   --minimum-age-seconds 600`. It may only convert stale `pending` owner alerts
   to `uncertain`; it must never deliver or retry an alert.
3. Perform discovery only through the deterministic gateway helper:

   ```bash
   python3 {baseDir}/scripts/gmail_fetch.py discover \
     --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor'
   ```

   It captures `window_end_ms`, applies the one-second overlap, paginates every
   Gmail page, fetches and validates integer `internalDate`, writes only Gmail
   IDs/thread IDs/timestamps, commits the discovery batch, and advances the
   watermark atomically. Never use direct curl, `gws`, `python3 -c`, a shell
   credential source, or call `discover-complete` directly. If the helper
   fails, the watermark remains unchanged and processing must stop.

### Queue processing phase

Every tick, `inbox_watcher.py` (a command job, no model turn) does the whole
of this itself: it discovers new mail behind a durable watermark, claims one
message at a time under a journal, fetches the thread, runs intake, and
judges the claim with a few stateless model calls (`pipeline.py`): triage
and extraction for a new inquiry, classification for a reply on a sent
estimate. Everything that follows is deterministic code: the specification
gate, pricing from the profile, the price card with its execute line, the
follow-up email (drafted in the shop's voice, reflowed to plain text), the
rate question, the appointment card from live free/busy, the rendering
from the customer's artwork with its card. Each claim ends processed,
parked behind a question, or on a card; a claim the tick cannot finish is
retried with a bound (six tries for a gateway or model hiccup, two for a
refusal) and then becomes one question to the owner. A worker agent job is
started only when the inline judgment hands off; it receives
`templates/worker-common.txt` plus one branch prompt, never this file, and
it files cards and never emails a customer.

The main session does none of this. It runs the execute line on an
approved card, runs `answer-question` with the owner's words, and runs
`doctor.py` when asked what is going on. Every state file under
`estimate-desk/` is the watcher's or an executor's to write.

## Email reply invariant

**MANDATORY: Every customer-facing Gmail message from this skill—including the
initial acknowledgment, specification request, estimate, scheduling message,
and follow-up—MUST use `scripts/gmail_reply.py` with the route produced from
the exact inbound message by `scripts/gmail_route.py`.**

**You cannot send a customer email without these scripts. Period.**

**Failure mode:** If you compose a new email with a subject like "Re: Following
up on your inquiry" instead of replying to the original thread, you have
violated this invariant. The customer will see a separate thread, not a
continuation of their inquiry. This is a critical bug that breaks the customer
experience.

**Pre-send validation (required before EVERY customer email):**

1. You have the original Gmail message JSON (from the inbox monitor or a fresh
   Gmail API fetch). If you don't have it, STOP and fetch it first.

2. You ran `gmail_route.py` on that message to produce `route.json`:
   ```bash
   python3 {baseDir}/scripts/gmail_route.py \
     "$WORK/gmail-message.json" '<outbound-mailbox>' "$WORK/route.json"
   ```

3. You ran `gmail_reply.py` with `route.json` and your reply body:
   ```bash
   python3 {baseDir}/scripts/gmail_reply.py \
     "$WORK/route.json" "$WORK/customer-reply.txt" "$WORK/gmail-send.json"
   ```

4. You verified `gmail-send.json` contains:
   - `threadId` (from the original message)
   - `In-Reply-To` header (original Message-ID)
   - `References` header (includes original Message-ID)

5. Send only through the applicable `workflow_safe.py` high-level action.
   It rebuilds and validates the reply, writes `pending` before provider
   invocation, stores accepted message/thread IDs, updates the authoritative
   record, and mirrors it. Never invoke `gmail_safe.py` or the provider directly.
   A repeated `sent` action reconstructs the receipt; `pending` or `uncertain`
   is never retried automatically.

**If you cannot complete all 5 steps, you cannot send the email. Stop and recover.**

**If you are composing a new subject line, you are doing it wrong. Stop and
recover the original message first. Never compose a standalone email, invent a
generic subject such as `Your inquiry`, or retrieve a recipient from a
name-based customer record. If route construction or reply construction fails,
stop without sending. A customer name may be retained as display-only contact
metadata, but it must never select an estimate, identity, recipient, or thread.**

## Phase 1: triage

Read the full inbound message and attachments. First decide whether it is a
quote request at all. The header classifier removes machine mail before
anything is read, but a person can write to the shop about anything, and
intake opens a record before the thread is read. When the record is still
`awaiting_specs` and the thread asks for no custom piece, replica, redesign,
remount, or repair (a supplier, marketing, a personal note, a job
application, an unrelated question), run `workflow_safe.py not-an-inquiry`
with the claimed Gmail ID, the estimate ID, a fixed reason
(`not_a_quote_request`, `vendor_or_marketing`, `personal_or_internal`, or
`unrelated`), and `--record-output`. It retires that record as
`not_an_inquiry`, mirrors it, and finalizes the claim with no customer reply
and no owner alert. Escalations (anger, legal, payment, appraisal) are never
`not-an-inquiry`; they remain manual review. Route the rest as follows:

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

Two rules the gate applies on its own. A stone the customer already owns
(their mother's diamond, a stone to reset or remount, anything under
`customer_supplied_materials`) is never graded: the desk asks only its
shape and its carat weight or millimetre size so the setting fits, never
its color, clarity, cut, or origin, and the price carries no stone line for
it. And the desk never sends the same follow-up twice: when a reply leaves
the same fields open that were already asked for, the owner gets a question
instead (skip and price it, ask again, or handle myself). And a customer
who asks to meet gets the meeting first, at any stage: before an estimate
the desk files the appointment card instead of the detail questions, the
confirmation says the design gets settled at the meeting, and pricing picks
up whenever the details arrive, by email or after the visit. Every customer
email reads as the jeweler writing back: it reacts to what the customer
shared, asks for everything still missing in one short list, and invites
them in.

Before sending a retail estimate, require all applicable fields:

- Stone type, lab/natural origin, carat, color, clarity, and cut/shape.
- Metal, karat, and color.
- Finger size for rings; length/dimensions for chains, bracelets, or pendants.
- Piece type and quantity and setting/style.

A descriptive design phrase such as `classic band`, `solitaire`, or
`channel-set` satisfies setting/style, as does an explicit delegation to the
jeweler. Placeholder values such as `not specified`, `unspecified`, `unknown`,
`tbd`, or `n/a` never satisfy it; the helper keeps `setting_style` missing.

Color, clarity, cut, finish, and similar quality choices are complete when the
customer explicitly delegates them to the jeweler; use shop defaults only as
owner-facing pricing assumptions. Budget and event date are useful intake
questions but are not prerequisites to an estimate. Default to shop sourcing
unless the customer says they are supplying the stone or metal.

A profile value of `defaults.stone_origin: ask_always` is not delegatable. The
customer must explicitly choose natural or lab-grown before the specification
gate can complete. The thread-review helper enforces this from the runtime
profile; never replace it with `delegated_to_jeweler` or infer an origin from
price sensitivity.

For a piece without stones, stone fields are not applicable; do not report a
misleading `x/8` score. Cost assumptions remain owner-only.

Evaluate these fields against the merged full-thread specification recorded in
Inbox monitoring step 3. A fact supplied in the initiating inquiry or any
later customer reply is known and must not be requested again.

When fields are missing, use `templates/spec-gate-email.md`: one friendly,
price-free, batched request; do not re-ask known facts. Offer two real open
slots with timezone only when `scheduling.calendar` is configured and
`scheduling.windows` contains declared availability. Otherwise omit appointment
slots from the specification request and do not call `calendar_query.py` or
treat their absence as an error. When both are configured, run exactly:

```bash
python3 {baseDir}/scripts/calendar_query.py \
  --time-min '<ISO timestamp with timezone>' \
  --time-max '<later ISO timestamp with timezone>' \
  --timezone '<profile scheduling.timezone>' \
  --calendar-id '<profile scheduling.calendar>' \
  --output '<work_paths.calendar_receipt>'
```

`calendar_query.py` does not support `--window-days`. Write candidate slots
derived only from the declared windows inside those query bounds to
`work_paths.calendar_candidate_slots`, then run exactly:

```bash
python3 {baseDir}/scripts/appointment_options.py \
  '<work_paths.calendar_receipt>' \
  '<work_paths.calendar_candidate_slots>' \
  '<work_paths.calendar_options>' \
  --timezone '<profile scheduling.timezone>' \
  --window-days '<profile scheduling.meeting_offer_window_days>'
```

Use slots only from the validated `work_paths.calendar_options`. Send the
specification request with `workflow_safe.py send-spec-followup` at every
stage; it carries no price. Never ask the owner whether to draft, send, or
continue routing.
For Gmail, send it only through the Email reply invariant above.
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
| Design development, casting, setting, finishing, engraving | profile fees |
| Bench labor | hours × profile $/hr; always separate internally |
| COGS | sum of costs |
| Proposed quote | `pricing_model.py` using the configured pricing model |

When spot metal pricing is enabled, run `spot_price.py` before pricing. Its
cache implements the configured per-estimate, daily, or weekly cadence. Keep
the spot response, quantities, and derived metal cost owner-only. If the fetch
or cache validation fails, stop pricing; never silently substitute a remembered
or stale spot price. Read `references/spot-metal-pricing.md` before the first
spot-priced estimate in a session.

Estimate the high side deliberately. If finished weight is unknown, give the
owner a bracket instead of false precision. On a first job without a rate card,
prepare quantities with dollars blank and ask for the missing rates. Never turn
an estimate into an appraisal or replacement value.

## Phase 3: bind and request owner approval

Pricing and the card are code, run by the watcher inside the tick (or by
`answer-question` after the owner supplies a rate):
`cost_components.py prepare` builds the cost skeleton from the recorded specification and the
profile (spot price included when enabled), the model supplies only the
few quantities a bench jeweler would estimate (finished grams, bench hours,
a missing center carat, which fees and accent stones apply), and
`pricing_model.py` turns the sheet into the quote. `workflow_safe.py price`
then binds the price to the record, files the Kolo card whose title carries
the quote, cost, profit, and every assumption (SMS shows the title alone),
registers the brief so a rejection is read from the audit trail, and drafts
the customer's estimate email so the approval sends in seconds. The
customer-visible price is the bound proposed price; cost, rates, hours, and
assumptions never leave the owner's card.

Rejections need nothing from the session: the watcher reads them from the
audit trail. An edited price is not applied: reject the card and the desk
re-prices on the owner's word. Nothing here is run by hand; the session
never authors a cost sheet, never runs the pricing helpers, and never
files a card itself.

## Phase 4: send through the customer route

After successful verification, use only `workflow_safe.py
send-approved-estimate`. It re-verifies approval, builds the same-thread reply,
journals the provider action against the authoritative claim, requires every
customer-visible dollar amount to equal the approved price, stores provider
evidence, moves the record from `pending_approval` to `estimate_sent`, and
mirrors it. Never call the raw Gmail gateway or edit the status yourself.

```bash
python3 {baseDir}/scripts/workflow_safe.py send-approved-estimate \
  --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
  --record-root '<absolute-workspace>/estimate-desk/records' \
  --estimate-id '<jed-id>' \
  --approved "$WORK/approved.json" \
  --body "$WORK/customer-reply.txt" --gmail-payload "$WORK/gmail-send.json" \
  --provider-response "$WORK/gmail-provider-response.json" \
  --record-output "$WORK/current-record.json"
```

Do not depend on the earlier per-claim `current-state.json`; normal inbound
finalization deletes that work directory. The helper reconstructs the exact
route, specification, price, and owner-only cost sheet from the authoritative
record, resolves the approval-source Gmail claim from its stored provider ID,
and verifies matching approval evidence before sending. The optional
`--message-id` and `--current-state` flags are for controlled diagnostics only.

Then:

1. Fill `templates/customer-emails.md` using only the customer-safe written
   specification and the exact owner-approved price. Do not read from, copy, or
   summarize the internal pricing or jeweler cost assumption fields while
   drafting. Save only the reply body to `$WORK/customer-reply.txt`.
2. Include the canonical high-end/pending-design substance from
   `templates/approved-estimate-note.md`, estimated—not guaranteed—lead time,
   validity date, and two or three live appointment options with timezone.
3. Before every customer send on every channel, run the reusable final
   confidentiality guard. Exit 2 blocks the send; rewrite the customer text
   without the confidential material and run it again. Never bypass a failure.

   ```bash
   python3 {baseDir}/scripts/customer_content_guard.py \
     "$WORK/customer-reply.txt"
   ```

4. `workflow_safe.py` uses the immutable route in the approved state and runs
   `gmail_reply.py` internally. That route is derived from the initiating
   customer email, so the estimate replies to the original request while
   remaining in its Gmail thread. The top-level `threadId` and encoded RFC 5322
   `In-Reply-To` and `References` headers are mandatory. Never rebuild the
   approved route from another message, substitute a new message, or compose a
   new subject.
5. Never use the Kolo `message` tool, `deliveryContext.to`, or `kolo:<uuid>` for
   the customer. Use those only for an owner-facing copy or notification.
6. Store the provider's outbound message ID. If the response is uncertain or
   lacks a message ID, do not retry automatically; inspect the Gmail thread or
   escalate to the owner first.

For scheduling, **always query the calendar first** before making any claims
about existing meetings. Use the routed calendar integration to search for
events with the customer's email as an attendee. Never infer
meeting state from email content, subject lines, or scheduling language in
messages.

After confirming no conflicting meeting exists, use `calendar_query.py` to
query live free/busy into a private receipt. It validates Google response kind,
query bounds, calendar ID, server date, and provider request ID, then hashes the
response. Create a candidate-slots JSON array only from declared profile windows
inside those query bounds, then run `appointment_options.py` with the receipt
and candidates. It rejects stale, out-of-range, or busy-overlapping slots and
derives the weekday/date labels. Offer those
specific times, then re-check immediately before
creating an event. Include the customer's email address (from `route.json`
recipient field) as an attendee in the calendar event so they receive the
invitation. Confirm to the customer only after the calendar write succeeds.
Use the owner's IANA timezone, never the pod's UTC clock. Never select meeting
times based on the desired delivery date.

### The main Kolo session: hard rules

The main session is the owner's chat, not a worker. It never writes a record,
the shop profile, a rate, a price, or any file under `estimate-desk/` by hand,
never calls the Gmail or calendar gateway itself, never runs
`cost_components.py`, `pricing_model.py`, `spot_price.py`, `gmail_reply.py`,
or `request-approval`, and never continues an inquiry in chat. Every decision
the desk needs from the owner arrives with the exact command to run, and the
session runs that command and nothing else. If unsure, ask and wait.

When an execute line fails, run the same line once more; every executor
resumes where it stopped and never sends or books twice (a send whose
outcome was unknown is checked against the thread first). If it fails
again, paste the output and wait: the desk has already asked the owner
what to do, with the fix attached.

Never summarise a record, a queue, a claim, or a brief from memory, and
never write, rename, or delete anything under `estimate-desk/`. When
something looks wrong, run `python3 {baseDir}/scripts/doctor.py --workspace
'<absolute-workspace>'` and show what it prints: every finding carries the
one line that repairs it. If the doctor has no line for what you see, say
so and stop. A missed email is handed back with `doctor.py --requeue
'<gmail-id>'`, never by writing a queue item.

While a desk question is open (the last desk message ended with
`desk-answer <CODE>`), the owner's next reply is the answer to it: run
`answer-question` with their words first, and only if that command refuses
treat the reply as anything else. Never ask the owner a question of your
own while a desk question is open, and never read their reply to the desk
as consent for something you proposed.

### Approved briefs: run the payload's `execute` line

Every approval the desk files (price, renderings, appointment, manual
review) carries an `execute` field in its execution payload. When Kolo
delivers the decision as approved, copy that line, replace `<Brief ID>` with
the Brief ID from the delivered decision, run it, and paste its output. That
one command re-verifies the bound state, sends or books through the desk's
own helpers, records the receipt, and reports the brief executed. A repeat is
a no-op. Rejections need no command from this session: Kolo does not
deliver them here, and the watcher reads them from the audit trail every
tick, then asks the owner what to do (appointment cards), holds the
renderings back, or notes the passed price. If Kolo ever does deliver a
rejected decision whose payload carries `execute_on_reject`, run that line;
it is harmless when the watcher got there first.

The execute lines are, for reference:

```bash
python3 {baseDir}/scripts/workflow_safe.py send-approved-estimate-brief \
  --workspace '<absolute-workspace>' --estimate-id '<jed-id>' --brief-id '<Brief ID>'
python3 {baseDir}/scripts/workflow_safe.py send-approved-rendering \
  --workspace '<absolute-workspace>' --estimate-id '<jed-id>' \
  --message-id '<gmail_message_id>' --brief-id '<Brief ID>'
python3 {baseDir}/scripts/workflow_safe.py book-approved-appointment \
  --workspace '<absolute-workspace>' --estimate-id '<jed-id>' \
  --message-id '<gmail_message_id>' --brief-id '<Brief ID>' --option 1
python3 {baseDir}/scripts/workflow_safe.py send-approved-times \
  --workspace '<absolute-workspace>' --estimate-id '<jed-id>' \
  --message-id '<gmail_message_id>' --brief-id '<Brief ID>'
python3 {baseDir}/scripts/workflow_safe.py appointment-rejected \
  --workspace '<absolute-workspace>' --estimate-id '<jed-id>' \
  --message-id '<gmail_message_id>' --brief-id '<Brief ID>'
python3 {baseDir}/scripts/workflow_safe.py resolve-review-approval \
  --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
  --review-key '<review_key>' --brief-id '<Brief ID>'
```

Appointment cards come in two kinds: a booking card names one time and
approve books it; an offer card lists two or three times and approve emails
them to the customer, nothing booked. An edited price is not applied by the
session: reject the brief and tell the owner the desk will re-price.

### Owner questions: the `desk-answer` tag

The desk asks the owner questions in plain words and parks the claim: which
rate to use, whether a new thread from a known customer is the same piece or
a new one, what an unclear reply meant, what to do after a rejected
appointment card, what to do when a customer asks to meet but the
calendar offers no free time (or could not be read), and what to do when a
customer was asked for details once and replied without giving them, and
what to do when a card's command failed part way (reply "retry", "release"
to let go of a calendar hold, or "handle myself"), and what to do with an
email the watcher could not finish after its own retries (reply "retry",
"skip", or "handle myself"; the email is never dropped silently); those get a question
straight away, never a card with nothing on it and never the same email
twice. Every such message
ends with one line, `desk-answer <CODE>`. When the owner replies to it, run exactly this, their words
verbatim:

```bash
python3 {baseDir}/scripts/workflow_safe.py answer-question \
  --workspace '<absolute-workspace>' --base-dir '{baseDir}' \
  --question '<CODE>' --answer '<the owner's reply, verbatim>'
```

When the owner rejects an appointment card, the watcher notices within a
tick and asks them here what to do. The owner may also answer before the
question arrives, in their own words: times to offer, "other times", or
"handle myself". Either way, run the same command without `--question` and
with their words; the desk matches it to the customer whose card was filed
last, or to the customer they named. Times become a new offer card; nothing
reaches the customer until that card is approved. Paste the output. If the command
refuses, tell the owner what it said and wait; never pick an answer or
re-run with a different one. If it fails part way (a traceback), run the
same command again: it carries on from where it stopped.

## Phase 5: records, follow-up, and cleanup

Update the estimate under the opaque estimate ID created with the initial
ownership record; never create a replacement ID or use a customer name. The
private local record is the authoritative routing source and the Kolo record is
its owner-visible mirror. The record must use `schema_version: 1` and retain the
complete validated `route`,
including its email-derived identity key, initiating inbound Gmail message ID,
thread ID, and original RFC Message-ID. It should also retain the inbound
timestamp; approval binding hash and approved price; specification and
assumptions; internal cost sheet; outbound provider message ID; trust stage;
appointment data; and next action date. First run
`scripts/estimate_record.py upsert` with the complete record JSON, then
mirror that exact JSON through `scripts/kolo_safe.py record-upsert` using record
type `skill.jewelry_estimate`, the estimate ID as `external-id`, and the same
status. Never update the Kolo mirror if the authoritative local upsert fails.

After the record is durable, write the corresponding audit event through
`scripts/kolo_safe.py log-action`. Use an idempotency key composed only of the
opaque estimate ID and fixed event type, and pass event details through a JSON
file. The record remains the workflow source of truth; the audit event does not
replace the inbox claim or record.

Statuses are `awaiting_specs`, `pending_approval`, `estimate_sent`,
`appointment_booked`, `approved`, `declined`, `manual_review`, and `dormant`.

For day-3 and day-7 follow-ups, read `references/nudge-workflow.md`. After day 7,
stop and mark dormant.

To retire one record the shop will not pursue (opened in error, a duplicate of
another thread, superseded, withdrawn by the customer before any price was
sent, not an inquiry, or a test artifact), run `scripts/estimate_record.py retire` with
`--estimate-id`, `--reason`, and an optional `--note`. It moves that single
record to `dormant`, stores the reason and previous status under `retirement`,
and changes no claim, queue item, watermark, or other record. It refuses records
that are already terminal and refuses `estimate_sent`, `appointment_booked`, and
`approved` records: once the customer has been told a price, resolve it with the
customer rather than by changing the record. Mirror the retired record through
`scripts/kolo_safe.py record-upsert` afterwards, as with any other update.

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
