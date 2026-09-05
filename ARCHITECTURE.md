# Jewelry Estimate Desk — target architecture

This document describes **how the desk should be built** on the Kolo platform
so that it implements `WORKFLOW.md` reliably. `WORKFLOW.md` says what the
business does; this document says which part of the system does each step,
what the platform provides, and in what order the change is built. Where the
two disagree, `WORKFLOW.md` wins.

Status: proposed design, 2 September 2026, revised the same day after a
platform review by Kolo (thread "Kolo CLI Architecture Review Summary").
Nothing below is built yet except where marked "exists today".

### Why split the job at all

Measured on the production pod on 2 September 2026:

| Run | Lane time | Model turns |
|---|---|---|
| Empty inbox tick, current design | 19 s | 8 |
| One inquiry, current design | about 6 min | 52 |
| Each model turn carries | about 24k tokens of context, 13k of it the SKILL.md read | |

Every tick today starts a model session and reads the whole skill even when
there is no mail. One timeout covers discovery plus every inquiry found, so
ticks stacked behind a long run and expired (all four timeouts on 2
September were this). Splitting makes empty ticks free, gives each inquiry
its own clock, and lets the model prompt shrink to one branch, which is what
allows a faster model to be trusted.

---

## 1. Design goals

1. **Nothing customer-facing depends on a model improvising.** Every send,
   booking, and delivery is one deterministic command that verifies the
   owner's approval and the bound state before acting.
2. **The scheduled job is cheap and boring.** It runs code, not a model, and
   finishes in seconds. Model work runs in short-lived jobs of its own, each
   with its own clock.
3. **The owner approves exactly what the customer will receive**: the email
   text, the price, the meeting time, the rendering images.
4. **Every step leaves evidence**: claims, records, receipts, approvals, and
   audit events, so any run can be resumed or explained.
5. **Small prompts.** A model job receives a few kilobytes describing one
   branch of the workflow, not the whole skill.

---

## 2. The pieces

```
Gmail (via Maton) ──► Watcher (command cron, every 1–2 min, no model)
                          │  discover → claim → fetch → classify → route →
                          │  match/create record → owner alerts → close trivial mail
                          │
                          ├──► Worker job (one per inquiry, isolated, own timeout)
                          │       extract spec ─► price quantities ─► draft email
                          │       ─► file a readable brief (approval request)
                          │
                          ├──► Follow-up scheduler (from record dates)
                          │
                          └──► Review tasks (Kolo task board)

Owner (Kolo) ──approve/edit/reject──► owner chat session
                                          │ one command only
                                          ▼
                              Approval executor (deterministic)
                              send estimate │ book meeting │ deliver rendering
                              ─► Gmail reply in thread, calendar write,
                                 record + mirror, audit, brief marked executed
```

### 2.1 Watcher (exists today only as part of the model-driven cron)

A command-kind cron job. It runs one bundled entry point with the pod's
environment and no model. On each tick it:

1. Validates the shop profile and monitor state; exits silently if not active.
2. Reconciles stale claims and stale owner alerts.
3. Discovers new Gmail messages since the durable watermark.
4. Claims each message, fetches it with its thread, classifies it, builds the
   reply route, decides thread ownership, and creates or matches the record.
5. Sends the "customer replied" owner alert for replies on known inquiries.
6. Closes automatic replies, files bounces and unclassifiable mail as review
   items, and finalizes those claims.
7. For every claim that needs judgment, writes a work packet and creates one
   worker job for that claim.
8. Schedules due follow-ups and creates review tasks (sections 2.5 and 2.6).
9. Prints nothing (silent) unless there is a real error to announce.

All of steps 1 to 6 exist today as deterministic helpers and are already
bundled behind one intake command; the watcher composes them.

If creating a worker job fails, the claim stays open and is retried on the
next tick; after three failures the watcher files a review item and alerts
the owner. The watcher never drops an inquiry silently. Its announced output
is bounded with an explicit output limit so nothing is truncated.

### 2.2 Worker job (new)

A one-shot isolated agent job created by the watcher, self-deleting on
success, with its own timeout, an explicit model and thinking setting, and
the minimal tool allowlist. Its prompt is a short branch template plus the
path of the work packet. There are three branch templates:

| Branch | Model tasks | Ends with |
|---|---|---|
| New inquiry, specification incomplete | Extract the merged specification from the whole thread; list missing fields; draft the batched price-free question email | Stage 1: brief for the owner to send. Stage 2+: deterministic send in thread. |
| New inquiry, specification complete | Extract the specification; fill the cost skeleton quantities; draft the estimate email using the price the helper computes | Brief containing the exact email and the owner-only cost sheet. |
| Reply after an estimate | Classify intents (accept, rendering, meeting, design change, pushback); for a rendering, write the image prompt; for a meeting, extract requested timing | Deterministic follow-through: rendering generation and approval, booking approval, owner alert, or escalation. |

