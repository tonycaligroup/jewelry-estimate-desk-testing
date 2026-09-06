# Multi-piece plan: one email, two pieces, one estimate

Written 6 September 2026 after a test inquiry for an engagement ring in
one size and a wedding band in another was quoted and rendered as one
piece. The desk holds one specification per inquiry; everything downstream
reads it as one piece. This plan makes the specification a list of pieces
and follows that list through every consumer, without changing a single
path a one-piece inquiry takes today.

## 0. The rule

A customer who names more than one object gets one estimate with one price
and one card, in which every piece is its own line: its own type, size,
metal, stones, setting, cost, labor, and renderings. "Matching" or "a set"
means one design language across the pieces, never one size. A single
piece keeps the shape it has today; nothing about it moves.

## 1. Where the specification is read (the whole system)

Every consumer, found by search, with what it reads and what changes.

| Module | Reads | Change |
|---|---|---|
| `judge.extract_specification`, `triage_and_extract`, `check_specification` | model output → flat keys from `SPEC_KEYS` | accept `pieces`: a list of piece objects with the same keys; keep it only when it holds two or more; the prompt says when to use it |
| `spec_gate.missing_required_fields`, `has_stones` | flat spec | run per piece; return names prefixed `pieces.<i>.<field>` for a multi-piece spec, bare names for one piece |
| `estimate_record.enforce_specification_policies`, `stones_in_words`, `customer_supplies_stone`, `mark_jewelers_choice`, `followup_stalled` | flat spec, missing-field names | policies per piece (called by the gate with a piece); `mark_jewelers_choice` writes into the named piece; the stall guard compares name sets and needs no change |
| `pipeline._send_followup`, `prioritized`, `plain_followup`, `judge.draft_followup` | list of missing field names | names become labels ("engagement ring: finger size"); priority sorts on the field part; the prompt lists them under each piece |
| `cost_components.extract_metal`, `has_center_stone`, `extract_center_stone`, `accent_stone_needs`, `missing_accent_rates`, `prepare`, `missing_rates` | flat spec → one metal line, stone lines, one labor line | one metal line and one labor line per piece, stone lines per piece, line labels carry the piece ("14K yellow gold, engagement ring"); `fill` keys index the lines; `unresolved` names the piece; rate questions are asked once per rate key |
| `judge.choose_quantities`, `check_quantities` | catalogs, `fill`, one set of numbers | for a multi-piece spec the answer is `{"pieces": [{label, finished_grams, bench_hours, center_carat?, fees, accents}]}`, checked per piece; the one-piece shape stays |
| `workflow_safe.price`, `pipeline._price_after_review`, `price_from_record` | `finished_grams`, `bench_hours`, `center_carat`, `fees`, `accents` | a `pieces` argument fills line `i` from piece `i`; the flat arguments keep filling line 0 |
| `cost_components.finalize`, `approval_guard.build_internal_cost_sheet`, `owner_review`, `pricing_model.quote_price` | line lists | no change: they already sum lists of lines |
| `kolo_safe.approval_title`, `_assumptions`, `_piece_words` | `summary_of_piece`, review lines | the summary joins pieces with "and"; assumptions already list every line, now with piece labels |
| `owner_questions.summary_of_piece`, `missing_rate_text` | flat spec | summary per piece joined with "and"; the rate question names the piece the rate is for |
| `workflow_safe.estimate_email_facts`, `ESTIMATE_NOTE`, `customer_mail.KIND_BRIEFS["estimate"]` | spec lines, piece summary, one price | facts carry a per-piece specification; the brief says "name each piece, one total"; the fallback lists each piece under its own line |
| `rendering.spec_text`, `plan_render`, `build_prompts`, `run` | one spec → one archetype, two views | per piece: one plan, two views, checks; a set passes "matching set" into the planner and the prompts; at most four images |
| `pipeline.render_and_send`, `rendering_materialize`, `inbox_monitor.WORK_ARTIFACTS`, `workflow_safe._rendering_images`, `request_rendering_approval`, `send_approved_rendering`, `gmail_reply.build_reply` | slots 1 and 2, at most two attachments | slots 1 to 4 (`rendering-3.png`, `rendering-4.png`), the card lists every slot with its piece, the executor checks every slot's hash, the reply carries up to four images |
| `kolo_safe.build_request_rendering_approval`, `send_owner_preview` | images list | one preview per image with "view 2 of 4, wedding band" |
| Appointments (`appointment_card`, `_appointment_next_text`, confirmation facts) | piece summary | the joined summary; nothing else |
| `estimate_record.record_thread_review`, approval binding, mirrors, doctor, reset | the spec as an opaque object and its hash | no change; `pieces` travels inside the object |
| Tests: `test_golden_path.World.spec`, `IntakeTests`, `SpecGateTests`, `InlinePipelineTests`, fault harness | flat specs | untouched; a new two-piece scenario is added beside them |

## 2. The data model

`specification` stays a flat object. It gains one optional key:

```
"pieces": [
  {"piece_type": "engagement ring", "finger_size": "6", "stone_type": "diamond", "stone_origin": "lab-grown", "stone_carat": "2", "stone_shape": "round", "setting_style": "solitaire"},
  {"piece_type": "wedding band", "finger_size": "10", "notes": "plain, polished"}
]
```

Shared facts (metal, karat, color, finish, budget, event date, scheduling
intent, customer-supplied materials, reference images) stay at the top
level and apply to every piece; a piece may override any of them. One
helper is the only place this rule lives:

