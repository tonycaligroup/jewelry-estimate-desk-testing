# The estimate record

One JSON file per inquiry at `estimate-desk/records/<estimate_id>.json`, written only through the functions in `scripts/estimate_record.py` (every write goes through `upsert_record`, which validates the route, keeps append-only lists append-only, and refuses to change approval-bound state after a card was filed). Nothing else, and nobody in the Kolo chat, edits a record by hand. `tests/test_record_schema.py` checks that every key named here is still written by the code and that no record key in the code is missing from this page.

`estimate_id` is `jed-` plus the first sixteen hex characters of the SHA-256 of the first Gmail message id, so the same first message always maps to the same record. `schema_version` is 1.

## Status

`status` is one of `awaiting_specs`, `pending_approval`, `estimate_sent`, `appointment_booked`, `approved`, `declined`, `manual_review`, `dormant`. A record starts as `awaiting_specs`; a filed price card sets `pending_approval`; the approved send sets `estimate_sent`; a confirmed booking sets `appointment_booked` (a cancelled booking goes back to `estimate_sent`); retirement sets `dormant`; a change request, a revival, or an owner's spec change sets it back to `awaiting_specs` and bumps `revision`.

## Keys, in the order they are usually written

Writer names are the `estimate_record` function, with the command that calls it: **tick** = `pipeline.py` (the watcher's per-message run), **execute** = `workflow_safe.py` executors (approved briefs, desk-answers, sends), **sheet** = `sheet_mirror.py` (the Google Sheet pull), **owner** = `owner_questions.py`.

| Key | Shape | Written by | When |
|---|---|---|---|
| `schema_version`, `estimate_id`, `status`, `route`, `inbound_timestamp_ms` | route = `{channel: "gmail", thread_id, identity_key, gmail_message_id, recipient, ...}` | `build_initial_record` (tick) | the first message of a thread becomes a record |
| `route_history` | list of earlier routes with `moved_at` | `upsert_record` | the thread moves (a reply from a new address on the same conversation) |
| `specification` | one dict of the fields in `judge.SPEC_KEYS` (piece_type, quantity, metal, metal_karat, metal_color, stone_*, accent_*, earring_style, setting_style, finger_size, dimensions, finish, engraving, event_date, budget, scheduling_intent, ...) plus `pieces` for multi-piece orders | tick, after `ledger.absorb`; `apply_owner_spec_changes` (execute) | every reading of the thread; the ledger (`ledger.sqlite`) keeps each fact's source and message |
| `missing_required_fields` | list of field names | tick; reset to `[]` on reopen, revive, owner change | after each reading; empty once the spec is complete or the jeweler chooses |
| `thread_reviews` | append-only `{thread_id, thread_message_count, missing_required_fields, outcome, design_change_assessment, intents, changed_fields, source_message_id}` | `record_thread_review` (execute, from tick's review step) | each customer message reviewed; a conflicting review for the same message is refused unless concierge details replaced it |
| `spec_gate_reply` | `{status: "sent", provider_message_id, thread_id, awaiting_specs}` | `record_spec_gate_sent` (execute) | the first questions email went out |
| `followup_replies` | append-only `{status, provider_message_id, thread_id, awaiting_specs}` | `record_followup_sent` (execute) | each later questions email |
| `inventory_inquiry` | `{since_gmail_message_id, note, marked_at}` | `mark_inventory_inquiry` (tick) | the customer asked for a ready-made piece; the desk offers a visit instead of pricing |
| `prior_piece` | `{on_file, estimate_id?, facts?, owner?, ...}` | `mark_prior_piece` (tick; execute on the owner's details answer) | "you made this for me": the piece on file, or the owner's answer, is the base |
| `concierge` | `{mode, asked, offered, details, renderings, ...}` | `mark_concierge` (tick, execute) | concierge mode: what was asked, the owner's details, the renderings attached |
| `meeting` | `{kind: "visit"\|"call", phone?, at}` | `note_meeting` (execute) | a booking with a phone call, and the number the customer gave |
| `customer_overrides` | `{name?, phone?, notes?, ..., at}` | `save_customer_overrides` (sheet); `note_meeting` for the phone | the owner edited the Customers tab; a customer wrote their number |
| `times_offered` | append-only `{provider_message_id, options: [{start, end, label}], ...}` | `record_times_offered` (execute) | an offer-times brief went out |
| `appointment_approval_requests` | append-only `{status: "pending_approval", ...card facts}` | `record_appointment_approval_requested` (execute) | a booking card was filed |
| `appointment_booked` | `{status, confirmed_start, confirmed_end, calendar_event_id, confirmation_message_id, confirmation_thread_id, booked_at, before_estimate}` | `record_appointment_booked` (execute) | the confirmation was sent and the event created |
| `appointment_history` | append-only earlier bookings with `replaced_at`, `replaced_by` | `record_appointment_booked` | a booking replaced another (reschedule) |
| `appointments_cancelled` | append-only `{...booking, cancelled_at, note}` | `record_appointment_cancelled` (execute) | the customer cancelled; status returns to `estimate_sent` |
| `sheet_draft` | `{status: pending\|ready\|quoted, details, lines, hash, at, acted_hash?}` | `save_sheet_draft` (sheet); `mark_sheet_draft_acted` (execute) | the owner typed into the Cost sheet; the desk acted on a "ready" block |
| `owner_quantities` | `{line_key: quantity}` | `save_sheet_draft` (sheet) | the owner's quantities on the Cost sheet override the desk's |
| `one_time_rates` | `{rate_key: number}` | `set_one_time_rate` (execute) | a missing-rate answer said "use N once" (not saved to the profile) |
| `owner_spec_changes` | append-only `{changes, question_id, at}` | `apply_owner_spec_changes` (execute) | the owner corrected the spec from a question |
| `proposed_price`, `internal_cost_sheet` | number; `{lines, hard_cost_total, margin, ...}` from `cost_components` | `prepare_approval_state` (execute, via `price`) | the desk priced the complete spec |
| `owner_price` | `{price, previous_price, desk_price, hard_cost_total, margin, expected_margin, question_id, set_at}` | `record_owner_price` (execute) | the owner named a different price |
| `approval_requests` | append-only `{brief_id?, binding_hash, source_message_id, ...}` | `record_approval_requested` (execute) | a price card was filed; also sets `approval_binding_hash`, `approval_source_message_id`, status `pending_approval` |
| `approval_binding_hash`, `approval_source_message_id` | sha256 of `{estimate_id, route, specification, proposed_price, internal_cost_sheet}`; the message id | `record_approval_requested` | immutable while a card is open: any change to bound state is refused |
| `rejected_approval_bindings` | append-only hashes with notes | `reject_approval` and friends | a card was rejected, or the state was reopened while bound |
| `estimate_delivery` | `{status: "sent", approval_binding_hash, approved_price, provider_message_id, thread_id, sent_at}` | `record_estimate_sent` (execute) | the approved estimate email went out; immutable evidence |
| `approved_price`, `outbound_provider_message_id` | number; Gmail message id | `record_estimate_sent` | same moment |
| `estimate_history` | append-only `{revision, specification, proposed_price, reopened_for, note, reopened_at}` | `reopen_for_change` (execute) | a change request reopened a sent estimate |
| `revision`, `reopened_for` | integer from 0; the kind of change | `reopen_for_change`, `revive`, `apply_owner_spec_changes` | each reopen |
| `rendering_revisions` | append-only `{note, pieces, round, at}` | `record_rendering_revision` (execute) | the customer asked for a change to the renderings |
| `rendering_deliveries` | append-only `{status: "sent", provider_message_id, thread_id, ...}` | `record_rendering_sent` (execute) | approved renderings went out |
| `retirement` | `{reason, retired_at, previous_status, note?}`; reason in `RETIREMENT_REASONS` (`duplicate_of_another_thread`, `created_in_error`, `superseded_by_another_estimate`, `customer_withdrew`, `test_artifact`, `not_an_inquiry`, `owner_handles_thread`) | `retire` (tick, execute) | the record was put to sleep; status `dormant` |
| `revived` | append-only past retirements with `revived_at`, `note` | `revive` (execute; `doctor.py` repair) | a dormant record woke up on a new message |

## What is not on the record

- **Facts and their sources**: `ledger.sqlite` (`estimate_id, field, piece, stone, value, source, gmail_message_id`). The record's `specification` is the ledger's current view.
- **Briefs, questions, approvals**: `briefs/`, `questions/`, `approvals/` and `run-work/brief-registry.json`; a question's `estimate_id` points back here.
- **Work in flight**: `work/<sha>/` per claimed message (renderings, held emails, the next-step file) and `inbox-claims/`.
- **The shop**: `shop-profile.json` (rates, hours, desk mode). A one-time rate lives on the record; a permanent one goes to the profile through the Rates tab.
