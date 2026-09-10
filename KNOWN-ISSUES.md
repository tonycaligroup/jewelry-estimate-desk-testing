# Known issues, dealt with

Every defect seen live or in rehearsal on the Jewelry Estimate Desk, the rule that replaced it, and the version that carries it. One line each, newest first within a group. Groups follow the reviewer's taxonomy of 10 September 2026: understanding, decision, writing, state, execution, platform. The rule for each entry is also in WORKFLOW.md §12; the customer-phrase ones are fixtures in `scripts/words.py`.

Dates are 2026. "Live" means a tester's real thread; "rehearsal" means a scripted run on the pod.

## Understanding: what the customer meant

| Seen | Symptom | Rule now | Version |
|---|---|---|---|
| 9 Sep live | "Hey remember those sapphire earrings?" from a customer with the pair on file was asked four questions | A reference to something from before is a whole family (a past cue beside a piece word, never a photo, website, heirloom or another jeweler), and the model judges what no rule reads, in the reading and at intake | 4.15.15 |
| 9 Sep live | "Everything the same but in platinum" read back as "white platinum" | A new metal in their words drops the old metal's colour and karat | 4.15.15 |
| 9 Sep live | "The emerald earrings we talked about earlier, the same but with sapphires" was asked every question again | An earlier estimate to the same address is carried beneath the new words; the new words win | 4.15.7 |
| 9 Sep live | "An exact replica of the pendant you made for me, smaller" asked the customer for facts already on file | The piece on file is the base; nothing on it is asked; a new size drops the old carat | 4.15.7 |
| 9 Sep live | The look-alike rendering lost the diamond halo on file | A piece on file outranks the reading's guesses | 4.15.10 |
| 9 Sep live | "15mm x 12mm oval" was followed by a carat question | A stone sized in millimetres is sized; the carat is derived, never asked | 4.15.7 |
| 9 Sep live | "Maybe 2 to 3 ct" was treated as unknown | A carat range prices at its top and is shown as the assumption | 4.15.8 |
| 9 Sep live | A sapphire pair priced as diamonds; the halo's diamonds became the center stone | The halo's stones, a photo's reading and a note are never the center stone | 4.15.0 |
| 9 Sep live | "A pair of earrings" with no photo rendered as drops for a customer who meant studs | Earrings alone are studs, hoops or drops: settled from words or photo, otherwise asked | 4.15.0 |
| 9 Sep live | For a pair, "2.5 ct" priced one stone | A pair's carat is each or total; the words settle it or the desk asks | 4.15.0 |
| 9 Sep live | A gift-giver was greeted by the recipient's name ("Hi Verónica" to David) | The address line says who is writing; a draft greeting someone else is refused | 4.15.0 |
| 8 Sep live | Earrings were asked for a finger size | A piece is a ring only when "ring" or "band" is a whole word of its name | 4.14.x |
| 8 Sep live | An eternity band was asked for a center carat and cut | An eternity, channel-set, pavé or all-around band has no center stone whatever the reading says; ".2 ct" is two tenths | 4.14.1 |
| 7 Sep rehearsal | "Reuse the wedding band" was read as a customer's own stone | Reuse, reset, remount, my own count as a customer's stone only beside a stone word | 4.13.x |
| 8 Sep live | The desk's own rendering became the reference for the next render | A rendering's references come only from the customer's messages | 4.14.x |

## Decision: what should happen next

