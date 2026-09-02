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
`litellm-fireworks/glm-5-3`, with no fallback. Monitoring crons must use
the same model and `--fallbacks ""`. If Kolo cannot verify the model, stop and
route to the pinned agent.

## Bundled resources

- `scripts/validate_profile.py`: validate runtime shop configuration.
- `scripts/activation_binding.py`: privately bind approvals to the Kolo user
  who installs and activates the skill.
- `scripts/customer_state_reset.py`: clear prior customer/job state for a fresh
  test while preserving shop, pricing, approval, cron, and watermark state.
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
- `scripts/gmail_fetch.py`: perform paginated Gmail discovery and fetch claimed
  messages/threads through fixed Maton requests without model-built commands.
- `scripts/gmail_route.py`: derive the recipient and private customer identity
  key from the exact inbound Gmail message rather than a display name.
- `scripts/rendering_materialize.py`: copy a native Kolo-generated PNG from the
  managed media directory into the claimed canonical rendering path.
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
4. Mode: `retailer`, `wholesale_middle_man`, or `both`.
5. Pricing model: cost-plus multiplier or target margin. For cost-plus, convert
   `25%` to `1.25` and confirm `$1,000 cost → $1,250 quote`. For target margin,
   store the decimal margin and confirm the resulting example price.
6. Whether spot metal pricing is enabled; provider (`stackerscan` or
   `gold-api`), refresh frequency (`per_estimate`, `daily`, or `weekly`), and
   unit. StackerScan is the default and supports grams; gold-api uses troy oz.
7. Requested owner-notification channel: main Kolo chat, email, or SMS, plus
   the destination for email/SMS. Store the request, but set `active_channel`
   to `kolo_chat`: this Kolo release has no supported durable owner-email or
   owner-SMS delivery mechanism. Never attempt or imply those channels are
   active until a future supported integration is configured and tested. This
   request never changes the customer's original-channel routing.
8. Trust stage. Default to Stage 1.
9. Booking mode, IANA timezone, and near-term meeting-offer window. Default the
   window to 7 days so the first meeting is offered ASAP, never near delivery.
10. Optional inbox-monitoring hours and timezone.

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
| 1 — Watch me | Read, calculate, and draft | Every outbound message, booking, and price |
| 2 — Ask questions | Stage 1 plus price-free specification requests | Every booking and price |
| 3 — Book me | Stage 2 plus offer and book inside declared windows | Every price |

Act on scheduling intent immediately at every stage; never delay a meeting
offer until near the desired delivery date. At Stage 1 or 2, prepare the
near-term options for owner action and route the visible action request through
the cron's Kolo-chat result described in Inbox monitoring. Only Stage 3 authorizes autonomous offers,
calendar writes, and confirmations inside
declared windows. Never advance the stage automatically. Missing or unreadable
stage means Stage 1.

## Inbox monitoring

Monitoring is optional and requires `inbox_monitoring.enabled: true`. Search
Gmail through the path returned by `kolo integration-routing`; when it returns
`maton`, read `/opt/kolo-skills/api-gateway/SKILL.md` before using Gmail.

### One-time setup and activation boundary

Never search or act on historical mail during installation. There is no
seven-day bootstrap or other fallback window. Jewelers may already have handled
every pre-activation inquiry manually.

1. Use `sessions_list` once in the interactive activation session to obtain the
   current Kolo session key, then bind that activating user as the automatic
   approver:

   ```bash
   python3 {baseDir}/scripts/activation_binding.py bind-activating-user \
     --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
     --session-key '<current-activation-session-key>'
   ```

   The helper stores the key in a private `0600` runtime binding and refuses to
   replace a different activating user. Never ask for an approver name or email.
   Isolated cron runs do not call `sessions_list`; `workflow_safe.py` loads this
   binding itself when it requests approval.
