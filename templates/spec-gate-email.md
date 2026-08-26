# Spec Gate — batched intake ask (retail)

Use once, early, price-free. Sendable at Stage 2+. Goal: clear the whole
checklist in ONE friendly email without making the customer feel quizzed.

---

**CRITICAL: This must be a REPLY to the original customer inquiry thread.**

Do NOT compose a new email or subject. Send through
`scripts/workflow_safe.py send-spec-followup`; it uses `gmail_reply.py` with
the route from `gmail_route.py` to keep the conversation in the original
thread. The reply headers preserve the original subject.

**Email body:**

Hi {{first_name}},

Thanks for reaching out — {{one_sentence_confirming_your_read_of_their_request_or_reference_photo}}. I'd love to get you a number quickly; a few quick things and I can put a real estimate together.

**The stone(s)**
- Lab-grown or natural? (This one moves the price the most.)
- Shape and approximate carat weight you have in mind — round, oval, emerald?
- Any preference on color and clarity? If you're not sure, no problem at all — just tell me the look you're after and your budget range and I'll recommend a grade that gets you there.

**The metal**
- Yellow, white, or rose?
- Most of our clients go 14K or 18K {{or platinum}} — any preference, or would you like me to recommend?

**The fit**
- Do you know the ring size? If not, I can walk you through a couple of easy ways to get it{{ without spoiling the surprise}}.
- {{length/dimensions if chain, bracelet, or pendant}}
- Any engraving or particular finish?

**The timing**
- Is there a date you're working toward?
- And a budget range you'd like me to design within? It's genuinely the most useful thing you can tell me — it lets me put your money where you'll see it.

If it's easier, we can cover all of this in about thirty minutes in person and I'll have your estimate that much faster. I have **{{slot_1}}** or **{{slot_2}}** {{timezone}} open — either work?

Warmly,
{{shop_signature}}

---

## Rules

- **MUST use workflow_safe.py with route.json from gmail_route.py** — never compose a new email.
- Slot labels must come from a fresh `calendar_query.py` receipt validated by
  `appointment_options.py`; never type or infer a weekday/date label.
- Delete any cluster you already have answers for; confirm in a half-sentence instead of asking.
- Never include a price, or a range, in this email.
- Never send more than one follow-up ask, and only for what is still missing.
- The subject line is automatic from the reply headers — do not invent one.
