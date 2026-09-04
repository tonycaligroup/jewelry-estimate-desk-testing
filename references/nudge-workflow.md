# Nudge Workflow

Read this reference only after an estimate or specification request has been
sent and the shop has authorized follow-ups.

## Cadence

Schedule one follow-up for day 3 and one for day 7 in the shop's IANA timezone.
After day 7, mark the estimate dormant. Never create more than two automated
nudges.

## Before every nudge

1. Read the current estimate with `kolo record-get`.
2. Stop silently if it is approved, declined, dormant, or has a newer customer
   reply.
3. For Gmail, rebuild the route from the latest inbound customer message with
   `scripts/gmail_route.py`. Require its email-derived `identity_key`, recipient,
   and thread ID to match the stored route, then build the nudge with
   `scripts/gmail_reply.py`. Never select a customer by name or use the owner's
   `deliveryContext.to` for a customer message.
4. A nudge asks the customer something and carries no price, so it may be
   sent like a specification follow-up; anything with a price or a time
   waits for the owner's card.

## Cron requirements

- Use a one-shot `openclaw cron add --at` timestamp with an explicit UTC offset.
- Pin the configured Kolo model and set `--fallbacks ""`.
- Put only the opaque estimate ID in the cron message.
- The cron must read the record and the customer email template at execution
  time; do not embed customer names, addresses, or piece details in the cron.
- Use `<estimate-id>-nudge-3` or `<estimate-id>-nudge-7` as the audit
  idempotency key.

If record lookup fails, do not send. Notify the owner with the opaque estimate
ID and request manual review.