2. Perform a read-only capability check. Verify that the Gmail integration can:
   use `after:<epoch-seconds>`, return integer `internalDate` epoch milliseconds,
   and enumerate every page until no `nextPageToken` remains. Write a private
   JSON file containing these three fixed booleans:

   ```json
   {"gmail_after_epoch":true,"gmail_internal_date_ms":true,"gmail_complete_pagination":true}
   ```

   If any capability is unavailable, report an unsupported environment and
   leave monitoring inactive. Never weaken the activation boundary.
3. Render the fixed cron message from the bundled template:

   ```bash
   python3 {baseDir}/scripts/cron_config.py render-message \
     --workspace '<absolute-workspace>' --base-dir '{baseDir}' \
     --output "$WORK/cron-message.txt"
   ```

4. Create exactly one disabled `jed-inbox-monitor` using that message. Default
   to every five minutes during the configured business hours in the owner's
   IANA timezone. If the owner requests another interval, use and preserve that
   interval instead; never silently reset an existing owner-selected schedule.
   Use model
   `litellm-fireworks/glm-5-3`, no fallbacks, a 420-second timeout,
   `lightContext: true`,
   `toolsAllow: ["exec", "read", "write", "image_generate"]`, an isolated
   session, and Kolo owner announcement delivery. Never enable or manually run
   it yet. If a job with that name
   already exists, stop and use the reconfiguration procedure below; never
   create a second job.
5. Re-read the disabled job from Kolo into private JSON and derive its stable
   binding:

   ```bash
   python3 {baseDir}/scripts/cron_config.py bind-live \
     --job "$WORK/live-cron.json" \
     --workspace '<absolute-workspace>' --base-dir '{baseDir}' \
     --output "$WORK/cron-binding.json"
   ```

   The binding includes job ID, agent when Kolo's native export exposes one,
   schedule, timezone, session, wake mode,
   complete prompt, model, fallbacks, timeout, light-context setting, exact
   required tool allow-list, optional thinking field, and delivery destination.
   Generated timestamps and runtime counters are excluded. `enabled` is also excluded
   because it is a lifecycle flag, but it must be false at this step and true
   only after activation.
6. Prepare durable state under an atomic setup lock:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py prepare \
     --capabilities "$WORK/capabilities.json" \
     --cron-config "$WORK/cron-binding.json"
   ```

7. Activate only against that exact verified binding, then enable the same job
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
Before reconfiguration, run `activation_binding.py status` against the monitor
root. For an older installation with no binding, use `sessions_list` in the
current interactive setup conversation and run `bind-activating-user` once;
that Kolo user becomes the automatic approver. Never make the isolated cron
discover or select an approver. Then edit the existing job ID in place:

If an operator already edited the disabled live cron before
`reconfigure-prepare`, do not manually synchronize state and do not revert the
cron merely to recreate the sequence. Use the bundled recovery command only
after re-reading the live job into private JSON:

```bash
python3 {baseDir}/scripts/inbox_monitor.py reconfigure-adopt-disabled-live \
  --current-cron-config "$WORK/cron-binding.json" \
  --live-job "$WORK/live-cron.json" \
  --workspace '<absolute-workspace>' --base-dir '{baseDir}'