A worker never sends anything to a customer. It writes JSON artifacts and
calls bundled commands. If it dies, its claim goes stale and the watcher's
existing recovery either retries once or files a review item.

Dead-spot guard (built 3 September 2026): a worker can die between
recording a thread review that says "ask the customer" and sending that
follow-up; the first emerald run did exactly that, the retry re-reviewed,
hit the one-review-per-message rule, and escalated while the customer sat
unasked. Now `estimate_record.pending_followup()` names that state,
`worker-start` returns a `resume` object telling the next worker to send the
recorded follow-up instead of reviewing again, a differing re-review while
the send is still pending returns the standing review rather than a
conflict, and when the send already happened `worker-start` finishes the
claim itself.

### 2.3 Briefs (approval requests)

A brief is filed by a bundled command, never composed by a model. It carries:

- A one-line action and a short reasoning line.
- **Flat, readable detail fields**: customer email, piece, specification as
  labeled lines, proposed price, and the exact customer email text that will
  be sent on approval. Nested objects are not used because the Kolo card
  renders them as `[object Object]`.
- The owner-only cost sheet as labeled lines.
- The execution payload: estimate ID, binding hash, action type, and the
  artifact paths the executor needs.

Three brief types: **estimate**, **booking** (with the calendar-checked
candidate times), and **rendering** (the two PNGs are attached to the
accompanying owner notification, since briefs cannot carry files).

### 2.4 Approval executor (partly exists today)

Kolo delivers the owner's decision into the owner's chat session. The only
thing the chat model is allowed to do with it is run one command with the
delivered payload. That command:

1. Verifies the binding hash against the current record and route; a mismatch
   or an edited price means "stale", and the executor files a fresh brief
   instead of sending.
2. Performs the action deterministically: reply in the original thread with
   the approved text, or write the calendar event with the customer as an
   attendee and invitations enabled, or attach the approved renderings to the
   thread.
3. Stores provider receipts, advances the record, mirrors it, writes the
   audit event, and reports the brief as executed or failed to Kolo.

`send-approved-estimate` exists today and already does most of the estimate
case; booking and rendering delivery move into the same executor.

Expiry: every brief carries `approval_valid_until` (the estimate's validity
date). An approval that arrives after it is refused by the executor, which
files a fresh brief with re-priced figures instead of sending. An edited
price is treated the same way as a stale binding: new brief, no send.

### 2.5 Follow-ups (new; documented but not built today)

On day 3 and day 7 after an estimate or a question email, a nudge is drafted
from the template with the record's facts, after checking that nothing newer
has happened on the record, and is either filed as a brief (Stage 1) or sent
deterministically (Stage 2 and 3). After day 7 the record is marked dormant.
No model is needed for a nudge.

Two ways to schedule it; the feasibility tests decide which:

- The watcher reads records with a `next_action_at` date on every tick (one
  scheduler, no extra jobs).
- Or, as Kolo suggested, the executor creates one-shot command jobs at the
  day-3 and day-7 timestamps when the estimate is sent (uses the platform
  scheduler as intended, but leaves two jobs per estimate to clean up).

### 2.6 Reviews as approval briefs (built 3 September 2026)

Every manual-review item is filed in the Kolo approval queue as a brief, the
one place the owner already looks, rather than on a task board. The card is
a yes/no control, so the brief asks one yes/no question: did you handle this
email? Its title names the sender and subject ("Check email from Pat: Ring")
so the owner can find the email in the shop inbox; the details repeat them,
say what to do, give the reason in plain words, and spell out approve (yes,
handled, the review closes) and reject (not yet, it stays open). Headers come
from the claim work file, falling back to the estimate record when the work
file is gone. Approving runs one deterministic command that closes the review
and marks the brief executed. The "show my unresolved reviews" chat query
stays as a secondary path.

Owner-channel discipline: the channel chosen at setup may be a phone, so it
receives only finalized messages. Ticks announce nothing unless the desk
itself fails; workers never announce; the redundant "unresolved review" chat
alert is gone because the brief is the notification.

### 2.7 Owner questions (built 3 September 2026; missing rate only so far)

WORKFLOW.md 6.10 splits what the desk sends the owner into permissions
(approval briefs) and facts (plain-English questions in the channel). The
first question kind is a missing rate. `cost_components.missing_rates()`
names the card section, a key built from the specification's own words, and
the words to ask with. The worker runs `workflow_safe.py ask-missing-rate`,
which files one question in `estimate-desk/questions/` (idempotent by
estimate, kind, and key), sends it through `kolo notify-owner` with
write-ahead journaling, and parks the claim as `awaiting_owner`, a third
terminal claim status that keeps the work directory and is invisible to the
review list and the stale reconciler. The watcher sends one reminder after
24 hours.

