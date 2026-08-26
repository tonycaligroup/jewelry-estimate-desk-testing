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
  workspace and persist crash-safe phases and external-action state.
- `scripts/inbox_monitor.py`: manage two-phase activation, validated monitor
  state, and a durable provider-ID-only discovery queue.
- `scripts/estimate_record.py`: create, update, and find the private local
  estimate records used as the authoritative inbox-routing index.
- `scripts/cron_config.py`: render the fixed cron message and bind durable
  monitor state to the complete behavior-bearing live Kolo cron configuration.
- `scripts/gmail_classify.py`: conservatively identify delivery-status and
  automatic-reply messages from deterministic Gmail headers.
- `scripts/customer_content_guard.py`: reject owner-only jeweler cost and
  pricing assumptions from any customer-facing text before sending.
- `scripts/route_ownership.py`: prove thread ownership from an exact route,
  one schema-valid estimate record, and its initiating inbox claim.
- `scripts/gmail_reply.py`: construct a Gmail reply payload bound to the
  original thread and RFC message headers.
- `scripts/gmail_safe.py`: send a claimed Gmail reply with write-ahead,
  ambiguity handling, and a durable same-thread provider receipt.
- `scripts/gmail_route.py`: derive the recipient and private customer identity
  key from the exact inbound Gmail message rather than a display name.
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
2. Business address (street, city, state, zip) — used for calendar invites and
   email communications.
3. Business website (if available) — used for calendar invites and email
   signatures.
4. Mode: `retailer`, `wholesale_middle_man`, or `both`.
5. Markup multiplier. Convert `25%` to `1.25` and confirm with the example
   `$1,000 cost → $1,250 quote` before saving.
6. Trust stage. Default to Stage 1.
7. Booking mode and IANA timezone.
8. Optional inbox-monitoring hours and timezone.

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

| Stage | Autonomous work | Owner approval still required |
|---|---|---|
| 1 — Watch me | Read, calculate, draft, and schedule | Every outbound message and price |
| 2 — Ask questions | Stage 1 plus price-free specification requests | Every price |
| 3 — Book me | Stage 2 work | Every price |

Scheduling autonomy (offering times, creating calendar events, sending
scheduling confirmations) is permitted at all stages. Trust stage restrictions
apply to pricing and estimates, not to scheduling. Never advance the stage
automatically. Missing or unreadable stage means Stage 1.

## Inbox monitoring

Monitoring is optional and requires `inbox_monitoring.enabled: true`. Search
Gmail through the path returned by `kolo integration-routing`; when it returns
`maton`, read `/opt/kolo-skills/api-gateway/SKILL.md` before using Gmail.

### One-time setup and activation boundary

Never search or act on historical mail during installation. There is no
seven-day bootstrap or other fallback window. Jewelers may already have handled
every pre-activation inquiry manually.

1. Perform a read-only capability check. Verify that the Gmail integration can:
   use `after:<epoch-seconds>`, return integer `internalDate` epoch milliseconds,
   and enumerate every page until no `nextPageToken` remains. Write a private
   JSON file containing these three fixed booleans:

   ```json
   {"gmail_after_epoch":true,"gmail_internal_date_ms":true,"gmail_complete_pagination":true}
   ```

   If any capability is unavailable, report an unsupported environment and
   leave monitoring inactive. Never weaken the activation boundary.
2. Render the fixed cron message from the bundled template:

   ```bash
   python3 {baseDir}/scripts/cron_config.py render-message \
     --workspace '<absolute-workspace>' --base-dir '{baseDir}' \
     --output "$WORK/cron-message.txt"
   ```

3. Create exactly one disabled `jed-inbox-monitor` using that message, the
   configured business-hours schedule and IANA timezone, model
   `litellm-fireworks/qwen-3-7-plus`, no fallbacks, a 300-second timeout,
   `lightContext: true`, an isolated session, and Kolo owner announcement
   delivery. Never enable or manually run it yet. If a job with that name
   already exists, stop and use the reconfiguration procedure below; never
   create a second job.
