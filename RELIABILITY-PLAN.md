# Reliability plan: failures that explain themselves and heal on retry

Written 5 September 2026 after a day of fixing failures one at a time. Every
one of them had the same shape: a command did several external things in a
row, one failed, and the desk left a half-done state with nothing to tell
the owner and nothing to run. Then the main Kolo session invented a story.
This plan removes the shape, not the instances.

## 0. Status

- Step 1, the harness: done (4.5.0). 84 combinations.
- Step 2, journaled executors, verified sends, lease, failure question: done (4.5.0).
- Step 3, bounded stuck in the tick, card verification, answer replay: done (4.6.0). Every combination ends ok or recovered.
- Step 4, doctor and requeue, readiness state line: done (4.7.0).
- Step 5, SKILL.md rules and coherence tests: done (4.7.1). SKILL.md also lost its agent-era runbook (64.5 KB to 43 KB).
- Next: section 7, speed and efficiency, each change with its measurement in place first.

## 1. What we are protecting

Three promises, in priority order. Everything below serves them.

1. **Never twice.** No customer email is sent twice, no calendar event is
   created twice, no card is filed twice for the same decision.
2. **Never silent.** Anything the owner approved or a customer is waiting on
   ends in exactly one of: done, or one plain-English question to the owner
   with the one thing to run or say.
3. **Never improvised.** The main session never has to guess at state or
   repair files. Every situation it can meet has a command, and the desk's
   own files, not a narration, are the truth.

## 2. Where failures come from

An inventory of every external effect, so nothing is designed by anecdote.

| Actor | External effects, in the order they happen |
|---|---|
| Tick (inline) | Gmail read; model calls; Gmail send (follow-up); Kolo card; Kolo notice (question, preview); image generate and describe; calendar free/busy read; record and claim writes |
| `send-approved-estimate-brief` | Gmail send; record; update-brief |
| `send-approved-rendering` | reopen claim; Gmail send with attachments; record; finish claim; update-brief |
| `book-approved-appointment` | free/busy read; **calendar event create**; calendar event delete (reschedule); Gmail send; record; question supersede; update-brief |
| `send-approved-times` | Gmail send; record; question supersede; update-brief |
| `answer-question` | claim resume; model calls; price; Kolo card; Gmail send (ask again); record |
| Watcher housekeeping | audit-trail read; reminders (Kolo notice); stale-claim reconcile; worker sweep |

Failure classes, with today's examples:

- **Transient**: gateway timeout, Kolo's command runner killing a long
  command, a model returning garbage once. Correct response: run the same
  command again, and it must be safe to.
