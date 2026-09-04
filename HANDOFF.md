# Jewelry Estimate Desk — handoff

Updated 4 September 2026, 03:30 UTC. Everything below was verified by
execution on the test pod unless marked otherwise.

## State in one paragraph

Master is the build that ran the whole desk end to end on 3-4 September:
inquiry to price brief inside one watcher tick, plain-text estimate on
approval, rendering previews plus a card, bookings and offers with the
three appointment scenarios, rejections read from the Kolo audit trail and
answered by the owner in plain words, rescheduling, and a readiness command
for setup. PRs #61 through #72 merged tonight. 331 tests pass with
`python3 -m unittest discover -s tests`.

## Where the code is

| | |
|---|---|
| Repo | `tonycaligroup/jewelry-estimate-desk-testing` |
| Local clone | `~/code/jewelry-estimate-desk-testing` |
| Test pod install | git checkout at `<workspace>/skills/jewelry-estimate-desk-testing` |
| Living platform notes | `KOLO-SKILL-PLAYBOOK.md` (Kolo capabilities, all verified or marked) |
| Design record | `ARCHITECTURE.md` (batches 1-6 at the end), `WORKFLOW.md` (business rules) |

## Packaging for the team marketplace

Kolo packages a skill as an archive of `SKILL.md`, `scripts/`,
`references/`, and `templates/`, unpacked on each instance to
`<workspace>/skills/<slug>` with `.clawhub/origin.json`. The slug and
version come from the SKILL.md frontmatter (`name:` and `version:`).

- The test pod also holds a stale marketplace copy, slug
  `jewelry-estimate-desk`, version 2.0.0, from 23 August. The main session
  ran scripts from that stale copy once by mistake. Publish the new build
  over that slug, or delete the old listing, so no instance can pick it up.
- Frontmatter version is `3.13.0` on master (the team listing "jewelry-estimate-desk-testing"). Bump it with every publish.
- `tests/`, `ARCHITECTURE.md`, `WORKFLOW.md`, `KOLO-SKILL-PLAYBOOK.md`,
  `HANDOFF.md`, and `TESTING-CHANGE-REPORT.md` are not needed on an
  instance; shipping them is harmless.
- SKILL.md must stay under 65,000 bytes (a test enforces it). It is 59.9 KB.

## First run on a brand-new instance

1. Install from the team marketplace.
2. Run the setup steps in SKILL.md (the profile questions, calendar and
   windows, rate card, activation binding, watcher cron, disabled).
3. `python3 <skill>/scripts/readiness.py --workspace <workspace> --base-dir <skill>`
   and fix every FAIL. It checks the profile, calendar and windows, the
   activation binding, monitor state, the judgment model from the watcher's
   environment, audit-trail access, the Kolo backend, and the watcher job.
4. Enable the cron. Send one complete inquiry from a test account and
   watch the price brief arrive in the setup thread.

Defaults that used to be hand-made: inline judgment is on unless
`estimate-desk/pipeline.json` says `{"inline": false}`; owner questions and
previews go to the setup thread unless the profile's `owner_channel` says
otherwise.

## How the owner works with it

- Approval cards carry the exact command in their payload; the main session
  runs that one line. Questions end with `desk-answer <CODE>`; the session
  runs `answer-question` with the owner's words.
- After rejecting an appointment card, the desk asks within a tick what to
  do. The owner answers in words: times to offer, "other times", or "handle
  myself". Times become a new offer card; nothing reaches the customer
  before approval.

## Known gaps

- A known customer writing on a new thread: "same" hands the thread to the
  owner instead of continuing the estimate.
- An edited price on a brief is rejected with a re-price note, not applied.
- The brief registry marks rejections; approved briefs stay "pending" in
  `estimate-desk/briefs/` (harmless, untidy).
- Rejected price briefs get a notice only; no follow-up question yet.