4. Re-read the disabled job from Kolo into private JSON and derive its stable
   binding:

   ```bash
   python3 {baseDir}/scripts/cron_config.py bind-live \
     --job "$WORK/live-cron.json" \
     --workspace '<absolute-workspace>' --base-dir '{baseDir}' \
     --output "$WORK/cron-binding.json"
   ```

   The binding includes job ID, agent, schedule, timezone, session, wake mode,
   complete prompt, model, fallbacks, timeout, light-context setting, optional
   thinking/tool allow-list fields, and delivery destination. Generated
   timestamps and runtime counters are excluded. `enabled` is also excluded
   because it is a lifecycle flag, but it must be false at this step and true
   only after activation.
5. Prepare durable state under an atomic setup lock:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py prepare \
     --capabilities "$WORK/capabilities.json" \
     --cron-config "$WORK/cron-binding.json"
   ```

6. Activate only against that exact verified binding, then enable the same job
   ID. Re-read it once more and require `enabled: true` and a successful
   `bind-live` result equal to `cron-binding.json`:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py activate \
     --cron-config "$WORK/cron-binding.json"
   ```

   The helper atomically records `activated_at_ms` and initializes the discovery
   watermark. Missing, corrupt, or unsupported-version active state fails closed
   and must never be silently recreated.

### Updating an active monitor

Never replace the cron or reset its activation timestamp or discovery watermark.
Edit the existing job ID in place:

1. Re-read the current live job. For legacy schema-1 state, reconstruct and
   cryptographically verify the exact historical five-field binding:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py verify-legacy-binding \
     --live-job "$WORK/current-live-cron.json" \
     --output "$WORK/current-bound-config.json"
   ```

   The helper compares the reconstructed canonical hash with the durable bound
   hash and makes no state change. For schema-2 state, use the previously
   verified complete binding. Stop on any mismatch; never guess or overwrite it.
2. Generate the intended complete target binding from the current job identity:

   ```bash
   python3 {baseDir}/scripts/cron_config.py target-binding \
     --job "$WORK/current-live-cron.json" \
     --workspace '<absolute-workspace>' --base-dir '{baseDir}' \
     --output "$WORK/target-cron-binding.json"
   python3 {baseDir}/scripts/inbox_monitor.py reconfigure-prepare \
     --current-cron-config "$WORK/current-bound-config.json" \
     --target-cron-config "$WORK/target-cron-binding.json"
   ```

   This atomically changes monitor state to `reconfiguring`; every cron run must
   then exit successfully with `NO_REPLY` before Gmail access or side effects.
3. Disable and edit the existing Kolo job in place with the rendered prompt and
   every target runtime field. Re-read it and run `bind-live`; the resulting
   binding must exactly equal `target-cron-binding.json`.
4. Commit the target binding and enable the same job ID:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py reconfigure-activate \
     --cron-config "$WORK/verified-target-binding.json"
   ```

   Re-read once more and require `enabled: true` plus the same verified binding.
   If the edit fails, restore the complete former live config before using
   `reconfigure-cancel`; never cancel while the live cron differs from the
   formerly bound config.

### Cron discovery phase

Discovery and processing are separate. A processing failure must not prevent a
later discovery window from being durably recorded.

1. Validate the shop profile, then read monitor status. Exit successfully and
   silently with `NO_REPLY` when state is `prepared` or `reconfiguring`. Missing,
   corrupt, or unsupported state is an error and fails closed without Gmail or
   customer side effects. Never call goal tools from the isolated cron.
2. Run `inbox_claim.py notification-reconcile-stale
   --minimum-age-seconds 600`. It may only convert stale `pending` owner alerts
   to `uncertain`; it must never deliver or retry an alert.
3. Capture `window_end_ms` before searching. Starting at the durable watermark,
   query Gmail with a one-second overlap using
   `in:inbox after:<epoch-seconds>`. Paginate within this run until
   `nextPageToken` is absent. Never persist a Gmail page token across runs and
   never impose a fixed ten-message limit.
