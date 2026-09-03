# Kolo Skill Playbook

What we have learned about building skills that run well on Kolo. A living
document: add to it whenever a probe, an incident, or a doc teaches us
something. Every claim carries a status:

- **verified** — seen on a pod, with the date.
- **reported** — a Kolo instance or a document said so; not yet seen by us.
- **open** — a question we still need to answer.

Started 3 September 2026 from the Jewelry Estimate Desk build. Companion
documents: `ARCHITECTURE.md` (this skill's design and platform-facts table),
`WORKFLOW.md` (the business rules), and Tony's mirror of the Kolo portal docs
at https://github.com/tonycaligroup/kolo-product-docs (UI only).

---

## 1. How a skill runs on a pod

- Kolo runs each workspace on a pod built on OpenClaw. The agent workspace is
  `/home/node/.openclaw/workspace-main`; installed skills live under
  `<workspace>/skills/<skill-name>`; session logs and trajectories under
  `~/.openclaw/agents/main/sessions/`. **verified 2 Sep 2026**
- A skill installed by hand as a git checkout can be pinned to a commit
  (`git fetch && git checkout --detach <sha>`) and verified with `sha256sum` of
  the files that matter. Run the skill's own test suite on the installed copy
  before trusting it. **verified**
- Marketplace skills are updated at boot (`openclaw skills update <name>`) and
  updates force-overwrite local edits. Never hand-edit a marketplace skill's
  directory. **reported (kolo-product-docs SKILL.md)**
- Environment available to scripts on the pod: `MATON_API_KEY` (Gmail and
  Calendar gateway, **verified**), `LITELLM_API_KEY` and `LITELLM_BASE_URL`
  (model access, **reported 3 Sep**), `KOLO_PORTAL_URL` and `KOLO_LANDER_URL`
  (from `~/.koloclaw-env`, **reported**). Command-kind cron jobs run as
  `sh -lc` inside the gateway process and inherit that environment.
  **verified for the Maton key; reported for LiteLLM**
- The pod's clock is UTC. Use the owner's IANA timezone from the shop profile
  for anything customer-facing. **verified**
- `SKILL.md` has a practical size cap. Ours is tested at under 65,000 bytes;
  above that the agent's bootstrap gets slow and the skill prompt budget can
  truncate it. Move operational detail to `references/` and keep SKILL.md for
  rules and the exact commands the main session must run. **verified by our
  own tests; the platform's exact budget is open**

## 2. The two ways to run work

### Agent turns

- An agent turn is a model driving tools (`exec`, `read`, `write`,
  `image_generate`, ...) in a session. It carries the session's bootstrap
  context (workspace files, skill index) plus the prompt on every model call.
  Cost and latency scale with the number of tool calls, not with the size of
  the task. **verified: 10-12 calls took 3-5 minutes on qwen-3-7-plus and
  timed out at 15 minutes on glm-5-3**
- One-shot isolated jobs are the right way to give a task its own clock:
  `openclaw cron create --at +5s --delete-after-run --session isolated
  --message <prompt> --model <id> --thinking off --tools exec,read,write
  --timeout-seconds 900 --light-context --no-deliver --json`. `--exact` is
  only valid with `--cron`. **verified**
- A timed-out one-shot job is re-run once by OpenClaw about 30 seconds later,
  in a new session recorded in the same trajectory file. Design the job to be
  idempotent. **verified 3 Sep**
- Thinking: some models silently downgrade an implicit "high" level; models
  that honor it (Qwen, Claude, GPT) get slower with it on. Always pass
  `--thinking` explicitly. **verified**
- Delivery: `--no-deliver` keeps a worker's narration off the owner's channel.
  Isolated jobs that should announce need an explicit `--to kolo:<chat-id>`;
  the default delivery target fails silently. **verified (ours); reported
  (kfinnyfin/kolo-skills routines.sh)**

### Command jobs

- `openclaw cron create --cron '<expr>' --tz <zone> --name <n> --command
  "<shell>" --command-cwd <dir> --timeout-seconds <n> --announce --channel
  kolo --to kolo:<chat-id>` runs a script with no model at all. Stdout is the
  announcement; printing only `NO_REPLY` is silent. `cron edit --command`
  converts an existing agent-turn job to a command job in place, keeping its
  id. **verified 2 Sep**
- Measured tick cost for our watcher: 2 to 11 seconds, versus 6 to 15 minutes
  for the model-driven version it replaced. **verified**
- `cron list --json` is paginated and wraps results in `{jobs: [...]}`; use
  `cron get <id>` for one job. `kolo routine-list` shows only agent routines;
  command jobs appear in the portal's Routines page but not in that CLI.
  **verified**

### Lanes and concurrency

- Concurrency is per lane, not per agent. Chat turns share the `main` lane,
  capped by `agents.defaults.maxConcurrent` (2 on our pod). Isolated cron
  agent turns hold a `cron` slot and run in `cron-nested`. Unconfigured lanes
  cap at 1. Sub-agents have their own lane (8). **reported (OpenClaw docs);
  the sub-agent lane running concurrently with its parent is verified**
- Completions made by a script (`openclaw infer model run`) are direct
  provider calls and take no agent slot. This is the main reason to move
  judgment out of agent turns. **reported 3 Sep; consistent with the live
  run: two claims finished inside their ticks with the owner's chat idle**
- A watcher tick that judges inline finishes an intake claim in one tick
  (follow-up sent at 15:50, price brief filed at 16:02 on the reply) with no
  worker job. One tick reported a timeout at the 300 s clock during the
  pricing tick; the tick now stops taking new claims after 170 s. **verified
  3 Sep 2026**
- Symptom of lane starvation: commands typed into an owner thread sit at
  "Working" for minutes while background agent jobs run. **verified**

## 3. Model access from scripts

- `openclaw infer model run --model <provider/model> --thinking off --json
  --prompt <text>` is the supported stateless completion: no session, no
  tools, no agent turn, usable from a cron command job. Envelope:
  `{"ok": true, "capability": "model.run", "provider": ..., "model": ...,
  "attempts": [], "outputs": [{"text": "...", "mediaUrl": null}]}`. Provider
  failure is `ok: false` and a non-zero exit. **verified 3 Sep 2026 on our
  pod: the envelope matched and a JSON-only prompt came back as asked; the
  command job's shell had both LiteLLM variables**
- Not available on that command: system prompt, max tokens, temperature,
  JSON-only mode. Put the contract in the prompt, parse strictly, validate the
  shape, retry once quoting the rejection. Our `judge.py` does this.
  **reported; design verified by tests**
- Stay on the CLI rather than calling the LiteLLM base URL directly: the CLI
  resolves aliases, auth profiles, and the model catalog, and its envelope is
  stable. The subprocess overhead is about 200 ms. **reported**
- Models available (`openclaw models list`): `litellm-fireworks/qwen-3-7-plus`
  (pod default, alias `qwen`), `litellm-fireworks/glm-5-3-flash`, glm-5-1/2/3,
  kimi, deepseek, `litellm/claude-sonnet-5`, `claude-opus-5`,
  `claude-haiku-4-5`, `claude-fable-5`, gpt-5.x, gemini 3.1,
  `litellm/kolo-best-available`. **verified 2 Sep**
- Cheaper models for short JSON extraction and classification, reported at 5
  to 10 times less per token than qwen-3-7-plus: `glm-5-3-flash`,
  `litellm/claude-haiku-4-5`, `litellm-openai/gemini-3.1-flash-lite-preview`.
  Rate limits and per-token cost are the same whether a model is called from
  a script or an agent turn. **reported**
- Images: `openclaw infer image generate --prompt <text> --model <id> --json`
  writes a PNG and returns `outputs[].path` (default model gpt-image-2 on our
  pod, **verified 2 Sep**); `infer image edit --file <png> --prompt <text>`
  exists **reported**.

## 4. Talking to the owner

### Approvals (briefs)

- `kolo request-approval --agent-id main --action <title> --reasoning <text>
  --risk-level low|medium|high --details <flat JSON> --execution-payload
  <JSON> --session-key <key>` files a brief in the owner's Approval Required
  queue. Details render as labeled rows; nested objects render as
  `[object Object]`, so keep them flat strings. **verified**
- The card shows the action title, the details rows (order not controllable),
  the reasoning, and three buttons: Reject (reason chips plus notes), Edit
  Intent, Approve. **verified on the card; buttons per portal docs**
- An approved decision is injected into the session named by the session key
  as a user message: "**Strategic Brief #NN APPROVED — Execute Now** ...
  **Brief ID:** <uuid> ... **Execution Payload:** ```json {...}``` ... report
  the result using `kolo update-brief`". The skill's SKILL.md must tell that
  session exactly one command to run per payload kind. **verified**
- Edit Intent replaces the execution payload with the owner's edited JSON and
  the brief comes back as a new pending brief; the session receives the
  revised payload. Validate a revised payload deterministically; never
  re-derive from it. **reported 3 Sep**
- `kolo update-brief --brief-id <uuid> --status executed|failed|needs_delegation
  --execution-result <JSON>` closes the loop. A brief filed by mistake cannot
  be cancelled from the CLI; the owner rejects it. **verified**
- Per-user Approval Rules in the portal include Auto-Expire (never, 4, 12, 24,
  48 hours) and Auto-Approve Low Risk Actions with a spending limit. One Kolo
  instance says no auto-approval exists; the code-verified portal docs say
  the setting does. Until settled: file anything that sends to a customer or
  closes a review at `medium`, never `low`. **open**
- Approving a brief whose record has since moved on can trigger a raw send
  from the main session. Executors must re-verify the record state before
  acting (our brief #83 incident, 2 Sep). **verified**

### Messages and questions

- `kolo notify-owner -m <text> [--file <png>]` posts to the owner's main Kolo
  chat (or the setup channel). There is no delivery receipt: write `pending`
  before the call, `sent` after acceptance, `uncertain` on any failure after
  invocation, and never auto-retry `uncertain`. **verified**
- There is no reply binding or hook for an owner's answer. The answer arrives
  as a normal chat message in the main session; put a short unique code in
  the question so SKILL.md can tell the session which command to run with the
  owner's words verbatim. **verified 3 Sep with a rate question**
- The main session will freelance if the instruction leaves room: it asked
  "what is 700 for", ran pricing tools itself, and hand-edited a record
  before running the one command it was told to. Give it hard rules (never
  write skill state, never price, one command per decision) and make the
  command refuse invalid state. **verified**
- The owner channel may be SMS. Only finalized messages and questions the
  desk cannot proceed without belong there; progress narration trains the
  owner to ignore it. **decided with the owner**

### Records and audit

- `kolo record-upsert --type skill.<name> --external-id <id> --payload <file>
  --status <s>` mirrors a record for the owner; keep the authoritative copy
  in the skill's own private files and mirror second. `kolo log-action`
  writes audit events with an idempotency key. Kolo records have no
  compare-and-swap. **verified**

## 5. Integrations

- Gmail and Calendar go through the Maton gateway
  (`gateway.maton.ai/google-mail/gmail/v1/`,
  `.../google-calendar/calendar/v3/`) with `MATON_API_KEY`. Check them with
  `kolo integration-routing`. **verified**
- Gmail: fetch with `format=full` (`format=metadata` returned no headers
  through the gateway); raw URL calls outside the routed path 404. Send
  replies in the customer's thread with `threadId`, `In-Reply-To`, and
  `References`; a new subject is a new thread and a broken experience.
  **verified**
- There are no Gmail webhooks or push notifications on Kolo today; polling
  on a schedule is the only trigger. **verified by Tony, 3 Sep 2026**
- Calendar: insert, read, and delete an event with an attendee works through
  the gateway (Stage 0 test 6). **verified**
- Per-account Gmail message ids differ between mailboxes; identify customers
  by normalized sender address, never display name. **verified**

## 6. Design rules from the incidents

1. **The model decides, the code writes.** Money, sends, identity, the rate
   card, and record status are deterministic commands with validation. The
   model returns data into a schema. **verified across every incident**
2. **Questions for facts, briefs for actions.** A missing fact is a
   plain-English question in the owner's channel; a consequential action is
   an approval brief. A review list the owner has to go and look at is
   neither. **decided 3 Sep; missing-rate question verified live**
3. **Never let the main session touch state.** It runs exactly one command
   per delivered decision or answer. **verified the hard way**
4. **Workers get a branch prompt, never the runbook.** A 65 KB SKILL.md read
   at the start of every worker turn was the largest single cost. **verified**
5. **Bundle every deterministic step behind the judgment it follows.** Each
   tool call is a full model round trip; one command that records the review
   and runs everything after it is worth more than any prompt edit.
   **verified**
6. **Bind live configuration with a hash and reconfigure in two phases.**
   Prepare against the current binding, activate against the target, and
   never recreate a job (a new id breaks bindings). **verified**
7. **Journal every external action** with pending, sent, and uncertain
   states, and never auto-retry uncertain. **verified**
8. **A review that says "ask the customer" is not done until the send is
   recorded.** A worker dying between the two strands the customer silently.
   **verified 3 Sep; guarded**
9. **Prefer command jobs to agent turns for anything on a schedule.** Model
   turns are for judgment only. **verified**

## 7. Operating a live skill

- Drive Kolo from a browser for installs and diagnostics: one shell command
  per message, "paste the raw output only", and never a newline in the
  message (typed newlines send fragments; up to five queue and the stop
  button drops them). Wrap long output with `fold -w 110` or
  `cut -c1-100` so it fits the code box. **verified**
- Kolo threads with hundreds of tool calls stop answering after a restart
  ("Kolo couldn't finish this response"). Start a fresh thread for
  diagnostics and keep the working thread short. **verified 3 Sep**
- Portal notices: "Kolo took too long to start" is a cold start after idle;
  "Your assistant restarted before it could finish replying" offers a
  Resend link; a stuck thread can be reset with "Start a fresh session" or
  `/new`. **reported (portal docs); the restart notice verified**
- Read a job's trajectory at
  `~/.openclaw/agents/main/sessions/<run-id>.trajectory.jsonl`: `toolCall`
  entries carry the command, `model.completed` the final text,
  `session.ended` the status and `aborted` flag. Grep those rather than the
  whole file; the main session's transcript is huge. **verified**
- Reading a page in the browser can be blocked when the text resembles
  secrets; strip `= & ? ;` before returning it. **verified**
- Repair recipes we have used: return a hand-edited record to a valid status
  and remove invented fields; resolve a stale review; move a claim from
  `manual_review` to `awaiting_owner` and reopen it; backfill an owner
  question with headers fetched from Gmail. All are one Python snippet run
  from the skill's own modules, never by editing JSON in an agent turn.
  **verified**

## 8. Open questions and next probes

- Session-key format for an SMS or Slack chat as a `kolo notify-owner`
  target (`kolo list-chats` shows chats; confirm the key shape before an
  owner picks SMS at setup).
- `openclaw infer image generate` from the tick produced two PNGs that the
  materializer accepted and that reached the customer's thread. **verified 3
  Sep 2026** (the send was ungated at the time; the gate is built since)
- A `kolo notify-owner --file <png>` preview plus an approval card is the
  pattern for "look at this before it goes out"; a card cannot carry an image
  itself. **design; preview verified earlier with PNG inline**
- `kolo notify-owner --session-key <activation session key>` posts into the
  thread that activated the skill (the response names the chat id), which is
  the same thread where `request-approval` cards appear. So the activation
  binding's key is the right default owner channel. **verified 3 Sep 2026**
- `kolo request-approval --help` and `kolo notify-owner --help` print nothing
  (2m46s, empty output), so flags have to be learned from docs or trial; no
  attachment flag is known for approval cards. **observed 3 Sep 2026**
- Does Auto-Approve Low Risk apply to skill briefs? Test with a throwaway
  low-risk brief once the setting is understood.
- Dedicated worker agent (`openclaw agents add`, `agents.entries.<id>` with
  `skills: []`, cron `--agent <id>`): does it shrink the bootstrap context,
  and can it be created from the pod or only through Kolo's own flow?
- Cron `retry.maxAttempts` for isolated jobs versus our lease-based retry.
- Which cheap model holds up on extraction and classification in practice.
- Whether `infer image generate` from the tick can retire the rendering
  worker.
- Exact SKILL.md and skill-prompt budgets on the platform.