- **Deterministic refusal**: a state guard ("claim is not in an allowed
  state"), the windows gate, a binding mismatch, "no longer free". A retry
  cannot help; a person must decide. Correct response: one question with
  options.
- **Partial completion**: a crash between two effects (event created,
  confirmation not sent). Correct response: resume from the last completed
  step, or release what was done, never start over blind.
- **Silent stuck**: a claim deferred every tick forever, a claim parked with
  no question anyone will answer, a silent manual review. Correct response:
  after a bounded number of attempts, one question.
- **Misinformation**: Kolo summarising a record and inventing a rendering
  delivery, or calling live queue items corrupted. Correct response: give
  Kolo commands that print the truth, and a rule never to narrate.

## 3. The design

### 3.1 Every executor is a journaled sequence of steps

Each executor declares its steps in one fixed order:

1. **Verify**: everything that can refuse, with no side effects. Claim
   state, approval binding, record state, windows, free/busy.
2. **Reversible writes**: the calendar event. Journaled with the provider id
   the moment it exists.
3. **Irreversible send**: the customer email. Already journaled pending, sent,
   uncertain by `gmail_safe`, with the exact payload kept for reuse.
4. **Record**: the private record, then the Kolo mirror.
5. **Report**: update-brief executed, supersede the dormant question.

A per-command journal (`work/<command>-<key>/run.json`, private) records each
step as `started`, `done` (with receipts), or `failed` (with the error). On
every run the executor reads the journal first and skips steps that are
done. So the same line pasted twice is a no-op, and pasted after a crash
resumes at the step that did not finish. This is what the booking got today
(`calendar-event.json`); it becomes the pattern, with one helper, not four
hand-rolled versions.

Two runs of the same command at once (the line pasted twice, or a retry
from `answer-question` while the first paste is still running) are handled
the way claims already are: the helper takes a **lease** on the run journal
(an exclusive create of `run.lock` with a token and an expiry, the same
mechanism as the claim lease) before reading it, and refuses with "another
run of this command is in progress" while a live lease exists. Within a
run, every step is **write-ahead**: `started` is written and flushed before
the side effect, `done` after it, so a crash between the two leaves a
`started` step, which is the ambiguous case below and never a silent
replay. Customer sends already work this way inside `gmail_safe`
(`acquire_external_action` marks the action pending under the claim lock
before the call); the run journal extends the same rule to the calendar and
to the report steps.

The ambiguous case is a crash inside the irreversible step: the journal says
`started`, the provider may or may not have sent. `gmail_safe` already marks
that `uncertain`. The rule: an uncertain send is never blindly retried. The
desk can usually answer the question itself, because it built the MIME
message and so knows its own `Message-ID` header: on the next run it
fetches the thread (read-only) and looks for a message carrying that id.
Found: the send happened; record the receipt and continue. Not found, and
the thread was read successfully: the send did not happen; resend the same
journaled payload. Only when Gmail itself cannot be read does the owner get
the question: "I cannot tell whether the confirmation reached Pat; look at
the thread. Reply 'sent' and I will record it, or 'resend'."

### 3.2 A failure is a question, with the fix attached

One function every executor and the tick's owner-facing paths call:

```
fail(kind, brief_id, step, error, retry_command, options)
```

It does four things, in order, each idempotent:

1. Marks the brief `failed` in Kolo with a structured result (step, reason).
2. Writes the failure into the run journal.
3. Files one decision question (kind `command_failed`) with fixed options:
   `retry` (the desk runs the journaled command again from inside
   `answer-question`, so the owner never needs the execute line),
   `release` (undo the reversible writes: delete the desk's own event by its
   journaled id; nothing else), and `handle myself`.
4. Delivers it once: the journal records that this step's failure was
   reported, so a second identical failure does not send a second message.

The text is plain: who the customer is, what the desk was doing, what it
managed to do ("the time is held on your calendar"), what it did not do
("the confirmation was not sent"), and the three replies.

What never becomes a question: the tick's own retries (transient model or
gateway trouble on the first attempts), housekeeping, and anything the
owner did not initiate and no customer is waiting on. Those stay in the run
summary, as today.

### 3.2a Which paths are journaled, and when

"Every executor" is a claim only once section 5 is complete. Precisely:

| Path | Effects | Covered by |
|---|---|---|
| The four card executors (`send-approved-estimate-brief`, `send-approved-rendering`, `book-approved-appointment`, `send-approved-times`) | send, calendar, record, report | Step 2 of section 5: the run journal and `fail()` |
| `answer-question` outcomes that act: price after a rate, the offer card after a rejection, "ask again", "skip" | model calls, card, send, record | Step 3: the same helper wraps each outcome as a journaled run keyed by question id, so a replayed answer resumes rather than repeats (today's replay rule for rate answers becomes the general case) |
| The tick's inline sends and cards (follow-up email, price card, rendering card, appointment card) | send, card, notice | Already keyed by the claim's external-action journal; step 3 adds the bounded-attempt rule and the `stuck_claim` question |
| Watcher housekeeping (audit poll, reminders, stale reconcile, worker sweep) | reads and notices | Reads are harmless to repeat; notices are deduplicated by the journal's reported flag; nothing else needed |

Until step 3 lands, `answer-question` keeps today's behaviour (safe to run
twice for rate answers and decisions; the offer-card outcome files a second
card if run twice after a crash), and the doctor names that as a known gap.

### 3.3 Stuck is bounded

The claim journal gains an `attempts` counter per phase. The tick's rules:

- A transient failure retries on later ticks, up to 6 attempts (about
  twelve minutes on a two-minute schedule), then asks.
