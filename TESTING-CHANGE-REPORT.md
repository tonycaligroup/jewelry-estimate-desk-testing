# Jewelry Estimate Desk Testing Change Report

Date: 2026-08-27

## Completed in this update

- Made the Kolo user who installs and activates the skill the automatic
  approver. Setup no longer asks for an approver name or email.
- Added a private activation binding for that user's durable Kolo session key.
  The isolated cron no longer needs `sessions_list`, and the LLM-facing
  approval command no longer accepts a session-key argument.
- Restored the immediate stop/pause/hold rule to the main runtime instructions.
- Reconciled trust-stage scheduling: scheduling intent is handled immediately,
  but only Stage 3 permits autonomous offers, calendar writes, and confirmations.
- Reconciled specification policy across the skill, cron, and customer
  templates: budget and event date are optional; delegated quality choices are
  complete; shop sourcing is the default unless the customer says otherwise.
- Clarified that a customer-reply notification is not an approval request.
- Reconciled first-job pricing guidance: unsupported rates remain blank instead
  of being replaced with invented market defaults.
- Added an explicit clean-test reset that removes local customer records,
  claims, queued/manual-review state, and job artifacts; advances the discovery
  watermark; and hard-deletes Kolo estimate mirrors while preserving business,
  pricing, approver, cron, and monitor configuration.

- Removed customer-facing use of the term `CAD`. Customer messages now use
  `design`, `final design`, or `visual rendering`.
- Added a final customer-content guard that blocks the prohibited term as well
  as all existing jeweler-only cost, assumption, markup, margin, and vendor
  disclosures.
- Treats customer-delegated stone quality choices as complete specifications.
  Budget is optional and shop sourcing is the default unless the customer says
  otherwise.
- Restored Kolo's native `image_generate` capability to the isolated inbox cron.
- Added a deterministic post-estimate rendering delivery command. It:
  - requires an already-sent estimate;
  - uses the original email-derived customer and Gmail thread;
  - supports one or two PNG, JPEG, or WebP attachments;
  - uses the claimed customer Gmail message as the idempotency key;
  - records image hashes, provider message ID, provider thread ID, iteration,
    and send time;
  - treats the same Gmail message as an idempotent replay rather than a new
    rendering request;
  - finalizes the inbox claim only after the send evidence and estimate record
    are durable.
- Added canonical persistent work paths for Gmail payloads and rendering images
  so the cron must not invent `.jed-work` or temporary folders.
- Added a bundled rendering materializer that accepts only native Kolo PNG files
  from the managed media directory and atomically copies them into the claimed
  canonical rendering slot.
- Prevented internal reasoning or loop-control text from becoming a Kolo cron
  announcement.
- New inbox-monitor installations default to every five minutes during the
  owner's configured business hours. A user-requested alternate interval is
  preserved during later reconfiguration.
- Replaced the one-shot cron chat result for Stage 1/2 appointment requests
  with a durable Kolo approval assigned to the activating user. The approval
  is bound to the authoritative estimate, customer email, Gmail thread, source
  message, requested timing, and any validated calendar availability.
- Appointment-only messages now finalize only after the approval request is
  journaled, persisted locally, and mirrored to Kolo. Combined rendering and
  appointment requests create the approval first, then let the existing
  rendering transaction finalize the claim.
- Approved appointment actions re-check live calendar availability and remain
  in the original Gmail thread. No appointment is described as booked until
  the calendar write succeeds.

## Verification

- 168 automated runtime tests pass.
- Python compilation passes for all scripts and tests.
- Git whitespace validation passes.
- No customer-facing template or instruction contains the prohibited term.
- Tests cover same-thread replies, email-derived customer identity, rendering
  MIME attachments, one/two-image limits, duplicate-request idempotency,
  distinct rendering iterations, owner-selected cron intervals, confidential
  customer content, delegated specification choices, private activation
  binding, installer-as-approver routing, cross-document policy coherence, and
  customer-state reset preservation/refusal boundaries. New coverage verifies
  durable appointment approval construction, activating-user binding,
  write-ahead idempotency, authoritative email/thread routing, record evidence,
  and combined rendering/appointment ordering.

## Kolo platform findings

- The earlier working skill and the current end-to-end test both used Kolo's
  native `image_generate` tool successfully.
- Recent deterministic cron hardening removed that tool from `toolsAllow`; this
  update restores only that previously used capability.
- Gmail's existing raw MIME send route supports attachments and preserves the
  provider thread ID; the skill now constructs and journals that attachment
  delivery itself.
- Brief #56's structured approval event could not be retrieved. It remains
  unproven and must not be used as authorization for a recovery send. Start the
  next end-to-end test as a new inquiry.
- Kolo changed its installed `kolo_safe.py` directly during analysis. That live
  edit is not part of this reviewed source and must be replaced by reinstalling
  the GitHub version. Returning success for an uncertain external action would
  hide a real unresolved state, so this update does not adopt that edit.

## Deployment state

- The durable appointment-approval change is currently source-reviewed and
  tested in the testing-skill branch; it has not yet changed the installed Kolo
  copy or the live cron.
- Installation must update the testing skill from the reviewed GitHub commit
  and rebind the existing cron prompt while preserving the user's selected
  interval and enabled state.