The answer arrives in the main Kolo session, which runs
`workflow_safe.py answer-question` with the owner's words verbatim. That
command reads exactly one number, saves it to the rate card with provenance
(`pricing.rate_provenance`), reopens the claim under a fresh token and a
worker lease (`inbox_claim.reopen`), writes an intake result the worker's
`worker-start` accepts, and spawns the one-shot worker. The price still goes
through the price brief, so a misread number is caught there. The answer
command refuses, before writing anything, when the estimate record fails
validation or is no longer awaiting specification, because on 3 September
the main chat session hand-edited a record while "helping" and the next
worker filed a misleading review; SKILL.md now forbids the main session
from writing desk state or pricing at all. The rate-key
matcher now prefers the key sharing the most descriptive tokens with the
specification, so the saved key resolves on the next pass instead of
re-raising the question. The same shape is intended for the unclear-reply
and same-sender cases, which still fall to manual review today.

### 2.8 Records, evidence, and audit (exists today)

Unchanged: one private record per inquiry as the routing index, mirrored to
Kolo; write-ahead journaling of every external action on the claim; audit
events with idempotent keys. The record gains an explicit `next_action_at`
and an `approval_valid_until` so follow-ups and stale approvals are data, not
inference.

### 2.9 Configuration binding (exists today, simplified)

The durable monitor state binds a hash of the watcher job definition and of
the three worker templates. Workers are created from those templates by the
watcher, so their prompts are deterministic and verifiable. The rebind
procedure stays two-phase and runs through the OpenClaw command line, never
by retyping through a chat model.

---

## 3. Platform facts this design relies on

Verified on the production pod on 2 September 2026 unless marked otherwise.

| Fact | Status | Used by |
|---|---|---|
| Command-kind cron jobs run a shell command inside the gateway with the pod environment, no model, own timeout, silent on `NO_REPLY` | Verified from CLI help and docs (`/app/docs/automation/cron-jobs.md`). Creating them needs operator-admin scope; our shell already has it, since the 2 September rebind edited and enabled the live job from the command line | Watcher |
| A shell command can create a one-shot isolated agent job (`--at +1m --delete-after-run --session isolated --message --model --thinking --tools --timeout-seconds`) | Verified live: a command job created and ran a child agent job (Stage 0 test 2) | Watcher → Worker |
| Two agent runs may proceed at once on this pod; sub-agent lane allows eight | Verified from `openclaw config get agents.defaults.maxConcurrent` (2) and `agents.defaults.subagents` (8), plus a live sub-agent test | Worker concurrency |
| Approval decisions are delivered as a message into the requesting chat session; no polling API | Verified from CLI help; no docs on expiry or on the Edit Intent return shape | Executor |
| Brief detail fields render readably only when flat | Observed on brief #85 | Briefs |
| Owner notifications accept repeated `--file` attachments | Verified live: PNG rendered inline in the owner's Kolo chat (Stage 0 test 4) | Rendering approval |
| A brief can be marked executed or failed after acting | Verified from CLI help | Executor |
| Kolo task board: create, list, update, assign, due dates | Verified from CLI help | Review tasks |
| Maton passthrough exposes Google Calendar event creation with attendees and invitations | Verified live: insert, read, delete with an attendee (Stage 0 test 6) | Executor (booking) |
| Maton passthrough exposes Gmail incremental history | Reported by Kolo; not yet exercised | Optional cheaper discovery |
| Images can be generated from a shell command with count and output path | Verified live: `gpt-image-2` default, PNG written (Stage 0 test 7) | Rendering |
| No Gmail push, no hooks, no public ingress on this pod | Verified 2 Sep; a second Kolo instance suggested Gmail hooks on 3 Sep and Tony confirmed there are none on Kolo today | Polling stays |
| `openclaw infer model run --model <id> --thinking off --json --prompt <text>` is the supported stateless completion; envelope `{"ok", "outputs": [{"text"}]}`, non-zero exit and `ok:false` on provider failure; no system-prompt, max-tokens, temperature, or JSON-mode flags; consumes no agent concurrency slot; command jobs inherit `LITELLM_API_KEY`/`LITELLM_BASE_URL` | Reported by a second Kolo instance from its CLI and docs, 3 Sep 2026; to confirm on this pod | Inline judgment (speed fix 2) |
| Cheaper models for extraction and classification: `litellm-fireworks/glm-5-3-flash`, `litellm/claude-haiku-4-5`, `litellm-openai/gemini-3.1-flash-lite-preview` (5-10x cheaper per token than qwen-3-7-plus) | Reported by a second Kolo instance | `pipeline.json` `model` |
| `openclaw infer image generate --prompt ... --json` returns `outputs[].path`; `infer image edit --file` exists | Reported; generate verified live 2 Sep | Retire the rendering worker later |
| Edit Intent replaces the brief's execution payload with the owner's edited JSON before it reaches the session | Reported by a second Kolo instance | Executor must validate a revised payload, not re-derive |
| No reply binding for `kolo notify-owner`; the owner's answer arrives only as a main-session chat message | Reported; matches our design (question code in the text) | Owner questions |
| Models available include `litellm-fireworks/qwen-3-7-plus` (pod default) and `glm-5-3-flash`; the current job's implicit "high" thinking is silently downgraded on GLM and must be set explicitly if the model changes | Verified from model list and logs | Worker model choice  Workers moved to `qwen-3-7-plus` with thinking off on 3 September 2026 after a glm-5-3 worker managed 14 tool calls in 900 s. |

