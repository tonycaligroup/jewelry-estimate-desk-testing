# Live defects, replayed locally

One file per defect seen on the instance. Each carries what was seen (the
customer's words, the model's reading, the record shape), what the desk
did wrong, and what it must do now. `tests/test_live_fixtures.py` replays
every file here on every run, so a defect fixed once stays fixed, and a
new defect is captured the same day it is seen, before the fix.

Name: `YYYY-MM-DD-<slug>.json`. Customer identities are replaced (names,
addresses); wording is kept as close to the original as the record allows,
and `text_is` says `verbatim` or `paraphrase`.

Two kinds:

- `reading_check`: pure. `messages` (customer bodies, oldest first),
  optional `shop` text, `specification` (the model's reading), and
  `expect.topics` (the disagreements the check must raise; `[]` for none).
- `golden_path`: runs the whole desk on the fake world. `profile` holds
  rate-card overrides; `steps` run in order, each a dict with one of:
  `estimate_sent` (`text`, `spec`: a first inquiry priced, approved, and
  sent), `customer` (`id`, `text`, `design_change`, then a tick),
  `answer` (the owner's words to the open question), `tick` (with `spec`,
  what the model reads now). Each step may carry `expect`:
  `outcomes` (the tick's inline outcomes), `sent` (emails sent so far),
  `claim_status`, `decision`, `missing_required_fields`, `pieces` (count),
  `piece` (`index` and `has`: facts that piece must carry), `record_thread`,
  `card_title_contains`, `no_new_card`, `assumptions_contain`,
  `assumptions_count`, `model_quantity_prompts` (per-piece quantity calls
  the model received during the step).

Keep a defect's `seen_on` version and `fixed_in` version in the file, so
the history reads without git.
