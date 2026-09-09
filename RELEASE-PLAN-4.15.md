# Release plan 4.15: the estimate ledger

Design only. Nothing in this plan is built. Written 9 September 2026 after
the Michael Park thread (eleven messages, four desk emails, one estimate,
two wrong renders) and a review against the Kolo Skill Optimizer
(`/kolo-skill-optimizer:review`, audit 0 high, 11 low: markdown emphasis
in the tone templates, which never reach a customer).

## 0. What the thread showed (desk's pod, 4.14.7, 8 September 2026)

| Step | What happened | Root cause |
|---|---|---|
| First email with a photo | Six questions at once, then times after approval | Sequencing (fixed, unpublished). "Yellow, white, or rose?" asked although the photo shows white metal: the photo's reading is kept only in the claim's scratch folder and never on the record |
| Times email | "designing their perfect piece together when they come in" | A fact written for the writer in the third person, echoed verbatim |
| Second round of questions | Color and clarity asked again, for the emerald | One `stone_color` and one `stone_clarity` for every stone on the piece; D VS1 for the halo landed on the same keys the emerald needed |
| "Can I see what this would look like?" | Diamond drops rendered for emerald halo studs | The render prompt lists twenty fields and the image model ignored the stone; the customer's photo is used only as a logo, never as the piece to change; the archetype was guessed from "earrings" |

Every one of these is the same defect: the record is a snapshot the model
rewrites on every message, with no memory of where a fact came from.

## 1. Goals

1. A fact, once stated by the customer, is never asked again and never
   overwritten except by the customer's own later words.
2. A fact taken from a photo, chosen by the jeweler, or given by the owner
   is labelled as such and treated accordingly by the gate, the price card,
   the emails, and the renders.
3. The model reads one message at a time, with the ledger, and proposes
   facts with the words that support them. It never re-reads the thread.
4. Stones are rows: the center stone and the accent stones each carry
   type, origin, color, clarity, shape, and carat.
5. The renders start from the customer's reference when there is one and
   name the must-be-exact facts first; the checker asks about those facts.
6. Fewer questions to the customer, fewer tokens per call, no new cards.

## 2. Design

### 2.1 The ledger, in SQLite

The ledger is a database, not a field on the record: one file,
`estimate-desk/ledger.sqlite` (Python's own `sqlite3`, WAL mode, no
dependency on the pod). Two tables, `facts` and `asks`, keyed by estimate
id. Writes are transactions, so parallel claims need no file lock;
"what did we ask this customer" and "every estimate waiting on a rate"
are queries. The per-estimate JSON record keeps route, status, the
journals of sends and bookings, and the mirror Kolo's record store
expects; its `specification` is derived from the ledger (2.1, last
paragraph). Reset (rows by estimate, refusing an unknown schema), doctor,
readiness, and the manifest learn the file. One way: the desk writes,
everything else reads.

Each row of `facts`:

```json
{"field": "metal_karat", "piece": 0, "stone": null,
 "value": 18, "source": "customer", "gmail_message_id": "1a08…",
 "span": "18k white gold", "asked_in": null, "answered_in": "1a08…",
 "at": "2026-09-08T22:45:00Z"}
```

- `source` is one of `customer`, `photo`, `jeweler`, `owner`, `reading`.
  `reading` is a model inference from words with no span (rare; the gate
  treats it as unknown for price-moving fields).
- `stone` is `center`, `accent`, or a name (`halo`) for the stone rows;
  `null` for piece and metal rows.
- `asked_in` is the message id of the desk email that asked; `answered_in`
  the customer message that answered. A row with `asked_in` and no
  `answered_in` is an open ask.
- The ledger is append-only. A change is a new row; the newest row per
  (field, piece, stone) wins, subject to the rule in 2.3.

`specification` stays on the record as a derived view (newest winning row
per key, stones flattened the way the pricing sheet expects) so pricing,
cards, and every existing test keep working through the migration.

### 2.2 The reading contract (replaces `triage_and_extract` for continuing messages)

Input: the handled message's own words (quoted text stripped), the
ledger as a compact table, the photo readings for this message, and the
field definitions. Not the thread.

Output, validated in code: `{"facts": [{"field", "piece", "stone",
"value", "span"}], "leaves_to_jeweler": ["field"…], "conflicts": [{"field",
"old", "new", "span"}], "asks_for": ["estimate"|"meeting"|"rendering"…]}`.

Code accepts a fact only when its `span` occurs in the message's own
words. A fact with no span is recorded as `reading` and never moves a
price-bearing field on its own. A conflict with a `customer` row becomes
a customer row (their later words win) and is noted in the card's
assumptions. This is the optimizer's "smallest useful contract": proposal
with spans, code decides.

The first message on a thread keeps triage as it is (kind of message),
then runs the same reading contract.

### 2.3 Precedence and protection