---

## 4. Build order

Each stage ships as its own pull request with tests, is installed to the pod,
and is proven with one controlled inquiry before the next stage starts.

**Stage 0 — feasibility tests (an hour, no production change).**
Each test uses throwaway names and is deleted afterwards:

1. Create a command job that prints a timestamp every hour; confirm it runs,
   appears in the job list and the portal, and survives a gateway restart.
2. From inside that command job, create a one-shot isolated agent job with a
   harmless message; confirm it runs and self-deletes.
3. Confirm the command job's announce reaches the owner chat, and that a
   `NO_REPLY` print stays silent.
4. Send an owner notification with a test PNG attached; confirm how it shows.
5. File a test brief, approve it, and capture exactly what arrives in the
   chat session; check for an expiry field.
6. Create a calendar event through Maton with a test attendee and
   invitations enabled; confirm the event ID and the invitation.
7. Generate one image from a shell command; confirm the output file and the
   default model.
8. Create ten one-shot jobs in quick succession; confirm none is refused.

If tests 1 or 2 fail, the fallback is the single model-driven cron whose
first step is the deterministic watcher command and whose remaining prompt is
one short branch; most of the benefit survives.

**Stage 0 results, 2 September 2026 (evening, production pod).**

| Test | Result | Evidence |
|---|---|---|
| 1. Command job runs, appears in job list | Pass. Persistence across Kolo reconciliation still to confirm; the job `jed-stage0-cmd` is left in place for that check | Manual run wrote a timestamp to the log; listed as `command / cron` |
| 2. Command job creates a one-shot agent job | Pass | Parent command job created `jed-stage0-child`; the child ran 62 s later with `qwen-3-7-plus`, thinking off, tools `exec`, wrote its file, self-deleted |
| 3. Command job announce and `NO_REPLY` | Pass | Stdout text appeared in the owner thread; ten `NO_REPLY` jobs posted nothing |
| 4. PNG via owner notification | Pass, with a routing note | The image rendered inline, but in the owner's main Kolo chat, not the estimate-desk thread; use `--session-key` to target a thread |
| 5. Test brief approved | Pass | Flat detail fields rendered as labeled rows; the decision arrived in the session as a `user` message beginning "Strategic Brief #86 APPROVED"; the model marked the brief executed and the card switched to "Action executed" |
| 6. Calendar through Maton | Pass | Insert returned 200 with the attendee, read back 200, delete 204 |
| 7. Image generation from a shell | Pass | Default model `gpt-image-2`, 1024×1024 PNG written to the requested path |
| 8. Ten one-shot jobs at once | Pass | All ten accepted; all self-deleted after running. Note: `--exact` is only valid with `--cron`, not `--at` |

Not shown as a Routine: the Kolo portal's routine list showed only the
model-driven inbox monitor, not the command job. Command jobs are managed
from the command line, not the portal.

Persistence across Kolo's reconciliation (test 1) remains open until the
throwaway job has survived at least one day; everything else the design
depends on is now proven.

**Stage A — built 2 September 2026 (PR pending deploy).** `scripts/inbox_watcher.py`
is the command-kind tick; the per-claim worker prompt was at first the full
runbook minus discovery and reporting (Stage B, 3 September 2026, replaced it
with `templates/worker-common.txt` plus one branch prompt, `worker-intake.txt`
or `worker-post-estimate.txt`, chosen from the record status, and workers no
longer read SKILL.md at all);
`workflow_safe.py worker-start` hands a leased claim to its worker; the
claim's recovery lease doubles as the worker lease; `cron_config` binds the
watcher command line and produces it as the target for any live job.

**Stage A — split the job (plumbing, low risk).**
Watcher as a command cron; workers created per claim using today's full
runbook as the worker prompt. Wins: silent zero-cost empty ticks, one clock
per inquiry, no tick stacking.