```

It fails unless the live cron is disabled, canonical, the same job ID, no
reconfiguration is pending, and the supplied prior binding still matches the
durable bound hash. It updates only the bound cron hash; activation time,
watermark, queue, claims, and records are preserved.

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

### Resetting customer state for a fresh test

Only when the activating Kolo user explicitly requests a clean test reset:

1. Disable the existing inbox cron and verify `enabled: false`. Do not delete or
   replace the cron.
2. Run the bundled reset helper:

   ```bash
   python3 {baseDir}/scripts/customer_state_reset.py \
     --workspace '<absolute-workspace>' --confirmed-cron-disabled
   ```

   It validates the shop profile, activation binding, and active monitor state;
   advances the Gmail discovery watermark to the reset time; and removes local
   estimate records, claims, queue/manual-review items, customer work artifacts,
   and abandoned run directories. It preserves the complete shop profile and
   pricing data, activation binding, monitor activation/binding state, cron job,
   schedule, and non-customer configuration. It refuses unknown directory
   shapes rather than deleting them.
3. Enumerate every page of owner-visible Kolo mirrors:

   ```bash
   kolo record-list --record-type skill.jewelry_estimate --page-size 200
   ```

   For every exact opaque `external-id` returned, run the explicitly destructive
   erasure requested by the user:

   ```bash
   kolo record-delete --record-type skill.jewelry_estimate \
     --external-id '<exact-external-id>' --hard
   ```

   Re-list every page and require zero remaining records. `--hard` is immediate
   and irreversible; never run it without the explicit clean-reset request.
   Kolo action/audit logs are append-only, non-PII historical traces and are not
   deleted; they do not own or route active customer work.
4. Leave the cron disabled until the user says the next test may begin.

### Resetting business setup from scratch

Only when the activating Kolo user explicitly requests a complete business
setup reset, first complete the customer-state reset above and leave the same
cron disabled. Then run:

```bash
python3 {baseDir}/scripts/business_state_reset.py \
  --workspace '<absolute-workspace>' --confirmed-cron-disabled
```

The helper refuses to run unless customer records, claims, queue items, and
customer work are already empty. It replaces the runtime shop profile with the
unconfigured bundled template, removes the activating-owner binding and private
spot-price caches, and returns the monitor to `prepared`. It preserves the
installed skill, durable cron binding and job identity, disabled live cron
configuration, and Gmail account authorization. Do not delete these files or
edit their JSON directly. After the reset, conduct the normal first-time setup
questions, bind the current Kolo user as approver, verify the live cron binding,
activate the monitor with a fresh forward-only watermark, and enable the cron
only when setup is complete and the user is ready.

### Cron discovery phase

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

Repeatedly call `inbox_monitor.py claim-next`. It returns the oldest eligible
message already claimed and synchronized, or `null`. The helper permits only
the oldest unfinished item in each Gmail thread; a stuck thread does not block
other threads.

For each returned message:

1. Select and claim the immutable Gmail message ID before fetching content,
   notifying, drafting, sending, or mutating a Kolo estimate:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py claim-next \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --stale-after-seconds 600
   ```

   This command selects, claims, synchronizes the queue, and creates the only
   supported persistent per-claim work directory in one deterministic call.
   Use the returned `work_paths.work_dir` as `$WORK` and each returned named
   artifact path exactly. Never create `.jed-work`, `/tmp/jed-work`, or another
   ad-hoc work directory. Never inspect `claim.json`, `state.json`, or queue
   files, and never use a generic show/display/read-back tool for generated
   artifacts; pass their returned paths directly between bundled scripts.
   `claim-next` returns exit 0 for both `claim.acquired:true` and
   `claim.acquired:false`.
   A duplicate `processed` or `manual_review` claim completes the queue item. A
   duplicate recent `processing` claim remains owned by the earlier run and
   receives no side effects. A stale claim is resumed with its original token
   only when its phase journal proves every external action is settled. A claim
   receives at most one automatic retry at the same phase; a second stale
   occurrence becomes manual review instead of refreshing its progress clock.
   Legacy, retry-exhausted, or delivery-ambiguous stale claims become manual
   review; never steal or automatically retry them.
   After an operator independently verifies that a legacy claim caused no
   external action, it may be journaled once with
   `inbox_claim.py authorize-legacy-resume --message-id '<gmail-id>'
   --claim-token '<token>' --minimum-age-seconds 600
   --confirmed-no-external-actions`. Never put this command in the cron runbook
   or infer the confirmation from an empty legacy state file.