4. For every result, collect only immutable Gmail `id`, `threadId`, and integer
   `internalDate`. Write no customer name, address, subject, or message content
   to the discovery batch. After complete enumeration, call:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py discover-complete \
     --batch "$WORK/discovery-batch.json" \
     --window-start-ms '<durable-watermark>' \
     --window-end-ms '<captured-window-end-ms>'
   ```

   The helper rejects pre-activation messages, inserts each provider ID
   atomically, and advances the watermark only after every insertion is durable.
   If Gmail pagination fails or times out, do not call `discover-complete`; the
   watermark remains unchanged and the same window is enumerated next run.

### Queue processing phase

Repeatedly call `inbox_monitor.py next`. It returns the oldest eligible message,
or `null`. The helper permits only the oldest unfinished item in each Gmail
thread; a stuck thread does not block other threads.

For each returned message:

1. Claim the immutable Gmail message ID before fetching content, notifying,
   drafting, sending, or mutating a Kolo estimate:

   ```bash
   python3 {baseDir}/scripts/inbox_claim.py claim \
     --message-id '<gmail-id>' --resume-stale-after-seconds 600 \
     > "$WORK/claim-result.json"
   python3 {baseDir}/scripts/inbox_monitor.py sync-claim \
     --message-id '<gmail-id>' --claim-result "$WORK/claim-result.json"
   ```

   `claim` returns exit 0 for both `acquired:true` and `acquired:false`.
   A duplicate `processed` or `manual_review` claim completes the queue item. A
   duplicate recent `processing` claim remains owned by the earlier run and
   receives no side effects. A stale claim is resumed with its original token
   only when its phase journal proves every external action is settled.
   Legacy or delivery-ambiguous stale claims become manual review; never steal
   or automatically retry them.
   After an operator independently verifies that a legacy claim caused no
   external action, it may be journaled once with
   `inbox_claim.py authorize-legacy-resume --message-id '<gmail-id>'
   --claim-token '<token>' --minimum-age-seconds 600
   --confirmed-no-external-actions`. Never put this command in the cron runbook
   or infer the confirmation from an empty legacy state file.
2. Only for `acquired:true`, fetch the full Gmail message and run the conservative
   deterministic header classifier before involving the LLM:

   ```bash
   python3 {baseDir}/scripts/gmail_classify.py "$WORK/gmail-message.json"
   ```

   - `auto_reply`: complete as processed with no response or owner alert.
   - `dsn_candidate`: correlate only against durable stored outbound provider
     evidence and the exact failed-recipient email. Never trust an estimate ID
     found only in message text. A verified failure becomes `manual_review` with
     reason `delivery_failed` and an event-specific owner alert. An uncorrelated
     DSN becomes `manual_review` with reason `uncorrelated_dsn`; never treat it as
     a customer.
   - `customer_or_uncertain`: derive and validate the customer route below.
   - Mailbox quota, authentication/security, or persistent system failures are
     `manual_review` with reason `system_actionable` and use the fixed generic
     monitor alert. Other uncertain classifications are `manual_review` with
     reason `uncertain_classification`.
3. Derive the route from the exact message. The normalized sender email remains
   exact; never remove plus-address tags or merge aliases:

   ```bash
   python3 {baseDir}/scripts/gmail_route.py \
     "$WORK/gmail-message.json" '<profile-outbound-mailbox>' "$WORK/route.json"
   python3 {baseDir}/scripts/inbox_claim.py advance-phase \
     --message-id '<gmail-id>' --claim-token '<claim-token>' --phase routed
   ```

4. A thread is Kolo-owned only when one schema-valid estimate record matches the
   exact `thread_id`, exact `identity_key`, and initiating Gmail ID, and that
   initiating ID has a valid local claim. A display-name match is irrelevant.
   Build the candidate array only through the private local record store's exact
   thread lookup, then run the ownership decision:

   ```bash
   python3 {baseDir}/scripts/estimate_record.py lookup-thread \
     "$WORK/route.json" \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --output "$WORK/candidate-records.json"
   python3 {baseDir}/scripts/route_ownership.py \
     "$WORK/route.json" "$WORK/candidate-records.json" \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --thread-message-count '<exact-Gmail-thread-message-count>'
   ```

   Treat the helper decision as authoritative. `manual_review` and
   `owned_manual_review` must use the combined durable manual-review command in
   step 6 and must not send to the customer. `declined` and `dormant` records
   retain ownership but require manual review rather than a new estimate.
   For every non-review decision, advance the claim to `ownership_confirmed`
   after this helper succeeds.

   For `new_inquiry`, create the minimal ownership record before drafting or
   sending any customer response and before finishing the claim:

   ```bash
   python3 {baseDir}/scripts/estimate_record.py create-inquiry \
     "$WORK/route.json" \
     --inbound-timestamp-ms '<Gmail-internalDate-ms>' \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --output "$WORK/inquiry-record.json"
   python3 {baseDir}/scripts/kolo_safe.py record-upsert \
     --record-type skill.jewelry_estimate \
     --external-id '<estimate-id-from-inquiry-record>' \
     --payload "$WORK/inquiry-record.json" --status awaiting_specs
   ```

   The local create happens before the Kolo mirror upsert and derives a
   retry-stable opaque estimate ID from the initiating Gmail ID. Repeating both
   operations targets the same record. If either persistence operation fails,
   send nothing and finish as manual review with reason
   `record_persistence_failed`. A successful initial record is updated through
   later phases; never create a second estimate record for that thread.
5. For a valid response on an active estimate, notify the owner at every trust
   stage before any customer response. Bind the alert to an event-specific key
   that includes the event, estimate ID, and Gmail ID:

   ```bash
   python3 {baseDir}/scripts/kolo_safe.py notify-owner-claimed \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --message-id '<gmail-id>' --claim-token '<claim-token>' \
     --notification-key 'customer_replied:<jed-id>:<gmail-id>' \
     --event customer-replied --estimate-id '<jed-id>'
   ```

   The wrapper writes `pending` before invoking Kolo and records `sent` after
   successful CLI acceptance; `sent` is not an independent user-visible
   delivery receipt. Because Kolo has no delivery-receipt query, any command
   failure after invocation is `uncertain`, never a retryable pre-delivery
   failure. A process crash may leave `pending`; the stale reconciler marks it
   `uncertain` after 600 seconds. Never resend `pending` or `uncertain` alerts.
   Generic mailbox alerts tied to a claimed message must never call
   `notify-monitor` directly.
6. Use only these combined terminal commands; do not call `inbox_claim.py
   complete`, `inbox_claim.py fail`, or `reconcile-terminal` separately.

   After successful authorized processing and all required durable record
   writes:

   ```bash
   python3 {baseDir}/scripts/inbox_claim.py advance-phase \
     --message-id '<gmail-id>' --claim-token '<claim-token>' \
     --phase work_persisted
   python3 {baseDir}/scripts/inbox_claim.py advance-phase \
     --message-id '<gmail-id>' --claim-token '<claim-token>' \
     --phase ready_to_finalize
   python3 {baseDir}/scripts/inbox_monitor.py finalize \
     --message-id '<gmail-id>' \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --claim-token '<claim-token>' --outcome processed \
     --record-root '<absolute-workspace>/estimate-desk/records'
   ```

   For an initiating inquiry that remains `awaiting_specs`, "processed" means
   the price-free specification request was actually sent in the original
   Gmail thread. After the Gmail API accepts that reply, persist its unmodified
   provider response before finalizing:

   ```bash
   python3 {baseDir}/scripts/estimate_record.py record-spec-gate-sent \
     --estimate-id '<jed-id>' --reply-body "$WORK/customer-reply.txt" \
     --provider-response "$WORK/gmail-provider-response.json" \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --output "$WORK/current-record.json"
   python3 {baseDir}/scripts/kolo_safe.py record-upsert \
     --record-type skill.jewelry_estimate --external-id '<jed-id>' \
     --payload "$WORK/current-record.json" --status awaiting_specs
   ```

   `finalize --outcome processed` fails closed if an initiating
   `awaiting_specs` record lacks same-thread provider send evidence. Never treat
   record creation or `awaiting_specs` alone as completed customer handling. If
   reply construction or sending fails, or provider acceptance is ambiguous,
   use manual review instead of `processed`; never resend an ambiguous reply.

   For a later customer reply that still lacks required specifications, persist
   the accepted same-thread follow-up receipt separately before finalizing:

   ```bash
   python3 {baseDir}/scripts/estimate_record.py record-followup-sent \
     --estimate-id '<jed-id>' --source-message-id '<gmail-id>' \
     --reply-body "$WORK/customer-reply.txt" \
     --provider-response "$WORK/gmail-provider-response.json" \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --output "$WORK/current-record.json"
   python3 {baseDir}/scripts/kolo_safe.py record-upsert \
     --record-type skill.jewelry_estimate --external-id '<jed-id>' \
     --payload "$WORK/current-record.json" --status awaiting_specs
   ```

   `record-spec-gate-sent` is only for the initiating inquiry. Never overwrite
   its immutable evidence with a later follow-up receipt.

   For every manual-review decision, persist the terminal claim and queue state
   before attempting the one privacy-safe owner notification:

   ```bash
   python3 {baseDir}/scripts/kolo_safe.py manual-review-claimed \
     --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --message-id '<gmail-id>' --claim-token '<claim-token>' \
     --reason-code '<fixed_reason>'
   ```

   That notification tells the owner to ask Kolo for unresolved Jewelry
   Estimate Desk reviews; it contains no customer data. The terminal claim is
   authoritative for processing side effects and the queue is authoritative
   only for discovery. Repeating the same terminal outcome with the same token
   is a successful no-op. A different token, outcome, or reason remains an
   error. Any impossible mismatch becomes manual review—never a customer send.

   After the queue loop returns no item, run:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py assert-settled
   ```

   A nonzero result means the run is incomplete. Never return `ok` or
   `NO_REPLY` while any queue item remains `processing`.

   When the owner asks to see those reviews, run:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py manual-reviews
   ```

   Present the privacy-safe review key, fixed reason, and time. Retrieve message
   content only after the owner selects an item to review. Only after the owner
   explicitly resolves it, run `inbox_monitor.py resolve-manual-review
   --review-key '<review-key>'`; never resolve it from silence or merely because
   cron announce metadata contains a fallback delivery field.

Kolo records do not provide compare-and-swap. These helpers protect one shared
workspace but cannot guarantee exclusion across independent hosts. Keep claim
and queue state indefinitely; do not delete claims or create a retention cron.
The monitor uses only the existing statuses `processed` and `manual_review` plus
fixed reason codes. Its cron message contains only fixed instructions and
opaque/provider IDs. Stay silent when no queued work or alert exists.

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

5. You send that JSON unchanged only through the claimed delivery wrapper:
   ```bash
   python3 {baseDir}/scripts/gmail_safe.py send-reply-claimed \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --message-id '<gmail-id>' --claim-token '<claim-token>' \
     --delivery-key 'customer_reply:<jed-id>:<gmail-id>' \
     --payload "$WORK/gmail-send.json" \
     --provider-response "$WORK/gmail-provider-response.json"
   ```

   This wrapper writes `pending` before provider invocation and stores the
   accepted provider message and thread IDs after success. Repeating a `sent`
   action reconstructs the receipt without sending again. `pending` or
   `uncertain` is never retried automatically.

**If you cannot complete all 5 steps, you cannot send the email. Stop and recover.**

**If you are composing a new subject line, you are doing it wrong. Stop and
recover the original message first. Never compose a standalone email, invent a
generic subject such as `Your inquiry`, or retrieve a recipient from a
name-based customer record. If route construction or reply construction fails,
stop without sending. A customer name may be retained as display-only contact
metadata, but it must never select an estimate, identity, recipient, or thread.**

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
misleading `x/8` score. Wholesale estimates may label customer-visible
product/specification unknowns, but never jeweler cost or pricing assumptions.
Cost assumptions remain owner-only in every mode.

When fields are missing, use `templates/spec-gate-email.md`: one friendly,
price-free, batched request; do not re-ask known facts; offer two real open
slots with timezone. At Stage 1, draft it. At Stage 2 or 3, it must be sent
before the initiating inquiry can be completed.
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
- `route`: original channel, mailbox, recipient email, email-derived
  `identity_key`, immutable Gmail message ID, Gmail thread ID, original RFC
  `Message-ID`, original subject, and existing `References` message IDs
- `specification`: the exact priced written specification
- `proposed_price`
- internal pricing, jeweler cost assumptions, feasibility, appointment options,
  and draft. Keep the internal pricing and assumption fields separate from the
  customer-safe specification; they are owner-only and must never be copied or
  summarized into customer-facing content.

Create the immutable approval request:

```bash
python3 {baseDir}/scripts/approval_guard.py create \
  "$WORK/current-state.json" "$WORK/approval-request.json"
