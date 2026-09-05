# Spec Gate — batched intake ask (retail)

Use once, early, price-free. Goal: the customer feels a jeweler wrote back
to them, not that they were handed a form. React to what they said, ask for
at most three things, and leave the door open to come in.

---

**CRITICAL: This must be a REPLY to the original customer inquiry thread.**

Do NOT compose a new email or subject. Send through
`scripts/workflow_safe.py send-spec-followup`; it uses `gmail_reply.py` with
the route from `gmail_route.py` to keep the conversation in the original
thread. The reply headers preserve the original subject.

**Email body:**

Hi {{first_name}},

{{One warm sentence that reacts to what they shared: the occasion, the person it is for, the stone they have, the idea they described.}} I would love to put this together for you.

To get you a real number rather than a guess, could you tell me {{the two or three details still missing, asked as plain questions inside one or two sentences, for example: what metal you are leaning toward, and roughly what size or carat weight the stone is?}} If you are not sure about any of it, say so and I will suggest what usually looks best.

And if it is easier to talk it through in person, you are welcome to come by the shop; just say when suits you and I will find a time.

Warmly,
{{shop_signature}}

---

## Rules

- **MUST use workflow_safe.py with route.json from gmail_route.py** — never compose a new email.
- Slot labels must come from a fresh `calendar_query.py` receipt validated by
  `appointment_options.py`; never type or infer a weekday/date label.
- Delete any cluster you already have answers for; confirm in a half-sentence instead of asking.
- Treat choices explicitly delegated to the jeweler as complete and default to
  shop sourcing unless the customer says otherwise.
- Never include a price, or a range, in this email.
- Never send more than one follow-up ask, and only for what is still missing.
- The subject line is automatic from the reply headers — do not invent one.