**Stage B — small prompts and readable briefs (prompts built 3 September 2026).**
Built: `templates/worker-common.txt` plus `worker-intake.txt` or
`worker-post-estimate.txt` chosen by record status, workers never read
SKILL.md, explicit model (Qwen 3.7 Plus) and thinking off per worker, flat
brief fields, the executor reports briefs as executed. Still open: the
customer email drafted before the price brief and included in it. Measured
before the prompt cut: about three minutes per claim on Qwen with the full
runbook; to be re-measured with the branch prompts.

**Speed fix 1 — bundled worker steps (built 3 September 2026).** A claim
used to cost 10-12 model round trips. Now `worker-start` returns the thread
as plain text (`gmail_text.thread_digest`), so the worker never opens Gmail
JSON; `review-thread` takes only the worker's judgment (specification plus
missing fields, or the post-estimate artifact), adds the thread ids,
records the review, and runs everything deterministic after it: the spot
price, the cost skeleton, the missing-rate question, or the post-estimate
finalize; and `price` takes the worker's quantities (grams, hours, an
optional carat, fee and accent catalog keys) and does skeleton fill,
finalize, binding, brief, record, mirror, and claim finish in one command.
An intake claim is now about five round trips: start, write review,
review-thread, then either write body plus send-spec-followup or price.

**Speed fix 2 — inline judgment, no worker job (built 3 September 2026,
switched off until verified live).** Kolo exposes a stateless completion,
`openclaw infer model run --model <id> --json --prompt <text>`, usable from
a command-kind cron job. `judge.py` wraps it: one call per judgment (triage,
specification extraction, post-estimate classification, follow-up drafting,
quantities), strict JSON parsing, shape validation, one retry that quotes the
rejection, and a `JudgmentError` that distinguishes a transient platform
failure from a malformed answer. `spec_gate.py` decides the missing required
fields by rule, so the model only extracts. `pipeline.py` runs a claim end to
end inside the watcher tick: dead-spot resume, triage, extract, gate,
`review-thread`, then draft plus `send-spec-followup` or quantities plus
`price`; post-estimate replies are classified and finalized the same way, and
only rendering or appointment work is still handed to a worker job. The
switch is `estimate-desk/pipeline.json` (`{"inline": true, "model": ...}`),
read by the watcher each tick, so enabling it needs no rebind. A transient
model failure leaves the claim processing and unleased for the stale
reconciler; a malformed answer after the retry files `classification_malformed`.
Expected: two to three completions per claim, finishing in the tick that
discovered it, and no agent loop that can wander.

**4.6.0 (built 5 September 2026): reliability plan, step 3.** The tick
owns its inline claims: each is marked and leased (`inline_attempts`,
`INLINE_LEASE_SECONDS`), a failure is counted on the claim with its kind and
the lease released, and the next tick retries claims whose lease has
lapsed (a deferral or a crash) before taking new ones. Transient failures
get six tries, deterministic ones two; then `ask_stuck_claim` files one
question (`stuck_claim`: retry runs the claim again in the answer, skip or
handle myself closes it) and parks the claim. The stale reconciler leaves
inline claims alone. A card whose filing is unknown is checked against the
audit trail by its title (`kolo_safe.verify_card`) the way sends are checked
against the thread; a rate answer that died mid-way replays from the
answered question; a started owner notice is never sent twice; the
follow-up reuses its journaled payload; the appointment card's binding
check ignores the reject code it adds after writing. The fault-injection
suite ends every one of its 84 combinations in ok or recovered.