python3 {baseDir}/scripts/kolo_safe.py request-approval-claimed \
  --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
  --message-id '<gmail-id>' --claim-token '<claim-token>' \
  --action-key 'approval_request:<jed-id>:<gmail-id>' \
  --estimate-id '<jed-id>' \
  --details "$WORK/approval-request.json" \
  --session-key '<session-key>'
```

The claimed approval request is the owner-facing Kolo action. Do not add a
second unjournaled `notify-owner` call for the same approval-ready event.

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

1. Fill `templates/customer-emails.md` using only the customer-safe written
   specification and the exact owner-approved price. Do not read from, copy, or
   summarize the internal pricing or jeweler cost assumption fields while
   drafting. Save only the reply body to `$WORK/customer-reply.txt`.
2. Include the canonical high-end/pending-CAD substance from
   `templates/approved-estimate-note.md`, estimated—not guaranteed—lead time,
   validity date, and two or three live appointment options with timezone.
3. Before every customer send on every channel, run the reusable final
   confidentiality guard. Exit 2 blocks the send; rewrite the customer text
   without the confidential material and run it again. Never bypass a failure.

   ```bash
   python3 {baseDir}/scripts/customer_content_guard.py \
     "$WORK/customer-reply.txt"
   ```

4. Call `kolo integration-routing`. For Gmail through Maton, read the
   api-gateway skill. Rebuild `$WORK/route.json` from the exact latest inbound
   Gmail message and confirm it matches the approved route byte-for-byte before
   building the send body with the command below. `gmail_reply.py` is also the
   mandatory final confidentiality guard: it rejects customer text containing
   jeweler cost assumptions or internal pricing language.

   ```bash
   python3 {baseDir}/scripts/gmail_reply.py \
     "$WORK/route.json" "$WORK/customer-reply.txt" "$WORK/gmail-send.json"
   ```

   Send that JSON unchanged through
   `gateway.maton.ai/google-mail/gmail/v1/users/me/messages/send`. The top-level
   `threadId` and the encoded RFC 5322 `In-Reply-To` and `References` headers
   are all mandatory. Do not substitute a newly composed message if building
   the reply fails.
5. Never use the Kolo `message` tool, `deliveryContext.to`, or `kolo:<uuid>` for
   the customer. Use those only for an owner-facing copy or notification.
5. Store the provider's outbound message ID. If the response is uncertain or
   lacks a message ID, do not retry automatically; inspect the Gmail thread or
   escalate to the owner first.

For scheduling, **always query the calendar first** before making any claims
about existing meetings. Use `gws calendar events list` or the Maton gateway
to search for events with the customer's email as an attendee. Never infer
meeting state from email content, subject lines, or scheduling language in
messages.

After confirming no conflicting meeting exists, query live free/busy, intersect
with declared windows, offer specific times, then re-check immediately before
creating an event. Include the customer's email address (from `route.json`
recipient field) as an attendee in the calendar event so they receive the
invitation. Confirm to the customer only after the calendar write succeeds.
Use the owner's IANA timezone, never the pod's UTC clock.

If a rendering is authorized, read `references/rendering-standards.md` first.

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