- A deterministic failure (a `ValueError` from a guard) retries once (the
  stale reconciler's resume), then asks.
- The question, kind `stuck_claim`, names the customer, quotes the newest
  message, says what failed in plain words, and offers `retry`, `skip` (treat
  as handled; the desk closes the claim), and `handle myself`. The claim
  parks as `awaiting_owner` behind it, exactly like a rate question, so it
  is visible in `open-questions` and gets the one-day reminder.

Silent manual review remains only for the cases the owner asked to be
silent (a coworker's calendar invite, junk mail). A stuck customer email is
never one of them.

### 3.4 A doctor, and a way to hand the desk an email

`doctor.py --workspace WS`: read-only, seconds, prints one line per finding
and the exact command that repairs it. Findings, each from a real incident:

| Finding | Repair line it prints |
|---|---|
| A journaled calendar event with no booking on the record | the execute line to resume, or `answer-question … release` |
| A claim parked (`awaiting_owner`) with no open question for it | `requeue` the message |
| An open question whose claim is gone | `answer-question … handle myself` to close it |
| A pending card in the registry whose claim is gone | "reject this card in Kolo" (the desk cannot withdraw it) |
| An external action `pending` or `uncertain` | the resume line, or the uncertain-send question |
| A processing claim with an expired lease and no worker | nothing; the next tick resumes it (say so) |
| A queue item with no claim folder | `requeue` |

`inbox_monitor.py requeue --message-id ID`: fetches that Gmail message
through the normal path, validates it is a real message in a thread the
desk is allowed to read, and puts it in front of the next tick. Refuses a
message already processed. This is the sanctioned answer to "the desk
missed an email"; Kolo will never again need to write a queue item by hand.

The readiness check gains a last line: "state: clean" or "state: N findings,
run doctor".

### 3.5 The main session gets three rules and nothing else

Added to SKILL.md's hard rules:

1. When a command fails, run the same line once more. If it fails again,
   paste the output and wait; the desk will have asked the owner already.
2. Never summarise a record, a queue, or a claim from memory. Run `doctor`
   or print the file, and show what it says.
3. Never write, rename, or delete anything under `estimate-desk/`. If
   something looks wrong and `doctor` has no line for it, say so and stop.

### 3.6 Fault injection: the test that finds the next ten

The golden-path harness already fakes Gmail, Kolo, the calendar, and the
model by contract. It gains two switches: `fail_next(service, times)` (the
next N calls to that service raise as the real one would) and
`crash_after(service)` (the call succeeds, then the process is "killed": an
exception after the side effect). A generated test runs every scenario in
the golden path once per external call per switch, and after each run
asserts the three promises directly against the fakes:

- customer sends per delivery key ≤ 1; calendar events per slot ≤ 1; cards
  per decision ≤ 1;
- the run ended in success, or in exactly one open question with a
  runnable line, and re-running that line (or answering `retry`) completes
  it without a second side effect;
- the owner heard nothing else.

This is the piece that stops the one-at-a-time work: a new executor or a
reordered step that breaks a promise fails here before it reaches a pod.

## 4. What this does not change

Nothing a customer sees. No new setup question. No new owner channel. The
execute line on every card stays the same line. Records, claims, and the
Kolo mirror keep their shapes; journals are new files beside the work.

A work folder without a run journal is **not** treated as untouched. The
effects the old code could leave behind are already durable elsewhere or
are reconstructed on first contact: customer sends live in the claim's
external-action journal (pending, sent, uncertain) and are honoured as
today; calendar events created by 4.4.2 or later live in
`calendar-event.json`; and for a booking work folder from before 4.4.2 with
an approval on record and no booking, the first run under the new code
checks free/busy for that exact slot before creating anything and, if the
slot is taken, stops and asks the owner whether the event on the calendar
is the desk's own ("release" deletes nothing here; the owner removes a
pre-journal event by hand, once). The doctor reports every such folder on
the day the new version is installed, so the migration is a list, not a
surprise.

## 5. Order of work

1. **Harness first**: the fault-injection switches and the three-promise
   assertions over the existing golden path. This will fail on today's
   code in several places; those failures are the worklist.
2. **Journal and resume** for the four card executors, with `fail()` and
   the `command_failed` question. Uncertain sends become the question.
3. **Bounded stuck** in the tick, with the `stuck_claim` question and
   parking.
4. **Doctor and requeue**, and the readiness line.
5. **SKILL.md rules** and the coherence tests that pin them.

Each step is its own version, tested on the pod before the next. Estimated
at a working day for the code, plus one clean-slate run on the instance to
prove it with the real gateway.

## 6. Risks and how they are held

- **Over-notifying.** Mitigated by the journal's reported flag (one message
  per step failure), by keeping tick internals out of chat, and by the
  bounded attempt counts before a stuck question.
- **Releasing the wrong event.** `release` deletes only an event whose id the
  desk journaled itself, never anything found by time on the calendar.
- **A resume sending stale mail.** Sends reuse the exact journaled payload
  and refuse on a binding mismatch, as today.
- **Journal drift.** Step names are constants shared by executor and test;
  the fault-injection suite runs every step, so a renamed or reordered step
  fails a test, not a customer.
- **Kolo ignoring the rules.** Rules reduce improvisation; commands remove
  the need for it. The doctor is the real defence: when there is a line to
  run, the session runs it.

## 7. Usability, speed, and efficiency

Reliability is the floor. These are the three things the owner feels every
day, each with a target the desk can measure about itself.

