# Architecture options: what would make the desk succeed by construction

Written 6 September 2026, after four days of building and testing on live
pods. Method: every incident sorted by cause; a first round of options
against those causes; a critique of each option against the business
workflow (WORKFLOW.md, the source of truth), against the platform facts we
have verified, and against its own failure modes; a second round with the
options that survived, revised; then a decision. The critique is kept in
the document so the reasoning can be checked, not only the conclusion.

## 1. Every failure we met, by cause

Forty-odd incidents, five causes. The count is what matters.

| Cause | Incidents | What it looked like |
|---|---|---|
| **The main Kolo session improvised.** It has a shell, it reads our files, and every approval and answer passes through it. | 11 | Hand-edited a record; sent a customer email with curl; wrote queue items by hand, called them corrupted, deleted them; took "New" as consent to its own question; invented "rendering delivered" in a summary; offered to hand-send one of two images; ran a line twice; retried a failed line with a different answer. |
| **Long or multi-step work died part way and nothing said so.** | 9 | Event created, confirmation not sent; a send whose outcome was unknown, then resent; an executor killed by the command runner; a claim deferred every tick; a rendering handed to the worker for a reason nothing recorded; a crashed tick left as a silent manual review. |
| **The model read the customer wrong on a fact that moves the price.** | 8 | A phantom 1 ct center stone on a pave signet; natural melee assumed; a mother's diamond asked for its grade twice; one piece where the customer said two; "The setting?" headings; a canned estimate; wrapped lines. |
| **The prompt-driven worker agent acted on its own reading of a runbook.** | 4 | A rendering sent with no card; a Sunday on a booking card; group shots of three pieces; a runbook that said "needs no new approval". |
| **The version on the pod was not the version we tested.** | 6 | Fixes verified locally, then the same symptom on the instance a day later; publish stalls; uninstall and reinstall by hand; a reset refused by an old allow-list. |

Three things are already true and stay true under every option below:
nothing customer-facing leaves without a card; nothing is sent, booked, or
filed twice; nothing is stuck silently. The fault harness proves those on
every commit. What is not yet true is that the two largest causes cannot
recur, because both live outside our code: an LLM agent between the owner
and the desk, and a deployment step done by hand.

## 2. The workflow rules every option is checked against

From WORKFLOW.md, the lines that constrain design:

- **6.10, two ways to reach the owner.** Permission to act is an approval
  brief the owner approves, edits, or rejects from the card. A fact only
  the owner has is a plain-English question in the channel, answered in
  words. "A review list is not one of the ways."
- **5 and 6.4.** No price, booking, or rendering reaches a customer without
  the owner approving that exact one; a conversational "yes" is not
  approval; an edited number is a new candidate and needs a fresh brief.
- **6.2.** The specification gate asks the customer for what is missing,
  once, in the original thread; nothing is invented; origin is never
  delegated when the shop says ask.
- **10.** The owner's channel may be a phone; only finalized, important
  messages and questions the desk cannot proceed without; never narration.
- **12.** Workflow changes are made in that document first.

## 3. Round one: the options as first drafted

- **A. The desk executes approvals itself**, reading `brief.approved` from
  the audit trail the way it reads rejections, and running the card's own
  executor in the tick. The session does nothing on an approval.
- **B. Owner questions with fixed answers become cards** (same/new,
  skip/ask again, retry/release), approve meaning the first answer.
- **C. Long work leaves the tick**: renderings run in a one-shot command
  job with its own budget, pieces in parallel.
- **D. Retire the worker agent**; after bounded retries the owner is asked.
- **E. Two readings of the price-moving facts**; a disagreement becomes a
  confirming question to the customer.
- **F. A versioned pod and a rehearsal mode**: readiness fails on a version
  mismatch; a rehearsal routes every customer email to the owner's own
  inbox under a tag, so every install is proven end to end before going
  live.
- **G. Move the desk off Kolo** to a small service with its own approval
  surface. Noted for completeness.

## 4. The critique

Each option, three questions: does it break the workflow; what does it
assume about the platform that we have not seen; how does it fail.

