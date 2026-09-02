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

### 2.6 Review tasks (new)

The Kolo task board becomes the primary place the owner sees manual reviews.
Every review item becomes a task assigned to the owner, titled with the
privacy-safe review key and reason, with a due date; resolving the review
closes the task. The "show my unresolved reviews" chat query stays as a
secondary path only.

### 2.7 Records, evidence, and audit (exists today)

Unchanged: one private record per inquiry as the routing index, mirrored to
Kolo; write-ahead journaling of every external action on the claim; audit
events with idempotent keys. The record gains an explicit `next_action_at`
and an `approval_valid_until` so follow-ups and stale approvals are data, not
inference.

### 2.8 Configuration binding (exists today, simplified)

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
| No Gmail push, no hooks, no public ingress on this pod | Verified | Polling stays |
| Models available include `litellm-fireworks/qwen-3-7-plus` (pod default) and `glm-5-3-flash`; the current job's implicit "high" thinking is silently downgraded on GLM and must be set explicitly if the model changes | Verified from model list and logs | Worker model choice |

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

**Stage A — split the job (plumbing, low risk).**
Watcher as a command cron; workers created per claim using today's full
runbook as the worker prompt. Wins: silent zero-cost empty ticks, one clock
per inquiry, no tick stacking.

**Stage B — small prompts and readable briefs.**
The three branch templates; the customer email drafted before the brief and
included in it; flat brief fields; explicit model and thinking per worker;
the executor reports briefs as executed. Wins: inquiries drop from about six
minutes to one or two; the owner reads real text.

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