**4.5.0 (built 5 September 2026): reliability plan, steps 1 and 2.**
`tests/test_fault_injection.py` fails and crashes every external call in
turn across the golden path (84 combinations) and checks three promises
after the operator's retry: nothing twice, nothing silent, nothing else
heard; gaps are listed in the test with the plan step that removes them.
Step 2 closed all ten executor gaps: a customer send whose outcome is
unknown (a crash around the call, a failed call) is settled by reading the
thread for the desk's own Message-ID (`gmail_safe.find_delivery`,
`inbox_claim.settle_external_action`, status `verified_unsent`), never
resent on a guess; the booking journals its slot before the calendar call
and adopts its own event by the estimate id after a crash
(`calendar_query.list_events`); the rendering executor resumes after its
reopen. Each executor runs under a lease (`run_lease.py`, `locks/`), and a
failure marks the brief, asks the owner once (`command_failed`: retry runs
the same command from inside `answer-question`, release deletes only the
desk's own journaled event, handle myself), and prints the error.

**4.4.2 (built 5 September 2026): two cards from one email, approved in
either order.** A customer asked for a rendering and a meeting in one
email; the desk filed both cards and parked the claim behind the rendering
card. The booking card was approved first: the executor created the
calendar event, then refused to send the confirmation because the claim
was parked ("claim is not in an allowed state"), leaving an event nothing
recorded. Now executors may act on a claim parked behind another card,
every refusal happens before the calendar is touched, and a created event
is written beside the booking work so a retry after a crash reuses it
instead of booking twice or finding its own slot busy.

**4.4.1 (built 5 September 2026): no ragged lines.** The model wrapped an
estimate at seventy columns and Gmail showed the breaks mid-sentence.
`plain_text` (applied on every send) now reflows single line breaks inside
a paragraph into spaces, keeping blank-line paragraphs, dash bullets,
greetings and sign-offs ("Warmly," then the name), and short heading
lines; the drafting prompt asks for one paragraph per line.

**4.4.0 (built 5 September 2026): meeting first, and a jeweler's voice.**
A customer who wrote "I can bring the stone, are you free next week?" was
answered with a questionnaire. Now scheduling intent before an estimate
files the appointment card and holds the detail questions; approval
stores, offers, and bookings accept an `awaiting_specs` record, a booking
made before the estimate keeps the record waiting for details
(`before_estimate` on the receipt), and the confirmation or offer says the
design gets settled at the meeting. The follow-up prompt and template were
rewritten as a jeweler writing back (react to what they shared, three
questions at most, an invitation to come in), the body check refuses
heading stubs and sign-offs ending in a question mark, and the default
voice and the meeting briefs carry the same warmth.

**4.3.5 (built 5 September 2026): a fresh start forgets everything about
customers.** The customer-state reset now also removes owner questions,
appointment approval stores, and the brief registry, and accepts the
newer work-folder names; the procedure tells the owner to reject any open
cards, since the desk cannot withdraw a Kolo brief.

**4.3.4 (built 5 September 2026): the customer's own stone, and no
question twice.** A customer resetting his mother's diamond was asked its
color and clarity grade, twice, and answered "what does that have to do
with this?". Now a customer-supplied stone (`customer_supplied_materials`,
or words like "my mother's diamond", "reset", "heirloom") needs only its
shape and size, carries no stone cost, and skips the ask-always origin rule.
A reply that leaves the same fields open that were already asked for never
gets the same email again: the owner is asked (`followup_stalled`: skip
and price with those fields as the jeweler's choice, ask again, or handle
myself), the claim parks, and the answer prices, resends, or closes. A
reply on a thread that already has a record is never re-triaged as junk
(that refusal was retrying every tick), and the tick's own bookkeeping
("could not be judged", "did not settle", "could not be started") stays in
the run summary instead of the owner's chat.

**4.3.3 (built 4 September 2026): windows are a gate, not a hint.** The
same worker path put a Sunday on a booking card: its prompt let the agent
write the availability list. `request-appointment-approval` now refuses any
option outside the declared consultation windows, and
`book-approved-appointment` refuses to book one, whoever wrote it.

**4.3.2 (built 4 September 2026): the rendering gate in code.** A tennis
bracelet's renderings reached the customer without a card. The inline
render had failed on every 4.2 and 4.3.0 pod (the materializer refused the
desk's own files), so each rendering request fell to the worker agent, whose
branch prompt said a rendering "needs no new approval" and ran
`send-rendering`, which had no gate. Now `send-rendering` refuses unless
called by `send-approved-rendering` with the approval the owner saw and the
same image hashes; the worker's branch (and the legacy cron prompt) render,
materialize, and run the new `request-rendering-approval` command, which
files the card and parks the claim; the worker never emails a customer.

**4.3.1 (built 4 September 2026): the full check.** A golden-path test
(`tests/test_golden_path.py`) drives the real watcher tick and the exact
execute lines from the cards through inquiry, follow-up, rate question,
price brief, estimate, rendering from the customer's logo, booking card,
rejection, the owner's words, offer card, the customer's pick, booking,
and reschedule, with Gmail, Kolo, the calendar, and the model faked by
contract. It found four seams: stones described only as accents counted as
a center stone (carat and cut were asked); the materializer refused the
desk's own renders because they were not under the Kolo media root; a
customer asking to meet when the calendar offered nothing (or could not be
read) got a card with no times, now a direct question with the claim
parked until the owner answers; and the pipeline built executor namespaces
without the runners, so brief registration and question delivery bypassed
them. The tick summary now carries the reason when an inline claim hands
off to a worker.

**4.3.0 (built 4 September 2026): the pieces made to fit.** Customer
emails are drafted when the card is filed (estimate at `request-approval`,
rendering note at `request-rendering-approval`, confirmation or offer at
`request-appointment-approval`) and stored beside the work
(`work/estimate-<id>-<msgkey>/customer-reply.txt`, the claim's
`customer-reply.txt`, `approvals/<id>-<msgkey>.email.txt`); executors read
and send in seconds. A built mail payload is kept and reused on a retry so
the journal binding matches, and a journaled action whose provider call
never ran may be retried. Stones are detected from the customer's words;
the origin is asked when the profile says so; a rate key never carries an
origin the customer did not state; once the origin is known, a missing
per-carat rate for small stones is asked of the owner. Pave pieces need no
carat or cut; a follow-up must ask something and may not recap.

**4.2.0 (built 4 September 2026).** Renderings go through `rendering.py`:
a planning call picks a construction archetype from `templates/render/`
(32 of them), code assembles the prompts from the archetype's clauses, the
image model renders two views with the customer's artwork attached
(`artwork.py` fetches image attachments from the thread), and a vision
model answers the archetype's bench questions per view with one
regeneration; the card carries the checker's verdicts. A piece whose
stones are all small pave or accents has no center stone
(`cost_components.has_center_stone`), so no center rate is asked for and no
center carat is sized. A rate answer prices inline from the recorded
review and can be replayed. The price brief title carries the whole cost
sheet. Every customer email is written for its thread by `customer_mail.py`
and checked before it goes.

**Batch 6: rejections read from the audit trail (built 4 September 2026).**
`kolo audit-query` returns `brief_lifecycle` events: `brief.submitted` when
the desk files a card (with the brief id and number), `brief.rejected` when
the owner rejects it (with any note). `brief_registry.py` records each
card's brief id at filing (matched by the card title in the newest submitted
events) under `estimate-desk/briefs/`, and every watcher tick polls for
rejections of pending cards: an appointment rejection wakes the card's
dormant question so the owner is asked what to do; a rendering rejection
closes the parked claim with one notice; a price rejection is one notice.
Owner answers after a rejection file a new offer card (no direct send), and
no code is typed: with no `--question`, the words go to the customer whose
card was filed last or the customer named.

