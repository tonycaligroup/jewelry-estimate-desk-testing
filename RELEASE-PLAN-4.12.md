# Release plan 4.12.0: everything still open, built as one system

Written 6 September 2026 after the owner's instruction that the desk is not
built piece by piece. Every open item below is designed against every
consumer it touches (code, card text, owner words, docs, tests, harness,
doctor, reset) before any of it is built, and the release ships once, green
on the suite and the harness. WORKFLOW.md is the source of truth; where a
rule is new it is written there first (section 12).

## 0. Items and their status

| # | Item | Rule (WORKFLOW.md) | Status |
|---|---|---|---|
| 1 | Rendering reject loop: reject, "what should change", re-render that piece, fresh card | 6.6, 6.10 | built (4.12.0) |
| 2 | Reading check: the model's reading cross-checked against the customer's words before pricing | 6.2 ("nothing invented") | built (E', 4.12.0) |
| 3 | Rehearsal mode: loud, one named address, real mail untouched | 10, 6.5 | built (F2, 4.12.0); real mail held, released on off |
| 4 | After an estimate, "change" reopens the gate on the thread; "second piece" adds a line | 6.8, multi-piece rule | built (4.12.0) |
| 5 | Known customer on a new thread, "same": continue, do not hand over | 6.1 | built (4.12.0); a pending price card still hands over |
| 6 | Wholesale wording and the trade markup removed; stale "stage 3 autonomy" note corrected | 1, 12 | built (4.12.0) |
| 7 | Watcher schedule: 24/7 every 2 minutes vs shop hours | 10 | owner's choice, config only; open |

## 1. Rendering reject loop

Today: reject holds the images back and the desk says "tell me what to
change if you want new views"; nothing implements the answer.

Design. A rejected rendering card wakes a `rendering_next` question (same
shape as `appointment_next` and `price_next`): "You passed on the renderings
for <customer> (<piece>). Tell me what should change (name the piece for a
set), or 'handle myself'." The owner's words become a change note. The desk
re-renders only the pieces the words name (all pieces when none is named, one
piece when there is one), keeps the other pieces' views as they were,
re-checks, sends previews, and files one fresh rendering card whose title
carries "revised: <the owner's words>". Approval sends as before.

| Consumer | Change |
|---|---|
| `workflow_safe.handle_rejected_briefs` (kind rendering) | ask `rendering_next` instead of a notice; the claim stays parked (it is parked behind the card already) |
| `owner_questions` | kind `rendering_next`, options `change_given` (free words) / `handle_myself`; `match_option` returns `change_given` for any other words |
| `workflow_safe._answer_rendering_next` | resume the parked claim; write `rendering-change.json` {note, pieces}; run the rendering job path again (`render_job` in-process via `pipeline.render_and_send(change=...)`) so the 900-second budget holds; "handle myself" closes the claim as before |
| `rendering.run_pieces(change=...)` and `build_prompts` | the note goes into the prompt for the named pieces only ("the owner asks: ..."); untouched pieces reuse their slot files |
| `pipeline.render_and_send` | accepts `change`; slots for untouched pieces are kept, hashes recomputed for the card; card title suffix; `rendering-approval.json` rewritten (new hashes) |
| `kolo_safe.build_request_rendering_approval` | a "Revised" row with the owner's words; "Reject means" says "tell me what to change" (already) |
| `request_rendering_approval` | second card for the same message: action key `rendering_approval:<est>:<msg>:r<n>`; register brief; the claim parks again |
| `brief_registry` | unchanged (kind rendering) |
| `estimate_record` | `rendering_revisions` append-only list {note, pieces, at}; nothing else |
| `doctor` | `rendering_next` joins PARKING_KINDS (its claim is parked) |
| `customer_state_reset` | work files live in the claim work dir; nothing new |
| SKILL.md, OWNER-GUIDE | the question in the list; "reject, then tell me what to change" |
| Tests | reject → question → "make the band wider" → only the band re-rendered → card with the Revised row → approve → sent once; "handle myself" → held; harness action list gains the loop |

## 2. Reading check (E')

Design (ARCHITECTURE-OPTIONS.md E'): a code check, no model call, run in
`pipeline.process_claim` after extraction and before the gate:

- ring sizes in the text (`size 6`, `6.5`, `a 7`) vs sizes in the reading;
- piece nouns (ring, band, pendant, earrings, bracelet, necklace) vs the
  number of pieces read;
- carat figures (`1 ct`, `1.5 carat`) vs `stone_carat`;
- origin words (`lab`, `lab-grown`, `natural`, `mined`, `moissanite`) vs
  `stone_origin`;
- a stone the customer already owns (`my`, `grandmother's`, `heirloom`) vs
  `customer_supplied_materials`.

A mismatch does not price. It is put to the customer in the follow-up as a
confirming line ("You mentioned two sizes, 6 and 10; are those two pieces?")
alongside whatever else is missing, one email, all at once (6.2). The record
carries `reading_checks` (what was compared, what disagreed). No mismatch,
no change in behaviour. The check is conservative: only disagreements the
text states plainly count; absence of a word never counts.

| Consumer | Change |
|---|---|
| new `reading_check.py` | `compare(text, specification) -> list[Disagreement]`; pure |
| `pipeline.process_claim` | run after `triage_and_extract`; disagreements become missing-field labels `confirm.<topic>` that `describe_missing` renders as questions |
| `spec_gate` | `confirm.*` names are accepted as missing fields; cleared when the next reply's reading agrees |
| `judge.draft_followup` | the confirming lines are given as bullets like any other missing detail |
| `estimate_record` | `reading_checks` list on `record_thread_review` |
| Tests | two sizes read as one piece → the follow-up asks; "my grandmother's diamond" read as lab-grown → asks; agreement → unchanged path; the multi-piece scenario unchanged |

## 3. Rehearsal mode (F2)

Design (ARCHITECTURE-OPTIONS.md F revised). `shop-profile.json` gains
`rehearsal: {"enabled": false, "address": null}`; a command
`rehearsal.py --on --address owner@example.com` / `--off` writes it (the
profile hash is not part of the cron binding; verified by the reset path).
While on:

- readiness prints `REHEARSAL MODE: only mail from <address> is handled` as
  its first line in capitals; the doctor and every tick summary repeat it;
- every card title and every customer subject line starts with
  `[REHEARSAL]`; every owner notice starts the same way;
- discovery still records every message (the watermark rule is unchanged),
  but a message not from the address is queued as `held_for_live` and not
  claimed; `rehearsal.py --off` and the next tick release held items in
  order, so real mail is delayed, never lost; the doctor lists held items;
- the rehearsal customer is the owner's own address, so the emails the desk
  sends reach the owner.

| Consumer | Change |
|---|---|
| `validate_profile` | the `rehearsal` block |
| `inbox_watcher.tick` | the hold at claim time; release on `--off`; the summary line |
| `inbox_monitor` | queue status `held_for_live`; `release_held(root)` |
| `readiness`, `doctor` | the capital line; held items listed |
| `kolo_safe.approval_title`, `appointment_card`, `build_request_rendering_approval`, `owner_questions.deliver`, `gmail_reply.build_reply` | the prefix |
| `customer_state_reset` | clears held items like queue items |
| SKILL.md, OWNER-GUIDE, monitor-operations | how to rehearse a new version |
| Tests | on: a real customer's mail is held and released on off; the owner's mail runs the whole path with the prefix everywhere; off: unchanged |

## 4. "Change" and "second piece" after an estimate

Today both answers close the thread to the owner.

Design. `change`: the record goes back to `awaiting_specs` with the sent
estimate kept as history (`estimates_sent` append-only, `superseded: true` on
the old one); the customer's message is read again for the changed details;
the gate asks for what is missing or, if complete, prices; a fresh card
follows. `second piece`: the specification becomes `pieces` (the existing
piece plus the new one read from the message), one estimate, one total, the
multi-piece rule; the old estimate is history the same way. Both stay in the
original thread.

| Consumer | Change |
|---|---|
| `estimate_record.reopen_for_change(root, estimate_id, message_id, kind)` | status `estimate_sent` → `awaiting_specs`; `estimates_sent` history; `approval_requests`/`rendering_deliveries` untouched; the route unchanged |
| `route_ownership` | `awaiting_specs` after `estimate_sent` allowed (status set is unchanged, the transition is new) |
| `require_processed_evidence` | a message whose review outcome is `reopened_for_change` is processed with the follow-up or approval evidence like a first inquiry |
| `workflow_safe.answer_decision` (unclear_reply) | `design_change` and `second_piece` resume the claim, reopen the record, and run `pipeline.process_claim` on the message with the reopen kind |
| `pipeline.process_claim` | reading a reopened record: extraction merges the new message into the existing specification (or into `pieces`); then the normal gate/price path |
| `judge` | extraction prompt variant "the customer already has an estimate for X; they now say ..." |
| Customer wording | the follow-up and the new estimate say "updated" ("Here is the updated estimate for ...") |
| Tests | change → gate asks the changed detail → price card → estimate; second piece → two-line card; history kept; the old estimate never re-sent |

## 5. Known customer, new thread, "same" (awaiting the owner's answer)

Proposed: the record's route moves to the new thread (thread id, message
id); every reply goes there from then on. Allowed while the record is
`awaiting_specs` (the customer answered the follow-up in a new thread) or
`estimate_sent`; a `pending_approval` record still hands over (the binding
holds the route). `route_history` keeps the old thread.

## 6. Wholesale remnants and the stale note

Remove `trade_markup_multiplier` from the profile template and validator,
the "Wholesale, add:" block from `templates/customer-emails.md`, and the
retail-and-wholesale sentences. Correct WORKFLOW.md section 12 (no stage-3
autonomy exists in code; bookings are card-gated at every stage).

## 7. Order of work

1, 2, 6 (independent), then 4, then 3, then 5 when answered. Version bumps
once to 4.12.0. Suite and harness green before publish; ARCHITECTURE.md
gets one note; SKILL.md, OWNER-GUIDE, HANDOFF, playbook updated in the same
commit.

## 8. Follow-up, 7 September 2026: rendering shape

The one-shot render job produced four live defects on 6 September (a model
resolved differently in its environment, a target-less delivery step that
marked every finished job errored so it stayed in the owner's routines
list, images refused by write-once slot files on a revision, and a job
that died without telling anyone). Options weighed with the owner: keep the
jobs with fixes; retire them and raise the watcher's clock (a rebind); a
second permanent routine; or render one view per tick inside the watcher.
Chosen: one view per tick inside the watcher, the safest by the measures
that bit us: one environment, nothing created on the instance, every step
small and resumable, nothing side by side. Built and green (538 tests,
harness 96/96), shipped as 4.13.0 on 7 September 2026 after the named vision model was proven inside a cron-run command.