```
estimate_record.pieces_of(spec) -> list[dict]     # [spec] for one piece; merged top-level + piece for each entry otherwise
estimate_record.piece_label(spec, index) -> str    # "engagement ring", "wedding band", or "piece 2"
estimate_record.is_set(spec) -> bool               # the customer said matching or a set
```

`pieces` with fewer than two entries is dropped by the extractor's check,
so a one-piece specification never carries the key and every existing
code path sees exactly what it sees today.

Missing fields on a multi-piece record are named `pieces.<index>.<field>`
(the record's field-name rule already allows dots). One-piece records keep
bare names.

## 3. What the customer and the owner see

- **Follow-up**: one email, a short list under each piece heading in the
  customer's words ("For the engagement ring: what ring size?" "For the
  band: what ring size, and the same metal?"). Still one ask, everything
  at once.
- **Price card**: title "Price approval: an engagement ring in 14K yellow
  gold with a lab-grown diamond 2 ct and a wedding band in 14K yellow gold,
  quote $X, cost $Y, profit $Z (P%). Assumptions: 14K yellow gold
  (engagement ring) 4.5g x $65; bench labor (engagement ring) 4h x $90;
  14K yellow gold (wedding band) 6g x $65; ..." One card, one total.
- **Estimate email**: names each piece in a sentence, one total, the same
  high-side wording, valid-through date, and invitation.
- **Rendering card**: up to four views, two per piece, each preview named
  ("view 1 of 4, engagement ring, front"); the checker line per piece; one
  card; one email with up to four attachments on approval.
- **Everything else** (appointments, questions, doctor) unchanged; the
  piece summary they show becomes "an engagement ring ... and a wedding
  band ...".

## 4. Compatibility rules, checked as a whole

1. **One piece is bit-for-bit today.** Every helper defaults to `[spec]`
   when `pieces` is absent; field names stay bare; the price fill keeps the
   flat arguments; slots 1 and 2 stay the first two. All 469 tests must
   pass unchanged before the new scenario is added.
2. **Records in flight.** A record made before this version has no
   `pieces`; nothing reads one. A record made after it, opened by older
   code, would see a flat spec with an extra key; there is no older code
   on the pod once published, and the reset clears the rest.
3. **Bindings.** The approval binding hashes the specification object and
   the cost sheet lines; a multi-piece record binds its `pieces` and its
   per-piece lines exactly as a one-piece record binds its own. Nothing in
   `approval_guard` changes.
4. **Rendering gate.** More slots, same rule: no image reaches a customer
   without the card that lists every slot's hash; the executor refuses if
   any slot differs.
5. **Owner attention.** Still one price card, one rendering card, one
   follow-up, and rate questions asked once per rate, not once per piece.
6. **Fault harness.** Runs unchanged on the one-piece path (84 of 84), and
   once more over a two-piece price-and-render sequence added as a second
   action list, with the same three promises.

## 5. Order of work

Each batch is green on the full suite before the next; the version bumps
once at the end (4.9.0).

1. **Normalization and reading.** `pieces_of`, `piece_label`, `is_set`;
   the extractor accepts and validates `pieces`; the gate, the policies,
   the stall guard, and `mark_jewelers_choice` work per piece; the
   follow-up lists missing details under each piece. Tests: two-piece gate
   results, a follow-up naming both pieces, one-piece results unchanged.
2. **Pricing.** Per-piece metal, labor, and stone lines with piece labels;
   `fill` and `unresolved` indexed; quantities judged per piece; the price
   fill by piece; rate questions deduplicated; title, assumptions, and
   summary joined. Tests: a two-piece sheet sums the pieces; the title
   names both; the one-piece sheet is unchanged.
3. **Rendering.** Slots 3 and 4; per-piece plans and prompts; the set flag;
   the card, previews, executor, and reply carry up to four images. Tests:
   four images on one card, hashes checked per slot, one-piece stays at two.
4. **Emails.** Estimate facts and fallback per piece; the brief wording;
   the rendering note names the pieces. Tests: the estimate email names
   both pieces and one price.
5. **End to end.** The golden path gains a two-piece customer (ring size 6
   with a lab-grown center, band size 10, shared metal): follow-up asks
   both sizes, price card lists both and sums, estimate names both,
   renderings four on one card, approval sends four attachments, then a
   booking. The fault harness runs over that sequence too.
6. **Docs.** SKILL.md (one paragraph under the specification gate),
   ARCHITECTURE.md note, OWNER-GUIDE line ("two pieces in one email are two
   lines on one card").

About a day. Nothing ships to the instance until step 5 is green.

## 6. Risks and how they are held

- **The extractor splits one piece into two** (a halo is not a second
  piece; "a ring with a matching band" is). The prompt gives examples both
  ways, the check drops `pieces` under two entries, and the two-piece
  scenario in the harness has a one-piece control beside it.
- **Wrong weights per piece.** The quantities prompt gets each piece's
  typical finished weight from the profile by piece type, as it does today
  for one piece.
- **Rendering cost.** Two pieces double the images; the cap is four, and a
  third piece gets one view each. The checker still regenerates at most
  once per view.
- **Prompt size.** A two-piece specification adds a few hundred characters
  to each prompt; the digest cap is unchanged.
- **A rate missing for one piece only.** The question names the piece, the
  answer saves the rate once, and the replay prices both pieces.