**A.** Workflow: conforms; the owner still approves the brief from the
card, only the hand that runs the line changes. Platform: `brief.approved`
events exist with brief ids (seen in the audit trail on 5 September); what
we have **not** seen is whether an edited number rides on that event.
Failure modes: the session also receives "APPROVED, execute now" and may
run the line anyway (the lease refuses the second run, the journal makes
the outcome identical, so this is harmless); the trail lags a tick
(registration has always found its event by the next tick); an edit the
desk cannot see would execute the original price, which 6.4 forbids.
**Revision:** execute only approvals whose event carries no note and no
changed payload; anything else becomes a question to the owner ("Brief #N
was approved with a change; reply 'send as approved' or 're-price'").
Verify the edit shape on the pod once before shipping.

**B.** Workflow: **breaks 6.10.** A fact only the owner has is answered in
words, and a card is for permission to act. Dressing a question as an
approval also invites exactly the confusion we saw with "New". **Dropped.**
What survives from the idea: every question already carries its two or
three acceptable replies and a one-day reminder, and Option A removes most
of the session traffic that caused the collision.

**C.** Workflow: mechanics only; conforms (the rendering card is unchanged,
6.6 and 5.7). Platform: the tick already spawns one-shot **agent** jobs;
whether a one-shot **command** job started by the tick has the gateway
token is unverified. Failure modes: the job never starts (the claim's lease
lapses; the next tick spawns once more, then asks); the job dies mid-way
(same); two ticks spawn two jobs (the claim lease prevents it). **Revision:**
one live check of a command child job before building; the job writes its
own journal like an executor; the doctor lists jobs in flight.

**D.** Workflow: conforms and strengthens 5.7 and 6.7 (no prompt ever
authors a send, a booking, or an image again). Failure modes: none new; the
fallback becomes the bounded-retry question that already exists. Depends on
C, since renderings were the worker's last real job.

**E.** Workflow: conforms with 6.2 (the gate asks; a confirming line is an
ask) and with "nothing invented". Failure mode: two calls to the same model
can misread the same way. **Revision:** the second reading is not a second
model call but a deterministic cross-check against the customer's literal
words: the count of distinct ring sizes, piece nouns ("ring", "band",
"pendant", "earrings"), carat figures, and origin words in the thread. When
the model's reading has fewer pieces than the text has sizes, or an origin
the text never says, the follow-up confirms it with the customer before
any price. No extra model call; the check is code and is tested like the
gate.

**F.** Workflow: the version half is mechanics; the rehearsal routes a
customer email to the owner, which 6.5 forbids for real customers, so
rehearsal must be impossible to mistake for live. **Revision:** rehearsal
is a profile flag that readiness prints in capitals on its first line, that
every card title and every subject carries, and that the run report
repeats every tick; the rehearsal inquiry comes from the owner's own
address so the customer identity is the owner. Off by default.

**G.** Would remove the session and the publish step, and with them the
cards the owner uses from a phone and the chat they answer in. Too much for
the gain; the other options reach the same guarantees inside Kolo.

## 5. Round two: the surviving options, revised

| Option | Removes | Assumes (to verify on the pod once) | Effort |
|---|---|---|---|
| **A'** The desk executes clean approvals from the audit trail; an approval with a note or a change becomes a question | The session as executor: 9 of 11 improvisation incidents | The shape of an edited approval in the trail | half a day |
| **C'** Renderings run in a journaled one-shot command job, pieces in parallel, doctor-visible | The 300-second tick kills and the last reason for the worker agent | Gateway token inside a command child job | half a day plus one live check |
| **D** No worker agent; bounded retries then a question | Every prompt-authored action, now and later | nothing | an hour plus tests |
| **E'** Deterministic cross-check of price-moving facts against the customer's words; a mismatch is confirmed with the customer in the follow-up | The single-read misreads: 6 of 8 in that row | nothing | half a day |
| **F1** Version on every readiness, doctor, and tick line; readiness fails on a mismatch with the expected version | The "which version is this" conversations | nothing | an hour |
| **F2** Rehearsal mode, loudly marked, driven by a scripted inquiry from the owner's own address | Untested installs reaching customers | nothing new | a day |

The two remaining improvisation incidents (the "New" collision and a
retried line with a different answer) are about owner **words**, which the
workflow says must stay in chat; they are held by the SKILL.md rules
already in place and by A' cutting the session's traffic to answers only.

## 5a. Answers from the pod, 6 September 2026

Two facts asked of Kolo before building, both answered on the pod.

1. **An approval in the audit trail carries no note and no edit.** The
   `brief.approved` event's details are exactly `source`, `status`,
   `previous_status`; a full lifecycle shows submitted, approved, executed,
   nothing else. If the owner edited the number or wrote a note before
   approving, the trail does not say so. So the desk cannot tell a clean
   approval from an edited one by reading the trail.
2. **A command job has the gateway credential at run time.** The watcher's
   job definition has no environment block and no agent id, yet a command
   job that printed `MATON_API_KEY present` got `yes`: the gateway injects
   the credential when the job runs. A one-shot command job spawned by the
   tick with the same shape (`kind: command`, `sessionTarget: isolated`,
   `argv: sh -lc python3 ...`) will reach Gmail, the calendar, and the
   image tool the way the watcher does. The watcher's own budget is
   `timeoutSeconds: 300`, which is the kill C' removes from the rendering
   path.

**What this does to A'.** The workflow (6.4) lets the owner edit the number
on a price brief, and an edited number must produce a fresh brief. The
desk cannot see an edit in the trail, so it must not execute a **price**
card from the trail. Every other card is a yes or no with nothing to edit:
renderings, a booking, an offer of times. A' therefore lands in two tiers:

- **Tier 1, now:** the desk executes rendering, booking, and offer
  approvals from the trail. That is where the improvisation incidents were
  (a hand-sent image, a half-done booking, "which image do you mean").
- **Tier 2, after one experiment:** the price card. The experiment is the
  owner editing the number on the next price card before approving, then
  showing what Kolo delivered to the session and what the trail recorded.
  If the delivered decision carries the edited payload, the session's
  rule for a price approval becomes "run the line only when the delivered
  price equals the card's; otherwise reject and tell the desk to re-price",
  and the desk keeps its hands off price cards. If an edit turns out to
  produce its own event or a new brief, tier 2 folds into tier 1.

**What this does to C'.** Builds as written, with the watcher's own job
shape: a one-shot command job, isolated session, no announce, a longer
budget (900 seconds), deleted after its run.

## 5c. Closed by the owner's rule, 6 September 2026

Cards are binary: approve or reject, nothing else (WORKFLOW.md 6.4, roles,
6.10). `kolo --help` on the pod lists no command that reads a brief back
(only `request-approval`, `update-brief`, and `audit-query` touch briefs),
so an edit could never have been verified anyway. Tier 2 therefore folds
into tier 1 without an experiment: the desk executes price approvals from
the trail like every other card, the session runs nothing on an approval,
and an owner who wants another number rejects the card and answers the
desk's question with the price; a fresh binary card follows (4.11.0).

## 5b. Built

4.10.0, 6 September 2026: A' tier 1, C', D, and F1, on the full suite and
the fault harness. The price-card experiment (tier 2) and E', F2 remain.

## 6. Decision

Build A' tier 1, C', and D as one architecture change, with F1 in the same
release; run the price-edit experiment on the pod; then E'; then F2; A'
tier 2 when the experiment says how. Each step a version, green on the full suite
and the harness before the next; A' and C' each preceded by their single
live check on the pod.

After A', C', D, and F1, the honest statement is: the desk cannot send,
book, or file anything without a card; cannot do it twice; cannot fail
silently; executes the owner's clean approvals itself within two minutes
and asks about any other; runs nothing through a prompt-driven agent; and
says which version it is on every line it prints. E' narrows judgment on
the facts that move the price. F2 proves each install before a customer
sees it. What remains is the platform itself (publish, SMS, the model),
which no design on our side controls, and the owner's words, which the
workflow keeps in chat on purpose.

Estimated effort: about two days for the first release, a day for E', a
day for F2.
