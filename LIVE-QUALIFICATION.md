# Live qualification

The local suite proves the code; it cannot prove the real model's reading,
the instance's environment, or a delivery. Those are proven live, on the
test instance, with this short set. Before each run, write the expected
outcome (the table below); after it, keep the receipts.

## Before a run

1. Publish and install; readiness prints `READY` and the version. Record
   the version: the installed one, not the repository's.
2. Reset or rehearse as the run needs (`rehearsal.py --on --address`, or
   `customer_state_reset.py`); note which.
3. Read the expected outcome for the case below before sending anything.

## The set

| # | Case | Send | Expected, before the run | Receipts |
|---|---|---|---|---|
| 1 | First inquiry, complete | one email with every fact | one price card within a tick; no follow-up; approve → plain-text estimate with one total; record `estimate_sent` | brief id, sent Gmail id, tick log line |
| 2 | First inquiry, incomplete | one email missing two facts | one follow-up asking both at once, nothing else; reply → price card | follow-up Gmail id, then brief id |
| 3 | Rendering and revision | "renderings please" after 1 | previews arrive one view per tick (a slow view may take two ticks; no cron timeout notice), then one rendering card; reject with a change → only the named piece re-rendered, a fresh card with the Revised row | brief ids, image count per view |
| 4 | Change after the estimate | "make it 14k rose gold" | owner question → "change" → one updated price card; estimate says "updated" | question code, brief id |
| 5 | Second piece | "also quote a band" | owner question → "second piece" → one card with two lines; the first piece's grams, hours, stones identical to case 1's card; shipping once; title whole | both brief ids, side by side |
| 6 | Same customer, new thread | new subject, "same ring as before" | owner question → "same" → the estimate continues on the new thread, no second record | question code, record route_history |
| 7 | Time outside the hours | "Saturday 6 pm" | offer card row "(outside your hours: …)"; email states the hours and offers open times; nothing booked outside | brief id, sent Gmail id |
| 8 | Rehearsal on | any of the above from the owner's address; one real-looking mail from another address | the owner's mail runs with [REHEARSAL] everywhere; the other is held, released on `--off` | held item in the doctor, release line |

## After a run

- The doctor prints `state: clean`, or every open item is explained; a `tick_killed` line names a tick the platform killed and what it was doing.
- Nothing else on the pod is generating images during a rendering case: the platform CLI runs one command at a time, and a chat generating images in the background stretches or fails the desk's calls.
- Every card and email is accounted for in the audit trail; nothing sent twice.
- A defect found live becomes a fixture under `tests/fixtures/live/` the
  same day, before the fix (see the README there).

## Status

| Version installed | Cases run | Result | Date |
|---|---|---|---|
| 4.13.2 | 1, 3, 4, 8 | passed; 5 failed (re-asked known facts, re-priced the first piece) → 4.13.3, 4.13.4 | 7 Sep 2026 |
| 4.13.4 | 5 | passed (briefs #45/#46: first band frozen to the cent, shipping once, title whole); defect: twin labels "(men's wedding band)" twice → fixed in 4.13.5; defect: the same band in rose gold was weighed again → twin rule, fixed locally | 7 Sep 2026 |
| 4.13.4 | 6, 7 | pending | |