Newest customer row wins. A `photo` row fills only a field with no
customer row and is replaced by any later customer row. A `jeweler` row is
written by code when the customer leaves a field to the jeweler ("I don't
know", "whatever is best") or when policy says the field is the jeweler's
(technical fields; colored-stone grades if the owner so decides). An
`owner` row (a rate, a price, a decision) outranks everything and is
never asked.

### 2.4 The gate on the ledger

Missing = a required (field, piece, stone) with no winning row. Asked =
a row with `asked_in` and no `answered_in`. "Never ask twice" is a lookup.
The stall question to the owner survives only for an ask the customer
replied to without answering and without leaving it to the jeweler.

Required fields are per stone: the center stone's type, origin, shape,
carat; the accent stones' type and origin; color and clarity per policy.
Technical fields (dimensions except a length the customer can name,
counts, weights) are never required.

### 2.5 The photo on the record

Photo readings become `photo` rows at intake (piece type, metal color,
stone shape, setting, design words), so they survive the claim's scratch
folder and later messages see them. The gate fills from them; the
follow-up says what was taken from the photo.

### 2.6 Renders from the ledger

- Must-be-exact, in this order, from the ledger: piece type, stone
  color/type per stone, metal color, setting. They open the prompt.
- When the customer sent an example piece, the render is an edit of that
  image ("the same earrings; center stones round green emeralds; white
  gold; halo of small white diamonds"), the archetype is the reference's,
  and the planner does not guess.
- The checker's questions are generated from the must-be-exact rows
  ("Are the center stones green?", "Is the metal white?", "Are these
  studs, not drops?"). A failed check regenerates once, then the owner
  sees the failure named on the card.

### 2.7 Emails from the ledger

Facts handed to the writer are written to the customer in the second
person, and the greeting is the customer's name or "Hello". A deterministic
check rejects a draft that greets the shop's or owner's name or speaks of
the customer as "they"; the fixed text goes instead. (The owner has said
not to prioritise the greeting; it is one line in the check, so it rides
along.)

### 2.8 An optional spreadsheet mirror (Google Sheets first, OneDrive/Office 365 later)

The mirror is optional and set up once: setup asks whether the owner
wants a spreadsheet mirror; if yes, the desk creates a spreadsheet named
"Jewelry Estimate Desk" in the shop's Google account (POST
`/v4/spreadsheets` through the gateway, tabs "Estimates" and "Facts" with
header rows) and writes its id and URL to the profile (`mirror.kind`,
`mirror.id`, `mirror.url`); the owner may instead paste the URL of a sheet
they already have, and the desk verifies it can read and write it before
accepting. Readiness checks the sheet is reachable. No Drive listing is
needed: a URL is enough, and the Drive route through the gateway is
reported, not verified. The mirror is built as one small interface
(`push_rows(tab, rows)`) with a Google Sheets adapter first and an
Excel-on-OneDrive adapter later, so the desk never depends on either.
The owner wants the ledger visible in a sheet. The desk already reaches
Google through Kolo's gateway token, proven for Gmail and Calendar;
the probe in 7.4 ran on the desk's pod on 9 September 2026: read 200,
addSheet 200, append 200 through `gateway.maton.ai/google-sheets/v4`, so
the same token serves the mirror. A small module pushes changed
rows after each ledger write, best-effort and journaled, to a "Facts" tab
(one row per fact) and an "Estimates" tab (one row per estimate:
customer, piece, status, missing, price, next step). If the scope is
missing, the alternative is a service account whose key lives on the pod
and whose address the sheet is shared with; a second credential, taken
only if the gateway says no. Rules: the sheet is a mirror and never the
source of truth (nothing reads back; a change is a question or a card,
so the audit trail holds); it lives in the owner's account and is shared
with nobody by default; a Google failure never holds up an inquiry.

#### What the sheet looks like

Built for the counter: a customer walks in, staff open the sheet, and the
estimate is on the first tab under the customer's name.

- **"Customers" (first tab).** One row per customer, newest activity at
  the top: name, email, phone when known, what they are having made (the
  piece in words), status in plain words ("waiting on details", "estimate
  sent $5,738", "meeting Thu 10 Sep 9:00 AM", "booked"), next meeting,
  last contact, what is still open, a link to the Gmail thread, and a link
  to their facts. Header row frozen and bold, filter turned on, banded
  rows, status coloured (green booked or sent, amber waiting, grey
  closed), columns sized to read without scrolling.
- **"This week".** The next seven days of meetings with the same row
  beside each, so the day's visitors are one glance.
- **"Facts".** One row per fact in readable words ("Metal karat", "18K",
  "customer wrote: 18k white gold", "8 Sep 10:45 PM"), grouped by
  customer, with the source in words: what the customer said, what the
  photo showed, the jeweler's choice, the owner's decision. Filter on the
  customer name shows one estimate's whole story.
- Formatting is applied once at creation and re-applied by readiness when
  it drifts (`batchUpdate`: frozen rows, bold header, banding, widths,
  conditional colours). Nothing in the sheet is edited by hand; the desk
  rewrites rows from the ledger, so a manual change is overwritten on the
  next push and never read back.

## 3. Consumers

| Consumer | Change |
|---|---|
| `spec_gate` | reads the ledger's winning rows; per-stone requirements; technical fields never required |
| `cost_components` | stone lines per stone row (center, accents); assumptions carry the row's source |
| `kolo_safe` cards | assumptions say "from the photo", "jeweler's choice", "customer" |
| `judge` | reading contract with spans; drafting facts in the second person; photo clause folds into the ledger |
| `rendering` | must-be-exact from the ledger; reference image as the edit base; checker questions from rows |
| `estimate_record` | derived specification from the ledger, migration from snapshot records on first read |
| `ledger` (new) | the SQLite store: schema, transactions, queries, per-estimate delete for the reset |
| `sheet_mirror` (new) | best-effort push of changed rows to the owner's Google Sheet, journaled |
| `customer_state_reset`, `readiness`, `manifest` | know the ledger file; the reset deletes rows by estimate and refuses an unknown schema |
| `reading_check` | becomes the span validator (its size and carat rules stay) |
| `doctor` | lists ledger inconsistencies (two winning rows, an ask with no email) |
| tests | golden path unchanged in outcomes; new ledger unit tests; Michael's thread as a fixture end to end |

## 4. Patterns pushed back on

From the optimizer's `patterns.md`, with what the live desk taught:

- **§2 and §9 let the model see the whole thread.** Wrong for extraction:
  every re-read paraphrased a meeting request, flipped "studs" to
  "earrings", and dropped the photo. The optimizer's own
  `intelligent-llm-use.md` already says "keep unrelated history out of a
  dedicated extraction call"; the desk did not follow it. Drafting an
  email may still see the thread; extraction may not.
- **§13 quiet chat, silent manual review.** Superseded by
  `error-recovery.md` and confirmed live: the bracelet inquiry sat silent.
  Every customer message ends in a customer email, a card, or a question.
- **§14 "install by marketplace version".** A version line is not a
  proof. The registry carried 4.14.2 code as 4.14.4 and 4.14.5 because
  "publish N" edited the line. Publish by commit; verify the manifest in
  the publishing folder and on the pod.
- **§5 "desk-answer CODE".** The owner replies "skip" from a phone with
  three questions open. The desk must resolve by the words, and refuse
  with the codes listed only when they fit several (built, unpublished).
- **§9 "check deterministically" was too narrow.** The checks covered
  figures and times, not voice: "Hi Tony" and "their perfect piece" went
  out. Add greeting and person to the check.
- **Not in the repo yet, learned this week:** never gate a decision on the
  equality of two model outputs (the ballpark re-offer); the customer's
  own words decide meetings, estimates, reschedules, picks, and "I don't
  know"; a size is asked only where a customer can name one, and no
  technical question ever goes out; a reference photo is the base to edit,
  not a logo; nothing reaches the customer before the approval when a
  card is involved.

## 5. Order of work

1. **Ledger in SQLite and the derived view** (ledger, estimate_record,
   doctor, reset, readiness, manifest, migration; tests). Live check:
   readiness READY, an old record reads and prices unchanged, the reset
   empties the ledger.
2. **Reading contract with spans** (judge, pipeline, reading_check;
   fixtures for every live email of 8 September). Live check: Michael's
   first email yields the right rows and asks only karat and origin.
3. **Gate, asks, jeweler's choice, per-stone rows** (spec_gate,
   cost_components, cards). Live check: the second round of questions does
   not happen; the price card shows the emerald and the halo separately.
4. **Photo rows and renders from the ledger** (pipeline, rendering,
   checker). Live check: green emerald halo studs rendered from Michael's
   photo, checker lines answered.
5. **Email voice check** (customer_mail, judge, content guard). Live
   check: no "Hi Tony", no "they".
6. **Sheet mirror** (sheet_mirror; after the scope probe in 7.4). Live
   check: a new fact appears in the sheet within a tick; a Google outage
   changes nothing for the customer.

Each step is one PR with tests and one live check the same day. Steps 1
and 2 change no customer-visible behaviour on their own.

## 6. Expected result

Michael's thread, replayed: one card after the first email (times and
four questions), one email after approval, one price card after his
answers, one render card with green emerald halo studs. Five model calls
fewer than today per such thread, roughly a third fewer tokens per call,
and no question the customer cannot answer.

## 7. Decisions needed from the owner

1. Colored-stone grades: jeweler's choice unless stated? (2.3)
2. Diamond color and clarity: ask as a preference, or jeweler's choice with
   the assumption on the card? (2.4)
3. Whether a photo alone may set metal color without asking. (2.5)
4. The Sheets probes (2.8) all passed on the desk's pod on 9 September
   2026: read, addSheet, append, and create (POST `/v4/spreadsheets` with
   two tabs returned the id and URL). No platform question remains for the
   mirror.
