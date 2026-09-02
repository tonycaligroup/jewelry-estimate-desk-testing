# Inbox monitor operations

Operator procedures for the inbox monitor: one-time setup and activation,
updating an active monitor (the two-phase reconfiguration), and the two reset
procedures. These are interactive-session procedures. A cron run never
performs them; it reads only the cron phases in SKILL.md.

## One-time setup and activation boundary

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
   `litellm-fireworks/glm-5-3`, no fallbacks, a 900-second timeout,
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

## Updating an active monitor

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

## Resetting customer state for a fresh test

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

## Resetting business setup from scratch

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