2. Only for `claim.acquired:true`, fetch the full Gmail message and run the conservative
   deterministic header classifier before involving the LLM. Fetch only with:

   ```bash
   python3 {baseDir}/scripts/gmail_fetch.py fetch-claimed \
     --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --message-id '<gmail-id-returned-by-claim-next>'
   ```

   This writes the complete message and thread only to the authoritative
   `work_paths.gmail_message` and `work_paths.gmail_thread`. Never construct a
   Gmail read request or choose an output path. Then run:

   ```bash
   python3 {baseDir}/scripts/gmail_classify.py "$WORK/gmail-message.json"
   ```

   - `auto_reply`: complete as processed with no response or owner alert, using
     `kolo_safe.py complete-claimed` so the helper resolves the authoritative
     token, advances the no-side-effect phase, reconciles the queue, and cleans
     claim work atomically.
   - `dsn_candidate`: correlate only against durable stored outbound provider
     evidence and the exact failed-recipient email. Never trust an estimate ID
     found only in message text. A verified failure becomes `manual_review` with
     reason `delivery_failed` and an event-specific owner alert. An uncorrelated
     DSN becomes `manual_review` with reason `uncorrelated_dsn`; never treat it as
     a customer. When the isolated cron has no bundled deterministic correlation
     result, it must choose `uncorrelated_dsn` rather than inspect raw records or
     guess.
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
     --message-id '<gmail-id>' --phase routed
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
5. For a valid response on an active estimate, normally notify the owner at
   every trust stage before any customer response. Bind the alert to an
   event-specific key that includes the event, estimate ID, and Gmail ID:

   ```bash
   python3 {baseDir}/scripts/kolo_safe.py notify-owner-claimed \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --message-id '<gmail-id>' \
     --notification-key 'customer_replied:<jed-id>:<gmail-id>' \
     --event customer-replied --estimate-id '<jed-id>'
   ```

   The wrapper writes `pending` before invoking Kolo and records `sent` after
   successful CLI acceptance; `sent` is not an independent user-visible
   delivery receipt. Because Kolo has no delivery-receipt query, any command
   failure after invocation is `uncertain`, never a retryable pre-delivery
   failure. A process crash may leave `pending`; the stale reconciler marks it
   `uncertain` after 600 seconds. Never resend `pending` or `uncertain` alerts.
   A `customer-replied` notice is not an approval request. If approval creation
   later fails, say explicitly that the reply notice was sent but no approval
   request was created.
   Generic mailbox alerts tied to a claimed message must never call
   `notify-monitor` directly.

   Exception: when a Stage 1 or 2 reply contains appointment intent, do not use
   the generic `customer-replied` notification as the appointment route and do
   not rely on the cron's final chat delivery. Write this exact-shape private
   artifact to `work_paths.appointment_intent`:

   ```json
   {
     "requested_times": [],
     "calendar_availability": []
   }
   ```

   Preserve customer-provided timing in `requested_times`. Add only live,
   validated slots to `calendar_availability`, each with exactly `start`, `end`,
   and the owner-timezone `label`; either array may be empty. Then run:

   ```bash
   python3 {baseDir}/scripts/workflow_safe.py request-appointment-approval \
     --monitor-root '<workspace>/estimate-desk/inbox-monitor' \
     --claim-root '<workspace>/estimate-desk/inbox-claims' \
     --record-root '<workspace>/estimate-desk/records' \
     --message-id '<claimed-gmail-id>' --estimate-id '<jed-id>' \
     --appointment-intent '<work_paths.appointment_intent>' \
     --appointment-approval '<work_paths.appointment_approval>' \
     --record-output '<work_paths.current_record>'
   ```

   This binds the activating Kolo user, the email-derived customer and thread,
   and the claimed source message into one durable, retry-safe approval. It
   records the approval before finalizing an appointment-only claim. Run
   `assert-settled` and return `NO_REPLY`; the approval remains visible and
   actionable even if cron announce delivery fails. For a message that also
   requests rendering, add `--defer-finalize-for-rendering`, then complete
   `send-rendering`, which finalizes the claim. Never claim the appointment is
   booked until approval is granted and the live calendar write succeeds.