### 7.1 Usability: the owner decides from one screen

- **Every card decidable from its title.** SMS shows the title only, so the
  title carries the piece, the price, the cost and profit, or the time.
  Already true for price and booking cards; the rendering card title gains
  the checker's verdict ("2 views passed") and the offer card gains the
  count of times.
- **Every question answerable in a word.** Each question ends with the two
  or three replies that work, in the owner's language, and the desk matches
  loosely (today's `match_option`). No codes to copy unless Kolo has its own
  question open, and section 3.4 removes most of those.
- **One place to look.** Cards in Approval Required, questions and previews
  in the activation thread, nothing else anywhere. The doctor is the one
  command for "what is going on".
- **The customer feels a jeweler.** Every email reacts to what they said,
  asks at most three things, invites them in, and is reflowed to clean
  paragraphs (4.4.0, 4.4.1). The meeting comes before the questionnaire.
- Measure: a weekly line in the run summary, "N cards, N questions, N
  notices," where notices other than previews should be zero.

### 7.2 Speed: minutes from email to a decision in the owner's hand

Today: the watcher runs every two minutes; a new inquiry takes two to
three model calls (triage, extraction, follow-up or quantities), each ten
to thirty seconds on the inline model; a rendering adds one to three
minutes for two views and their checks; executors finish in seconds
because the email was drafted when the card was filed.

Targets and the changes that reach them:

- **Inquiry to card or follow-up: under three minutes** from arrival.
  Keep the two-minute schedule (one-minute doubles the idle cost for a gain
  the owner will not notice); the time is in the model calls. Triage and
  extraction can be one call when the thread is short, and the follow-up
  can be drafted in the same call as extraction when fields are missing.
  Two calls instead of three or four on the common path.
- **Reply to card: under two minutes.** Post-estimate classification is one
  call already; the appointment path adds one for the times. Keep.
- **Rendering: under two minutes for two views.** Render the two views in
  parallel (two subprocesses), check them in parallel, regenerate at most
  one. Today they run one after another.
- **Executors: under five seconds.** Already the design; the journal in
  section 3.1 keeps it that way because nothing is drafted or fetched at
  approval time.
- Measure: the tick summary records per-claim wall time and per-call model
  time; the run summary keeps a rolling median. A claim over five minutes
  is a finding for the doctor.

### 7.3 Efficiency: cost per customer, and the owner's attention

- **Model tokens.** The thread digest is capped at 12,000 characters and
  sent once per call; merging triage with extraction and the follow-up with
  extraction cuts the common path from three or four calls to two. Triage
  for a reply on an open estimate is skipped entirely (4.3.4). Estimated
  cost per inquiry on the inline model stays under a few cents; the
  numbers go in the summary so drift is visible.
- **Image calls.** Two views, one regeneration at most, checks by the
  cheap vision model. Never a third view, never a re-render after approval.
- **Kolo calls.** The audit trail is polled only while a card the desk
  filed is still pending (today it is polled every tick regardless); when
  the registry has nothing pending, the tick makes no audit call.
- **Idle cost.** An idle tick is one Gmail discovery call and nothing else,
  about five seconds, no model. Outside the owner's monitoring hours the
  job does not run.
- **Owner attention.** The scarcest resource. The count of messages the
  owner receives per customer is the number to watch: one follow-up
  decision at most, one price card, one rendering card, one or two
  appointment cards, and questions only when the desk genuinely cannot
  proceed. Anything above that is a bug by definition and the weekly line
  in 7.1 makes it visible.

Speed and efficiency changes ship after the reliability steps in section 5,
as their own versions, each with the measurement in place first so the
gain is a number and not an impression.

## 8. Review, 5 September 2026

An independent review of this plan raised four points; all four are taken.

1. **Concurrency of journal-based idempotency.** Two runs could both see a
   step as not done. Answered in 3.1: a lease per command run, and
   write-ahead `started` before every side effect, the same mechanism the
   claim journal already uses for sends.
2. **Legacy folders treated as fresh starts.** Answered in section 4:
   absence of a journal is not proof of nothing done; old sends are already
   journaled on the claim, old calendar work is checked against free/busy
   before anything is created, and the doctor lists every pre-journal
   folder at install time.
3. **"Every executor" was wider than the first step delivered.** Answered in
   3.2a: a table of which paths are covered by which step, and what the gap
   is until then.
4. **Uncertain sends resolved by the owner's word alone.** Answered in 3.1:
   the desk verifies against Gmail first using its own Message-ID; the
   owner is asked only when Gmail cannot be read.
