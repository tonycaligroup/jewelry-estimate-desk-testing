# Jewelry Estimate Desk — target architecture

This document describes **how the desk should be built** on the Kolo platform
so that it implements `WORKFLOW.md` reliably. `WORKFLOW.md` says what the
business does; this document says which part of the system does each step,
what the platform provides, and in what order the change is built. Where the
two disagree, `WORKFLOW.md` wins.

Status: proposed design, 2 September 2026. Nothing below is built yet except
where marked "exists today".

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

### 2.5 Follow-ups (new; documented but not built today)

The watcher reads records with a next-action date. On day 3 and day 7 after
an estimate or a question email, it drafts the nudge from the template with
the record's facts, checks that nothing newer has happened, and either files
it as a brief (Stage 1) or sends it deterministically (Stage 2 and 3). After
day 7 it marks the record dormant. No model is needed for a nudge.

### 2.6 Review tasks (new)

Every manual-review item also becomes a task on the Kolo task board,
assigned to the owner, titled with the privacy-safe review key and reason,
with a due date. Resolving the review closes the task. The owner keeps the
"show my unresolved reviews" chat query as well.

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
| Command-kind cron jobs run a shell command inside the gateway with the pod environment, no model, own timeout, silent on `NO_REPLY` | Verified from CLI help and docs | Watcher |
| A shell command can create a one-shot isolated agent job (`--at 0m --delete-after-run --session isolated --message --model --thinking --tools --timeout-seconds`) | Verified from CLI help; creation from inside a command job not yet exercised | Watcher → Worker |
| Two agent runs may proceed at once on this pod; sub-agent lane allows eight | Verified from config and a live test | Worker concurrency |
| Approval decisions are delivered as a message into the requesting chat session; no polling API | Verified from CLI help; no docs on expiry or on the Edit Intent return shape | Executor |
| Brief detail fields render readably only when flat | Observed on brief #85 | Briefs |
| Owner notifications accept repeated `--file` attachments | Verified from CLI help; inline PNG display not yet confirmed | Rendering approval |
| A brief can be marked executed or failed after acting | Verified from CLI help | Executor |
| Kolo task board: create, list, update, assign, due dates | Verified from CLI help | Review tasks |
| Maton passthrough exposes Google Calendar event creation with attendees and invitations | Reported by Kolo from the API-gateway skill; not yet exercised | Executor (booking) |
| Maton passthrough exposes Gmail incremental history | Reported by Kolo; not yet exercised | Optional cheaper discovery |
| Images can be generated from a shell command with count and output path | Verified from CLI help; not yet exercised | Rendering |
| No Gmail push, no hooks, no public ingress on this pod | Verified | Polling stays |
| Models available include `litellm-fireworks/qwen-3-7-plus` (pod default) and `glm-5-3-flash`; the current job's implicit "high" thinking is silently downgraded on GLM and must be set explicitly if the model changes | Verified from model list and logs | Worker model choice |

---

## 4. Build order

Each stage ships as its own pull request with tests, is installed to the pod,
and is proven with one controlled inquiry before the next stage starts.

**Stage A — split the job (plumbing, low risk).**
Watcher as a command cron; workers created per claim using today's full
runbook as the worker prompt. Wins: silent zero-cost empty ticks, one clock
per inquiry, no tick stacking. Feasibility tests first: create a command job,
create a one-shot job from inside it, confirm the announce path.

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
  write valid JSON. Each candidate is proven on controlled inquiries before
  it becomes the default.
- **Sub-agent handoff was proven concurrent but is not used here.** One-shot
  jobs give the same isolation with a simpler lifecycle and an explicit
  timeout; sub-agents stay a fallback.

---

## 6. Questions for Kolo to verify before Stage A

1. Does a command-kind cron job created by us persist across Kolo's
   reconciliation of the pod, and does it appear as a Routine in the portal?
2. Can a command job's script create a one-shot agent job, and does that job
   inherit nothing from the owner's chat session?
3. What does a command job's announce look like in the owner's chat, and is
   `NO_REPLY` honored?
4. Does `kolo notify-owner --file` show a PNG inline in the owner's Kolo chat?
5. What exactly is delivered to the chat session on Approve, Edit Intent, and
   Reject, and do briefs expire?
6. Does Maton's calendar passthrough deliver invitations to attendees, and
   does the response include the event ID?
7. Does `openclaw infer image generate` work from a command job or worker
   with the gateway token available there, and which image model is default?
8. Is there any per-org limit on the number of one-shot jobs created per day?

---

## 7. What does not change

The reply-in-thread invariant, the content guard before every customer send,
the claim journal and stale-claim recovery, the private record with its Kolo
mirror, the hash-bound configuration, and the rule that a conversational
"yes" is never an approval.
