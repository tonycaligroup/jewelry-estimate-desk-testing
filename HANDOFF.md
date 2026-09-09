# Jewelry Estimate Desk — handoff

Updated 4 September 2026, 03:30 UTC. Everything below was verified by
execution on the test pod unless marked otherwise.

## State in one paragraph

Master is the build that ran the whole desk end to end on 3-4 September:
inquiry to price brief inside one watcher tick, plain-text estimate on
approval, rendering previews plus a card, bookings and offers with the
three appointment scenarios, rejections read from the Kolo audit trail and
answered by the owner in plain words, rescheduling, and a readiness command
for setup. PRs #61 through #72 merged tonight. 376 tests pass with
`python3 -m unittest discover -s tests`; `tests/test_golden_path.py` runs
the whole desk end to end on the real code (ticks plus the exact execute
lines from the cards) with only Gmail, Kolo, the calendar, and the model
faked, and six side branches beside it.

## Tests, CI, fixtures

- `python3 tests/run_all.py` runs the suite and the fault-injection harness
  in one go; `--results out.json` writes counts, timings, Python version and
  the commit; `-k words` runs only matching tests. The suite alone:
  `python3 -m unittest discover -s tests -q`; the harness alone:
  `python3 tests/test_fault_injection.py`.
- `.github/workflows/tests.yml` runs both on every push and pull request
  (Python 3.9 and 3.12) and keeps the results file as an artifact per
  commit. The skill audit runs there only when the repository secret
  `OPTIMIZER_TOKEN` (read access to `tonycaligroup/kolo-skill-optimizer`)
  is set; locally: `python3 ~/code/kolo-skill-optimizer/scripts/skill_audit.py .`
- `tests/fixtures/live/` holds one file per defect seen on the instance,
  replayed by `tests/test_live_fixtures.py` on every run. A live defect is
  captured there the same day, before the fix. Format in the README there.
- `LIVE-QUALIFICATION.md` is the short live set with expected outcomes
  written before each run, and the status of the latest runs.

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
- Before every publish, in the publishing folder: `python3 scripts/manifest.py`
  must print `OK <n> scripts match manifest <version>`; after every install, readiness's
  `installed scripts` line must PASS. On 8 September 2026 a pod ran the 4.14.2
  pipeline.py and estimate_record.py under a 4.14.5 SKILL.md for three releases;
  the version line alone proved nothing.
- Frontmatter version is `4.15.5` on master (bundled sends for cards born from one email; clock bounds in offers; the sheet skips non-inquiries; concierge mode by default: the call first, the owner's details in chat, one price card with renderings; the earring style question; times inside the days named; a bare day or a flat day-and-time after an offer; the vision confirmed from a photo; the center stone never an accent; the sender's own name; millimetre sizes; repeat pieces on file; the wider 'I don't know'; 49 scripts, manifest checked) (4.13.8 was the pod's republish of 4.13.7 after a stale publishing folder) (the team listing "jewelry-estimate-desk-testing"). Bump it with every publish.
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

- Cards are binary (6 September 2026): the desk executes every approval
  from the audit trail, price cards included; a rejected price card asks
  the owner for the price and files a fresh card at it (`price_next`).
- 4.13.0 (7 September 2026): renderings run one view per tick inside the watcher (`pipeline.render_step`, `rendering-progress.json`); the one-shot render job is gone; every owner answer returns in seconds (`_hand_to_tick`, `next-step.json`). See ARCHITECTURE.md's latest note.
- 4.12.0 (6 September 2026): rendering reject loop (`rendering_next`),
  reading check before pricing (`reading_check.py`), rehearsal mode
  (`rehearsal.py`), "change" and "second piece" reopen the estimate
  (`estimate_record.reopen_for_change`, `revision`, `estimate_history`),
  "same" moves the thread (`move_route`, `route_history`). See
  RELEASE-PLAN-4.12.md for the consumer tables.