6. Before deciding which specifications are missing or complete, fetch the
   exact Gmail thread resource and read every message in chronological order,
   including the initiating inquiry and all later customer replies. Never
   evaluate only the newest message and never treat the current record as a
   substitute for missing thread content. Build `$WORK/thread-review.json`
   directly from that exact thread with:

   - `thread_id`: the owned Gmail thread ID;
   - `source_message_id`: the currently claimed inbound Gmail ID;
   - `message_ids`: every Gmail message ID in the fetched thread, in
     chronological order, including shop messages;
   - `specification`: for an estimate that has not been sent, one normalized
     customer-safe specification merged from the complete thread; after an
     estimate is sent, omit this field because the helper preserves the
     immutable approved specification from the authoritative record;
   - `missing_required_fields`: only the applicable Phase 1.5 field keys still
     absent after the merge, or an empty array when complete; and
   - `post_estimate_artifact`: only after an estimate is sent, an exact-shape
     object with `design_change_assessment` (`unchanged`, `changed`, or
     `uncertain`), unique `intents` chosen from `estimate_acceptance`,
     `rendering_request`, and `appointment_request`, and `changed_fields`.
     `changed_fields` must be nonempty only for `changed`; use `uncertain`
     whenever the newest customer wording might alter the approved design but
     cannot be mapped confidently to a specification field. Include every
     explicit intent. A combined unchanged rendering-and-appointment request
     is exactly `{"design_change_assessment":"unchanged","intents":
     ["rendering_request","appointment_request"],"changed_fields":[]}`.

   Persist this review before drafting, pricing, requesting approval, or
   finalizing:

   ```bash
   python3 {baseDir}/scripts/estimate_record.py record-thread-review \
     --estimate-id '<jed-id>' --snapshot "$WORK/thread-review.json" \
     --shop-profile '<absolute-workspace>/estimate-desk/shop-profile.json' \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --output "$WORK/current-record.json"
   ```

   The helper stores normalized specifications plus hashes and counts, not raw
   email bodies or later provider message IDs. It fails if the initiating or
   currently claimed message is absent. For a post-estimate reply it derives
   the approved-specification hash from the authoritative record, never from
   model output, and records the bounded intent decision. Then run:

   ```bash
   python3 {baseDir}/scripts/workflow_safe.py finalize-post-estimate \
     --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --message-id '<gmail-id>' --estimate-id '<jed-id>' \
     --record-output "$WORK/current-record.json"
   ```

   This command mirrors the review, finalizes acknowledgements, terminalizes
   changed, uncertain, or malformed classifications to manual review, and
   returns the only allowed next action for rendering and appointment intents.
   Malformed evidence stores only bounded structural error codes, never the
   raw model artifact or customer content.
   Do not retry a deterministic classification with rewritten JSON. The later
   appointment and rendering commands revalidate that the persisted decision
   belongs to the same estimate and claimed Gmail message before acting. Do not
   re-ask any field present anywhere in the thread.

   If `missing_required_fields` is nonempty, follow the specification-request
   branch and persist its same-source send receipt. Treat that returned list as
   authoritative: never rewrite or retry the same source-message thread review
   to clear, replace, or invent a returned missing field. If it is empty, continue
   through internal pricing and the claimed owner-approval request in the same
   run. Notification alone is never a completed customer reply.