**Batch 5: silent rejections and a clean chat (built 4 September 2026).**
Kolo delivers approvals into the session but not rejections (observed 4
September; no CLI to poll a brief). So every appointment card files a
dormant `appointment_next` question and names its code in the reject row;
an owner who rejects and then replies with the code and a plan answers that
question through the same `answer-question` path. Approving the card closes
the dormant question as superseded. Owner-visible question messages end with
one line, `desk-answer <CODE>`, instead of the full command; SKILL.md maps
the tag to the command.

**Batch 4: three appointment scenarios (built 4 September 2026).** A
specific requested time that is free is a booking card, one time, yes or
no. No time, or a time that is taken, is an offer card: two or three free
times (near the request when there was one; otherwise up to two on the
nearest day with room plus one on the next day) that approve emails to the
customer (`send-approved-times`, recorded under `times_offered`), nothing
booked. Every rejected appointment card runs `execute_on_reject`
(`appointment-rejected`), which asks the owner in plain words what to do:
times they type are resolved by the judge, checked against the calendar,
and emailed; "other times" picks new free ones; "handle myself" leaves the
thread to them. Owner-typed times go straight to the customer because the
owner's words are the approval.

**Batch 3: the command travels with the decision (built 3 September 2026).**
Three approvals in a row (renderings, a booking, and earlier a price) were
executed by the main session improvising: raw gateway calls, hand-built
mail, hand-edited records. Now every approval payload carries an `execute`
field with the one command to run, and every owner question ends with the
`answer-question` line. Two new one-command executors:
`send-approved-estimate-brief` (plain-text estimate email from the record and
profile terms, sent through `send-approved-estimate`) and
`book-approved-appointment` (re-checks free/busy for the chosen option,
inserts the event through the calendar gateway with the customer invited,
sends the confirmation through `gmail_safe`, records the booking receipt,
reports the brief). Appointment approvals are kept under
`estimate-desk/approvals/` because claim work is cleaned when the claim
closes. The judge also resolves a customer's specific request to a local
date-time and `slots.offer_times` puts it first when it is inside the windows
and free.

**Answer replay (built 3 September 2026).** `answer-question` leases the
parked claim before it records the answer, so a failure leaves nothing half
done; run again, it continues from a claim an earlier attempt already
reopened and replays a recorded decision whose claim is still parked or has
no intake result yet. Once the inquiry has moved on it reports
`already_answered` and does nothing. Motivation: a session-made record with an
invented route made the first answer fail after recording, and every tick
after that failed on the same record until the file was quarantined.