| Seen | Symptom | Rule now | Version |
|---|---|---|---|
| 9 Sep live | "Sorry, I meant to say Tuesday." after a booking went to the owner as a stalled question | With a meeting booked, a bare day with reschedule words moves it to that day at the booked time | 4.15.14 |
| 9 Sep live | "My mistake, I have the wrong day. Can we do Monday at 3pm?" after a booking got seven questions | With a meeting booked, a question naming a time is a reschedule whatever the words around it | 4.15.13 |
| 9 Sep live | "See you tomorrow!" after a booking got a questionnaire | A courtesy note after a booking sends nothing | 4.15.12 |
| 9 Sep live | The jeweler's carat loop: "Do you know the carat?" "No I don't." asked again | "No, I don't", "not really", "no clue", "haven't decided" leave the detail to the jeweler like "I don't know" | 4.15.6 |
| 9 Sep live | "Monday would be best" plus the details was priced with no meeting card | After times were offered, a bare day answers the offer; the meeting card is filed before the price | 4.15.6 |
| 9 Sep live | "Monday the 21st at 11am please" stated flat was not a meeting | A day with a clock time stated flat is a meeting sentence | 4.15.6 |
| 9 Sep live | "A call tomorrow at 3pm" came back from the reading as "tomorrow" and times were offered instead of booking | The customer's own scheduling sentences ride with the reading's quotes | 4.15.9 |
| 9 Sep live | A missing rate skipped the booking card | The meeting card is filed before the review, whatever the review does next | 4.15.6 |
| 9 Sep live | "Next week" offered the nearest days; "after 1pm any day next week" offered 9:00 AM | A stretch of days is where offers come from; a clock bound narrows them | 4.15.5 |
| 8 Sep live | A reschedule ("something came up… Friday at 4pm?") was answered with the questionnaire | A reschedule or a proposed day and time is a meeting request decided in code | 4.14.x |
| 8 Sep live | The desk offered times again after "before I come in, can I get a ballpark?" | On a reply, a meeting request stands only on the reply's own words | 4.14.x |
| 8 Sep live | A customer asking to come in and for a price got the questionnaire first | Meeting first; the offer card carries the questions; nothing reaches them before approval | 4.14.x |
| 8 Sep live | A ready-to-ship bracelet inquiry was skipped, then quoted like a custom piece | A ready-made inquiry is offered a visit; after two replies without a booking the owner takes it | 4.14.x |
| 8 Sep live | An out-of-scope reading went to a silent manual review | It asks the owner: "quote it" or "handle myself" | 4.14.x |
| 8 Sep live | Millimetre diameters, drop lengths and prong counts were asked of a customer | The desk asks only what a customer can answer; technical questions never go out | 4.14.7 |
| 9 Sep, the jeweler | The desk priced from chat details, rendered and sent in concierge mode; too ambitious | Concierge books the call and stops; the owner sends the estimate | 4.15.12 |
| 9 Sep, the owner | A bare number to a missing-rate question was saved to the rate card | "save N", "use N once", or a bare number that prices now and asks save-or-once | 4.15.12 |
| 9 Sep, the jeweler | A piece the shop made was carried silently or the owner was asked | The piece on file is said back, never its cost, and confirmed before pricing; nothing on file gets a warm welcome back and a reminder-or-photo ask | 4.15.12, 4.15.15 |
| 7 Sep rehearsal | A second piece re-asked facts the first piece had (the wife's ring) | Shared top-level facts reach every piece; the quoted piece is never re-priced | 4.13.x |
| 7 Sep rehearsal | The same band in rose gold was weighed again (brief #46) | A quoted piece in another colour takes the quoted numbers | 4.13.x |

## Writing: what the customer read

| Seen | Symptom | Rule now | Version |
|---|---|---|---|
| 9 Sep live | A visit was confirmed with "I'll call you at (310) 810-3004" from a number in the signature | A number never makes a visit a call; a visit's confirmation may not say the shop will call (guarded at draft, filing and booking) | 4.15.12 |
| 9 Sep live | The questions email invited a visit that was already booked | After a booking, questions say the rest is settled when you meet | 4.15.12 |
| 9 Sep live | The estimate email said "Thursday at 11am, which I have reserved" with nothing booked | A customer email never claims a meeting is booked unless one is | 4.15.4 |
| 9 Sep live | A confirmed visit and a rendering request in one email went out as two emails | Cards born from one email send one email; the first approved holds, the second carries both | 4.15.5 |
| 9 Sep live | The vision line said "confirm the halo" instead of the customer's vision | With a photo the desk confirms the vision the way a jeweler would ("Just so I have your vision right…") | 4.15.0 |
| 9 Sep live | Renderings were sent without a caveat | Renderings are "for guidance only"; the guard refuses a draft without it | 4.15.3 |
| 9 Sep live | An offer email dropped a question the card promised | The email falls back to the fixed text | 4.15.0 |
| 8 Sep live | An appointment before any estimate mentioned the estimate | An appointment before the estimate never mentions one | 4.14.x |
| 4 Sep | Customer emails read like a form | Emails are drafted in the jeweler's voice from facts; fixed text only as fallback | 4.4.0 |

## State: what the record held

| Seen | Symptom | Rule now | Version |
|---|---|---|---|
| 9 Sep live | Two cards for the same estimate: the sheet's cost block and the record disagreed after a draft | The cost sheet is the owner's workbench: drafts kept every tick, "ready" prices from the owner's numbers | 4.15.8 |
| 9 Sep live | Vendor emails listed as closed customers on the sheet | The mirror never lists mail triage closed as not an inquiry | 4.15.5 |
| 9 Sep live | A rejected price card answered with a fact was read as "handle myself"; the estimate went dormant | An owner's changed fact re-prices; `doctor.py --revive` brings a dormant estimate back | 4.15.0 |
| 8 Sep live | A change from a new thread re-asked both bands' sizes and karats (case 6) | The reading is handed the record's specification and merges the new words into it | 4.13.x |
| 8 Sep live | A reopened piece lost its quoted base (question 9CD6F2) | The quoted estimate is the base for a reopened piece | 4.13.x |
| 8 Sep live | A resumed claim tripped on its own record and stranded the inquiry | The resume self-block is excluded; a binding no card was filed against is replaced | 4.13.x |
| 8 Sep live | An owner's reply without a code went nowhere | A reply goes to the one open question its words fit, or the desk lists the codes | 4.14.x |
| 2 Sep incident | Approving a brief for a declined record sent a raw email and a hand-edited record | Executors validate the record's state against the approval binding; the main session never edits records | 4.1x |
| 10 Sep | A record key could be added without anyone knowing what wrote it | RECORD-SCHEMA.md lists every key; a test diffs it against the code | 4.15.11 |

## Execution: what actually happened

| Seen | Symptom | Rule now | Version |
|---|---|---|---|
| 9 to 10 Sep live | Nothing was written to the spreadsheet for a day; no Rates tab | The sheet read built a URL with a space in "Cost sheet"; Python's HTTP client refused it and the push behind it never ran. Range URLs are escaped, a failing pull never stops the push, and a test hands the URL to the real client | 4.15.14 |
| 9 Sep live | Approving two cards in one message dropped one | One APPROVE per message; executors are idempotent | (chat) |
| 9 Sep live | A phone-number reply crashed the cancellation branch | A local import shadowed a module name; removed | 4.15.9 |
| 8 Sep live | A rendering run killed while filing its card rendered again on resume | A killed run resumes to the card and never renders twice | 4.13.x |
| 7 Sep rehearsal | Renders overran the tick; a view killed three ticks running looped | Image and vision calls are cut off at the tick's own deadline; three kills become a question | 4.13.5 |
| 7 Sep rehearsal | "Still processing" reached the owner once per view | A claim handed to the next tick is silent in the run report | 4.13.x |
| 6 Sep | A flaky vision check killed a rendering | The check is retried, views checked one at a time, an uncheckable view is carded as such | 4.12.1 |
| 8 Sep live | A reply sat undiscovered through idle ticks | Discovery overlaps the watermark by two hours | 4.13.x |
| 2 Sep | Cron timeouts while queued behind active work | Reading and pricing bundled into one tick; the watcher's own clock | 4.13 |

## Platform and release

| Seen | Symptom | Rule now | Version |
|---|---|---|---|
| 8 Sep | A pod ran 4.14.2 scripts under a 4.14.5 SKILL.md for three releases | A manifest of script checksums; readiness fails naming the files when the folder does not match | 4.14.7 |
| 8 Sep | The publishing folder was stale; "publish N" re-labelled old code | Publishing is by commit: re-clone, checkout, manifest check, then publish | (process) |
| 10 Sep | The same version number cannot be published twice | Every publish bumps the version | (process) |
| 10 Sep | The publish thread's send button, not Return, sends | Publishing driven from the in-app browser on the owner's word | (process) |
| 9 Sep | Kolo's chat summarises probe output unreliably | Probes ask for raw output only | (process) |

## Open, not yet dealt with

- A direct customer question is not proven answered, deferred or turned into a clarification by any guard.
- A failed audit query reads as "no events" and the approvals watermark still advances a minute; a decision made during an outage can be missed until something else nudges the card.
- A Kolo card is matched to local work by the first 120 characters of its title; two cards for one estimate with the same opening could cross.
- A reschedule reports success on a deletion the calendar may not have performed.
- Failing-checker rendering views are sent on approval.
- The stalled-question card A84A36 on the engagement ring thread was left open on purpose.