7. Complete normal branches only through `workflow_safe.py`; do not split
   delivery, persistence, mirroring, phase advancement, or finalization.

   For missing specifications, first write the price-free body to the returned
   customer-reply path, then run:

   ```bash
   python3 {baseDir}/scripts/workflow_safe.py send-spec-followup \
     --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --message-id '<gmail-id>' --estimate-id '<jed-id>' \
     --route "$WORK/route.json" --body "$WORK/customer-reply.txt" \
     --gmail-payload "$WORK/gmail-send.json" \
     --provider-response "$WORK/gmail-provider-response.json" \
     --record-output "$WORK/current-record.json" \
     [--initiating]
   ```

   Use `--initiating` only when the claimed message created the estimate.
   Otherwise the helper appends later follow-up evidence. Provider ambiguity
   is never retried.

   For complete specifications, build the exact current state and owner-only
   cost sheet, then use the `workflow_safe.py request-approval` command in
   Phase 3. It creates the binding, requests approval, persists `pending_approval`,
   mirrors the record, and finalizes the inbound claim.

   For every manual-review decision, persist the terminal claim and queue state
   before attempting the one privacy-safe owner notification:

   ```bash
   python3 {baseDir}/scripts/kolo_safe.py manual-review-claimed \
     --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
     --message-id '<gmail-id>' \
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

   Then derive the owner-facing summary from durable state rather than writing
   it yourself:

   ```bash
   python3 {baseDir}/scripts/inbox_monitor.py run-report --announce \
     --claim-root '<absolute-workspace>/estimate-desk/inbox-claims'
   ```

   `--announce` suppresses a report identical to the last one announced, so an
   open review is raised once rather than every few minutes. A `NO_REPLY`
   message means there is nothing new to tell the owner, not that nothing is
   outstanding.

   Deliver its `message` field verbatim as the entire announcement. Do not
   compose, summarize, reword, or add to it, and never announce an outcome that
   did not come from this command. A run that reports something it did not
   observe is worse than a run that fails, because the owner and every later
   session treat the announcement as evidence.

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
misleading `x/8` score. Wholesale estimates may label customer-visible
product/specification unknowns, but never jeweler cost or pricing assumptions.
Cost assumptions remain owner-only in every mode.

Evaluate these fields against the merged full-thread specification recorded in
Inbox monitoring step 6. A fact supplied in the initiating inquiry or any
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

Use slots only from the validated `work_paths.calendar_options`. At Stage 1,
draft the specification request. At Stage 2 or 3, send it automatically
with `workflow_safe.py send-spec-followup`; never ask the owner whether to
draft, send, or continue routing.
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

For cron processing, use only the deterministic per-claim work directory and
artifact paths returned by `inbox_monitor.py claim-next`. For an interactive
non-cron estimate, create a private temporary directory with Python
`tempfile.mkdtemp()` or `mktemp -d`. Directory permissions must be `0700`,
contained files `0600`, and the name must not contain customer data. Generate
an opaque ID:

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
- `cost_components`, containing exactly `metal_lines`, `stone_lines`,
  `labor_lines`, and `other_hard_cost_lines`. Use these exact line shapes:
  `{metal, rate_key, quantity_grams, unit_cost}`,
  `{stone, rate_key, quantity, unit_cost}`, `{task, hours, rate}`, and
  `{label, rate_key, total_cost}`. Every `rate_key` must name an entry the
  owner configured in `pricing`: `metal_per_gram`, `stones_per_carat`, and
  `fees` respectively, and the unit cost must equal that configured rate.
  Labor `rate` must equal `bench_labor_per_hour`. When `pricing.spot_metal` is
  enabled a metal line instead uses `rate_key` naming the spot metal plus
  `spot_price_per_gram` and `purity`, and its `unit_cost` must equal
  `spot_price_per_gram` times `purity`, with the spot figure matching the
  recorded spot price evidence. If a required rate is not configured, never
  substitute, estimate, or infer one: fail closed to manual review with reason
  `invalid_cost_components` and let the owner add the rate. Do not add line totals,
  `hard_cost_total`, or `customer_price`; the approval helper calculates and
  inserts them deterministically into the owner-only `internal_cost_sheet`.
- internal pricing, jeweler cost assumptions, feasibility, appointment options,
  and draft. Keep the internal pricing and assumption fields separate from the
  customer-safe specification; they are owner-only and must never be copied or
  summarized into customer-facing content.

Create and deliver the immutable approval request with the single high-level
command documented in Inbox monitoring. Do not call approval, Kolo, record, or
finalization helpers separately. The binding includes the exact proposed price
and complete owner-only cost sheet as well as estimate ID, route, and
specification.

```bash
python3 {baseDir}/scripts/workflow_safe.py request-approval \
  --monitor-root '<absolute-workspace>/estimate-desk/inbox-monitor' \
  --claim-root '<absolute-workspace>/estimate-desk/inbox-claims' \
  --record-root '<absolute-workspace>/estimate-desk/records' \
  --message-id '<gmail-id>' \
  --estimate-id '<jed-id>' \
  --current-state "$WORK/current-state.json" \
  --approval-request "$WORK/approval-request.json" \
  --shop-profile '<absolute-workspace>/estimate-desk/shop-profile.json' \
  --record-output "$WORK/current-record.json"