**Batch 1 (built 3 September 2026).** Same-sender and unclear-reply reviews
are owner questions with fixed outcomes (`owner_questions.create_decision`,
`match_option`; `answer-question` applies them: a new piece is quoted through
intake and the pipeline, everything else closes the parked claim on the
owner's word with no card). Desk failures are one plain notice in the owner's
channel, never a brief. `owner_channel.session_key` in the shop profile
targets every owner message. The center-stone matcher ignores melee keys. The
price brief shows flat rows (customer, subject, piece, price, what approve
and reject mean) with the bound state in the payload. The tick keeps an
inline budget, sweeps errored one-shot jobs, and now sends renderings and
files appointment approvals itself, spawning a worker only when image
generation fails. Still open: validating an Edit Intent revision, the
reopen-the-gate path for a "change" answer, calendar slots in tick-side
appointment approvals.

**Batch 2 (built 3 September 2026).** Renderings are gated: the tick
generates the views, sends each to the owner's channel as a PNG, files a
`send_rendering` approval (flat rows, medium risk, image hashes in the
payload), and parks the claim; `send-approved-rendering` re-verifies the
hashes and sends, `reject-rendering` closes with nothing sent. Appointment
cards carry what the customer asked for (one judgment call quoting their
words) and up to three live-checked times from `slots.py` (declared windows,
free/busy through the calendar gateway, labelled by `appointment_options`),
with approve meaning "book Option 1" and Edit Intent to choose another; risk
medium. Still open: the main-session executor booking exactly the option on
the card, and a "change" answer reopening the gate.

**Stage C — deterministic approvals for bookings and renderings.**
Calendar-write helper with receipts; rendering generation by script with
PNGs attached to the owner notification; booking and rendering approval at
every stage; Stage 3 renamed to "Offer times". This closes two of the three
gaps recorded in `WORKFLOW.md`.

**Stage D — follow-ups, review tasks, retail-only cleanup.**
Day-3 and day-7 nudges from record dates; review items mirrored to the task
board; removal of wholesale mode, wording, and trade markup; estimate
validity and stale-approval handling; per-run metrics line for the weekly
summary the owner guide promises.

---

## 5. Risks and what would change the design

- **Kolo may reconcile or reject command-kind jobs or dynamically created
  jobs on a managed pod.** Stage A's feasibility test settles this. If it
  fails, the fallback is a single model-driven cron whose first step is the
  deterministic watcher command and whose remaining prompt is one short
  branch; most of the benefit survives.
- **The approval decision still passes through a chat model.** The design
  limits that model to one command, but a misbehaving model could still do
  something else. Mitigation: the executor is the only path that can send,
  and the chat prompt for approvals is a single sentence naming that command.
- **Worker model quality.** A faster model must follow exact commands and
  write valid JSON. A candidate becomes the default only after passing a
  fixed qualification set: ten controlled inquiries covering each branch,
  with zero invented commands, zero invalid artifacts, and prices equal to
  the helper's figures.
- **Volume.** Fifty inquiries a day means fifty one-shot jobs a day. No limit
  is documented; test 8 probes it, and the watcher's retry-then-review
  fallback covers a refusal.
- **Sub-agent handoff was proven concurrent but is not used here.** One-shot
  jobs give the same isolation with a simpler lifecycle and an explicit
  timeout; sub-agents stay a fallback.

---

## 6. Kolo's review, 2 September 2026

Kolo reviewed this document against the pod's docs, CLI help, and config.
Its answers to the eight open questions, and what changed as a result:

| Question | Kolo's answer | Action |
|---|---|---|
| 1. Command job persists across reconciliation, shows as a Routine? | Unknown; needs operator-admin scope (which our shell has) | Stage 0 test 1 |
| 2. Command job can create a one-shot agent job? | Unknown; gateway token availability inside a command job unproven | Stage 0 test 2 |
| 3. Command job announce and `NO_REPLY`? | Confirmed from docs | None |
| 4. PNG inline via notification attachment? | Unknown | Stage 0 test 4 |
| 5. What arrives on Approve, Edit, Reject; expiry? | Message into the chat session; no polling API; expiry and edit shape unknown | Stage 0 test 5; expiry handled in the executor (2.4) |
| 6. Maton calendar invitations and event ID? | Unverified; passthrough documented, attendees not explicitly | Stage 0 test 6 |
| 7. Image generation from a shell? | CLI confirmed; default model and token in command jobs unknown | Stage 0 test 7 |
| 8. Daily one-shot job limit? | Undocumented | Stage 0 test 8; watcher fallback added (2.1) |

Kolo's critique and the responses adopted:

- Use the task board as the primary review surface, not a mirror. Adopted
  (2.6).
- Consider one-shot jobs for day-3 and day-7 nudges instead of watcher
  polling. Recorded as an option (2.5).
- Specify approval expiry handling, a model qualification test, an explicit
  output limit for the watcher, and a fallback when job creation fails.
  Adopted (2.1, 2.4, 5).
- Kolo questioned why splitting helps at all. The measurements at the top of
  this document answer that; Kolo did not have them.
- Kolo marked the concurrency limits as unverified; they were read from the
  live config in a separate thread and are cited in section 3.

---

## 7. What does not change

The reply-in-thread invariant, the content guard before every customer send,
the claim journal and stale-claim recovery, the private record with its Kolo
mirror, the hash-bound configuration, and the rule that a conversational
"yes" is never an approval.
