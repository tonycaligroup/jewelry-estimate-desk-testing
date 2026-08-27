# Customer Email Templates

Fill the brackets. Never send below the trust stage that authorizes it.
Nothing here contains a price except the estimate template.

> **These are body templates only. Never draft or replace the subject.** Every
> email must be sent as a reply in the original inquiry thread through
> `workflow_safe.py`; the original subject is preserved by the reply headers.
> This also keeps surprise-sensitive inquiries out of a new lock-screen thread.

---

## 1. Missing-information request (Stage 2+, or owner-sent at Stage 1)

No prices. Not even a range. One email, batched.

```
Hello <Name>,

Thank you for reaching out -- I appreciate you thinking of us to create this.

To put together an accurate estimate, could you share:

  - <load-bearing item 1>
  - <load-bearing item 2>
  - <load-bearing item 3>

<One sentence confirming what you understood from their reference photo, so
they can correct you cheaply.>

If it's easier to talk it through, I have <slot 1> or <slot 2> <TIMEZONE>
open -- happy to do a quick call, or have you visit the showroom.

<signature>
```

### What's load-bearing, by piece

| Piece | Moves the price -- ask | Ask, but don't wait |
|---|---|---|
| Engagement ring | Stone origin (lab/natural), carat, metal + karat, finger size, setting style | Color, clarity, cut, budget, band width |
| Wedding band | Metal + karat, finger size, width, plain vs set, budget | Finish, comfort fit, engraving |
| Necklace / pendant | Chain length, metal + karat, gauge/style, stone spec, budget | Clasp, bail size, extender |
| Bracelet | Length, metal + karat, style, stone spec, budget | Clasp, safety catch, taper |
| Earrings | Metal + karat, stone spec, budget | Back type, drop length |
| Remount / redesign | What exists, what to keep, metal, budget | Photos of the original, sentiment constraints |
| Anything with stones | **Lab or natural and size** | Color, clarity, cut, certificate, origin preference |

Cap it at four bullets. A wall of questions gets no reply. **Five load-bearing
fields but only four bullets?** Combine, don't drop: stone origin + carat ride
in one bullet ("lab-grown or natural, and roughly what size"), metal + karat in
another. Timing and event-date questions move into prose, not the bullet list.
Budget may keep its own bullet when useful, but the customer does not have to
provide one before receiving an estimate. Event date is also useful for
feasibility but is not an estimate prerequisite.

**Delegated choices:** if the customer asks the jeweler to choose what looks
good, treat color, clarity, cut, and similar quality choices as delegated—not
missing. Use the shop's default tier as a jeweler-facing assumption. Default to
shop sourcing unless the customer says they are supplying the material.

**Budget:** optionally ask for a range as design guidance, never as a
qualification screen or prerequisite to an estimate. "A rough range can help
me steer the design toward something you'll actually love."

---

## 2. Estimate (only after owner approval -- always)

The number here is the **owner-approved** figure, not the computed one.

```
Hello <Name>,

Thank you for your patience. Here's the estimate for the piece we discussed.

<One paragraph, general. Enough that they recognize their piece -- not enough
to hand a competitor a build sheet. Use only the customer-safe specification.
No internal weights, jeweler cost assumptions, or component costs.>

Estimate: $<approved price>

Just so you know how to read this: we estimate on the high end on purpose. This figure is pending final design approval, and once your design is finalized the final price is often a little lower — if it comes in under, we pass that straight along to you. Nothing is locked in until you've seen and approved the final design.

Estimated lead time: <x> business days
<deposit terms> · <tax handling>
Valid through <date>.

<If rendering attached: "Attached is a rendering for your review -- it's an
illustration of the design direction; the final piece is built to the written
specification above.">

Happy to walk through any part of this. I have <slot 1> or <slot 2>
<TIMEZONE> open if you'd like to see it in person.

<signature>
```

### Post-estimate visual rendering

```
Attached are visual illustrations of the design direction we discussed. The
written specification and the final design you approve control the finished
piece.

If you would like an adjustment to the look, reply here and tell me what you
would like changed.
```

**Wholesale, add:**

```
This is an estimate based on previous comparable jobs, to help you close the
sale. On custom work the price can move once the design is finalized, and the final
price is often lower than estimated -- we estimate high and pass any savings
along to you.
```

Never: jeweler cost assumptions, line items, per-gram, per-carat, component
costs, costing quantities, vendor or manufacturer names, markup, or margin.
This applies to retail and wholesale. **"Estimated lead time"** -- never a guaranteed
date, never a promise against a wedding, unless the owner said so in writing.

---

## 3. Appointment offer (Stage 3+ for autonomous send)

Always 2-3 named times. Always the timezone. Never "what works for you?"
Every label must be produced from a fresh provider-backed calendar receipt by
`appointment_options.py`; never calculate or type the weekday independently.

```
Hello <Name>,

I have <day, time> or <day, time> <TIMEZONE> open for a <meeting type> --
about <duration>, <at our showroom / by phone / by video>.

Either of those work? If neither does, tell me roughly when you're free and
I'll find something.

<signature>
```

---

## 4. Appointment confirmation (only after the calendar write succeeded)

```
Hello <Name>,

You're confirmed:

  <Day, Date> at <time> <TIMEZONE>
  <duration> · <showroom address / video link / "I'll call you at this number">

<If useful: "Bring <reference photos / the ring you're matching / the heirloom
piece / the stone certificate> if you have it.">

If you need to move it, just let me know.

<signature>
```

Never send this before the calendar confirmed the write. "I've requested it"
is not "you're confirmed."

---

## 5. Follow-up nudges -- day 3, then day 7, then stop

```
Hello <Name>,

Just circling back on your <piece> -- still happy to put this together
whenever you're ready.

<Restate the one thing you're waiting on, or offer a named time.>

<signature>
```

Day 7 is the last one. Mark dormant and stop. Two follow-ups is service;
four is harassment.

---

## 1a. "Roughly what would this run?" — the direct price question

The most common sentence in an inbound custom lead, and the most delicate.
It is **not** price pushback and **not** an escalation — nothing has been
quoted yet. Never answer with a market range, a ballpark, a "typically," or a
starting-at figure. That breaks rule 1.

Fold this into the info-request email rather than sending it alone:

```
On "roughly what would this run" -- I'd rather not throw a figure at you yet,
and here's the honest reason: on a piece like this one factor, whether the
centre stone is lab-grown or natural, can move the total several times over.
A number now would be one you'd have to un-learn. Answer the couple of
questions above and I'll come back with a real one.
```

Swap the named factor for whatever actually dominates that piece: stone origin
on anything with a centre stone, finished weight on plain metal, stone count on
pavé. Naming a real reason is what keeps this from reading as a dodge.

---

## 6. Things with no template, on purpose

These never get an agent-drafted reply. They go to the owner:

- Price pushback on a number you already sent, or a discount request
- "What's my old ring worth?"
- A complaint, a dispute, or anything with a lawyer in it
- "Is this ethically sourced?" / "Is my other piece real?"
- "Can you have it by Saturday?" when the bench hasn't confirmed
- Anything asking for a payment link or card details