```

The command succeeds only when the claimed Kolo action, authoritative local
record, Kolo mirror, claim phases, and queue finalization are complete.
It always replaces any model-produced route and specification with the
authoritative estimate record before requesting approval. If a retry finds an
existing `approval-request.json`, it validates and reuses that exact artifact;
never delete or rewrite it to force a retry.

The claimed approval request is the owner-facing Kolo action. Do not add a
second unjournaled `notify-owner` call for the same approval-ready event.

The command loads the activating Kolo user's private approval binding. The
owner notification contains only the opaque estimate ID. Never insert customer
text into CLI arguments.

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

### Handling approved appointment requests in the main Kolo session

When Kolo delivers an approved execution payload whose `action_type` is
`appointment_booking`, the main session must continue the existing estimate,
not create a new inquiry:

1. Load the local record named by `estimate_id`. Verify its customer email and
   Gmail thread against the execution payload with the existing
   email-identity and route-ownership rules. The local record remains
   authoritative; stop if any value differs.
2. Query Google Calendar again with `calendar_query.py`. Approval does not make
   stale availability current. Use the same routed calendar integration
   configured during activation for the event write, and include the
   authoritative customer email as an attendee.
3. If one approved time remains unambiguous and free, create the event through
   that calendar integration. Only after the provider returns a successful
   event ID may the record advance to `appointment_booked` and the customer be
   told that the appointment is confirmed.
4. If no single time was approved or the approved time is no longer free, use
   `appointment_options.py` to create two or three fresh near-term options.
   Send those options through the same authenticated Gmail reply route used by
   the estimate, in the authoritative original thread. Do not write an event
   or claim a booking yet.
5. Before a new calendar write, stop if the authoritative record already has an
   `appointment_booked` receipt; do not create another event or send another
   confirmation. After a successful calendar write and confirmation in the
   authoritative Gmail thread, write a private JSON receipt containing exactly
   `estimate_id`, `source_message_id`, `calendar_event_id`, `confirmed_start`,
   `confirmed_end`, `confirmation_message_id`, and `confirmation_thread_id`,
   then run:

   ```bash
   python3 {baseDir}/scripts/workflow_safe.py record-appointment-booked \
     --record-root '<absolute-workspace>/estimate-desk/records' \
     --estimate-id '<jed-id>' --receipt '<private-receipt-json>' \
     --record-output '<private-current-record-json>'
   ```

   The helper matches the source-message hash to the durable appointment
   approval, validates the original Gmail thread, atomically stores the event
   and confirmation receipt, and mirrors the `appointment_booked` record.
   Identical retries are no-ops and conflicting receipts fail closed. Never
   expose the approval payload or internal estimate data to the customer. This
   narrow receipt fix does not close the provider-action crash window before
   receipt persistence; treat such an unverified retry as manual work rather
   than risking a duplicate booking or confirmation.

After an estimate is sent, treat an explicit customer request for a visual
rendering as an in-scope continuation of that estimate. It does not require
another owner approval. Read `references/rendering-standards.md` first, bind
the request to the existing email-derived record and Gmail thread, and never
interpret “rendering” as a request for a manufacturing file. Each distinct
customer request may create one new rendering iteration; replaying the same
Gmail message must not create or send another iteration.

Generate exactly two complementary-view PNG illustrations of the same approved
design in parallel with Kolo's native `image_generate` tool; never ask for
alternate design proposals. The tool completes asynchronously. Until both
completion events arrive or the bounded budget is exhausted, keep the isolated
cron session active by running exactly:

```bash
python3 {baseDir}/scripts/rendering_wait.py wait \
  --monitor-root '<workspace>/estimate-desk/inbox-monitor' \
  --claim-root '<workspace>/estimate-desk/inbox-claims' \
  --message-id '<claimed-gmail-id>'
```

After each wait returns, collect any completion events already delivered to
this same session. Otherwise repeat the exact wait command. The helper permits
at most eight 30-second waits. A pending rendering is not a valid final
response: never promise to send it later, return success, or leave the claim
processing.

Compare each completed candidate with the immutable approved specification and
the structural and `DO NOT CHANGE` constraints in
`references/rendering-standards.md`. Discard any candidate that changes the
silhouette, rail or shank layout, setting topology, stone location or coverage,
requested view, or another explicit feature. When both events arrive, continue
with one conforming image if the other is wrong, or both when both conform. If
the eighth wait returns `exhausted:true`, continue with at least one completed,
conforming candidate rather than blocking on the other. If no completion event
supplied a usable managed-media path, run the manual-review command exactly,
using `rendering_generation_timeout` when no candidate completed or
`rendering_validation_failed` when completed candidates were nonconforming:

```bash
python3 {baseDir}/scripts/kolo_safe.py manual-review-claimed \
  --monitor-root '<workspace>/estimate-desk/inbox-monitor' \
  --claim-root '<workspace>/estimate-desk/inbox-claims' \
  --message-id '<claimed-gmail-id>' \
  --reason-code '<rendering_generation_timeout-or-rendering_validation_failed>'
```

Then run `assert-settled` and return a concise real failure. Do not send a
customer email or an appointment-success message.

For each conforming completion event, run the bundled materializer; never use
`cp`, `mv`, `curl`, or a generic write tool for image bytes:

```bash
python3 {baseDir}/scripts/rendering_materialize.py \
  --monitor-root '<workspace>/estimate-desk/inbox-monitor' \
  --claim-root '<workspace>/estimate-desk/inbox-claims' \
  --message-id '<claimed-gmail-id>' \
  --source '<image_generate-managed-media-path>' --slot 1
```

Use `--slot 2` for a second conforming candidate. The script accepts only a
regular PNG inside Kolo's managed media directory and writes it atomically to
the claim's returned canonical rendering path.
Write the post-estimate visual-rendering note from
`templates/customer-emails.md` to `work_paths.customer_reply`, then call:

```bash
python3 {baseDir}/scripts/workflow_safe.py send-rendering \
  --monitor-root '<workspace>/estimate-desk/inbox-monitor' \
  --claim-root '<workspace>/estimate-desk/inbox-claims' \
  --record-root '<workspace>/estimate-desk/records' \
  --message-id '<claimed-gmail-id>' --estimate-id '<jed-id>' \
  --body '<work_paths.customer_reply>' \
  --image '<work_paths.rendering_image_1>' \
  --gmail-payload '<work_paths.gmail_payload>' \
  --provider-response '<work_paths.gmail_provider_response>' \
  --record-output '<work_paths.current_record>'
```

When both candidates conform, add a second
`--image '<work_paths.rendering_image_2>'`. Do not send if image generation does
not produce at least one supported, conforming local file at a returned claim
path. The high-level command binds the send to the claimed customer message,
attaches the selected file or files to the original Gmail thread, records
immutable provider/image evidence, mirrors the record, and finalizes the claim.

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
sent, or a test artifact), run `scripts/estimate_record.py retire` with
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
