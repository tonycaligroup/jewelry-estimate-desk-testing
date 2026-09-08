# Release plan 4.14: the model off the CLI, claims in parallel

Written 8 September 2026 after two days of probes on the desk's pod. The
desk's slowness and its blocking were never the models or the image
provider: they were the platform CLI in front of them, which runs one
command at a time (an SQLite lock), does not honour its own timeouts, and
adds ten seconds of start-up to every call. 4.13.11 took the image calls
off the CLI. This release takes the judgement calls off it and lets the
tick work on several claims at once. WORKFLOW.md is the source of truth;
no rule changes here, only how fast and how many.

## 0. What was measured (desk's pod, 8 September 2026)

| Call | Through the CLI | Direct to the proxy |
|---|---|---|
| One judgement (the intake prompt, Qwen 3.7 Plus, thinking off) | 13 s | 1.0 to 1.7 s, median 1.2 s; 24 at once in 1.7 s; 24 of 24 parse |
| One image (gpt-image-2, 1024x1024, default quality) | 40 to 351 s | 14 to 25 s; 72 at once in 45 s |
| Two commands at once | "database is locked", one fails at once | no limit seen |

The proxy's address and key sit in `LITELLM_BASE_URL` / `LITELLM_API_KEY`
in every process the agent spawns, the watcher's cron-run ticks included.
With thinking on, Qwen spends the whole token budget on reasoning and
returns an empty answer; `reasoning_effort: "none"` (or
`chat_template_kwargs: {"enable_thinking": false}`) gives the clean JSON
the desk needs at ~180 tokens a call.

Today's tick also takes at most two claims per tick (`DEFAULT_MAX_WORKERS`
= 2), one after another, so the desk's ceiling has been about 60
inquiries an hour whatever the call speed.

## 1. Goals

1. A judgement call costs about a second, not thirteen, and never waits
   behind another command.
2. A tick handles many claims at once: a burst of twenty inquiries drains
   in one tick; several renderings run together.
3. Nothing customer-facing changes: the same prompts, the same readings,
   the same cards, the same rules. The CLI stays as the fallback wherever
   the proxy is not reachable (the lab, the tests, a pod without the
   variables).
4. Every consumer walked: code, tests, harness, docs, readiness, doctor,
   reset, rehearsal.

## 2. Design

### 2.1 One transport for every model call

`judge.complete` is the only place the desk runs a model; every judgement
(reading, classifying, quantities, drafting, times, rendering plan) goes
through it. It gains the same shape `rendering.render` got in 4.13.11:

- `model_provider.available(mode)` (in `image_provider.py`, renamed
  `provider.py` with `image_*` and `chat` functions, or a sibling module;
  one credentials reader either way): the profile's `model.provider`
  (auto | direct | cli), then the environment.
- Direct: `POST {base}/v1/chat/completions` with `model` = the profile
  model's proxy id (`litellm-fireworks/qwen-3-7-plus` → `qwen-3-7-plus`,
  the part after the last `/`), one user message holding the prompt
  unchanged, `temperature` 0 for judgements and 0.3 for drafts (a
  `creative` flag on `complete`), `max_tokens` 1500 (drafts 2500),
  `reasoning_effort: "none"`, a timeout of 60 s. The answer is
  `choices[0].message.content`, then the existing `extract_json` and
  checkers. A non-JSON or refused answer is the same `JudgmentError` as
  today, transient on 5xx/timeout, deterministic otherwise.
- CLI: unchanged (`infer model run --thinking off --json`).
- `CALL_LOG` entries gain `transport: direct|cli`; the tick summary's
  timing block reports both counts. The key is read from the environment
  at call time and never logged.

### 2.2 Claims in parallel inside the tick

`inbox_watcher.tick` keeps its order (reconcile, approvals, rejections,
reminders, discovery, then claims) and changes only how claims run:

- `DEFAULT_MAX_WORKERS` becomes the number of claims a tick may take
  (default 16, profile `desk.claims_per_tick`), and a thread pool of
  `desk.parallel_claims` (default 8) runs `_attempt_inline` for them.
  Non-rendering retries are submitted first, then new claims, then
  renderings, so mail is still ahead of views in the queue; with the
  direct transport a rendering claim is ~40 s and no longer needs to go
  last, but the order costs nothing.
- The summary is written under one lock (`summary["inline"]` appends,
  counters); `mark_tick` records the set of claims in flight, not one.
- What is already safe per claim: the claim state file lock, the record
  lock, atomic `write_private`, the run lease (one tick at a time),
  per-claim work folders. What is shared and must stay safe: Gmail token
  load (read once per tick), the profile (read once per tick and passed
  down where it is re-read today), `rendering.PROVIDER_MODE`/`IMAGE_*`
  (set once per tick before the pool starts), `judge.CALL_LOG` (append
  only, fine).
- The inline budget (170 s) is checked before each submission; a claim
  submitted late still finishes because every call inside has its own
  timeout and the direct calls are seconds.
- The `kolo` CLI calls (cards, notices, previews, audit) remain
  sequential per claim and may overlap across claims; they go through
  `rendering.run_cli` (moved to a shared `cli.py`) so a "database is
  locked" costs a five-second retry, not a failure. Whether the `kolo`
  binary shares the OpenClaw lock is unknown; the retry covers either
  answer.
- The cron sweep of one-shot render jobs is removed (jobs retired in
  4.13.0); one fewer `openclaw cron list` per tick.

### 2.3 Settings

Profile `model` block: `provider` (auto | direct | cli), `temperature`
overrides for judgements and drafts (optional). Profile `desk` block:
`claims_per_tick` (1..64, default 16), `parallel_claims` (1..16, default
8). `validate_profile` knows them; the template documents them; readiness
prints `model transport: direct (proxy reachable)` or `cli`.

## 3. Consumers

| Consumer | Change |
|---|---|
| `judge.complete` | transport choice; `creative` flag; `transport` in `CALL_LOG` |
| `image_provider.py` → `provider.py` | `chat(prompt, model, timeout, temperature, max_tokens)`; `credentials` shared; `image_provider` kept as a thin alias for one release |
| `rendering.run_cli` → `cli.py` | shared by `rendering`, `kolo_safe`, `inbox_watcher` |
| `inbox_watcher.tick` | thread pool for claims; summary lock; `mark_tick` list; budget check per submission; sweep removed |
| `pipeline`, `workflow_safe` | read the profile once per tick where they re-read it per call today (a `profile` argument threaded through `_namespace`) |
| `validate_profile`, `templates/shop-profile.json` | `model.provider`, `model.temperature_*`, `desk.claims_per_tick`, `desk.parallel_claims` |
| `readiness` | the transport line; the inline-judgment check runs through `judge.complete` so it proves the transport in use |
| `doctor` | `tick_killed` unchanged; the tick mark lists claims |
| `customer_state_reset`, `rehearsal` | unchanged (nothing new on disk) |
| `tests/test_golden_path.py` World | `model_direct` fake beside the CLI fake: `provider.chat` patched to answer from the same prompt routing; a class of tests runs the whole desk on the direct transport; the fault-injection harness gains `model_direct` as a service that can fail/crash |
| `tests/test_runtime.py` | `chat` client tests (body, headers, timeout, thinking off, failures without the key), transport choice, model id mapping, parallel tick tests (N claims in one tick, summary integrity, budget) |
| SKILL.md, ARCHITECTURE.md, HANDOFF, OWNER-GUIDE, KOLO-SKILL-PLAYBOOK | transport, settings, the new ceiling; the "exec 10 s" note stays for the chat session |

## 4. Risks and how each is held

- **Readings drift between transports.** Same prompt, same model,
  temperature 0; the CLI's `--thinking off` is `reasoning_effort: none`
  on the proxy (probed: identical parse rate). Rehearsal on the pod
  compares one inquiry's record read both ways before the version is
  installed for real (`model.provider: cli` then `direct` on the same
  email).
- **Threads and shared state.** Everything per claim is already file
  locked; the tick summary gets a lock; module globals are set before the
  pool starts and read only inside it. A test runs eight claims in one
  tick under the fake and checks every record, claim and card once.
- **The kolo CLI under overlap.** Unknown lock; the retry makes a
  collision a five-second pause; a test injects "database is locked" on a
  card call and expects the card filed.
- **A tick that takes on too much.** Budget checked per submission; each
  call has its own timeout; the tick's own cutoff (4.13.5) still applies
  to renders. A burst of 50 drains over four ticks, not one.
- **Cost.** Direct calls are cheaper (no reasoning tokens); a burst of 20
  is ~60 judgements at ~180 tokens each, cents.
- **Credentials.** Read from the environment at call time; never written,
  logged, or raised; the profile never holds them.

## 5. Order of work

1. `provider.chat` + `judge.complete` transport + tests (one commit).
2. `cli.py` shared retry; `kolo_safe` and the sweep removal (one commit).
3. Parallel tick + settings + readiness line + tests (one commit).
4. Docs, harness, rehearsal script for the live comparison (one commit).
5. One version, 4.14.0, on the owner's word; publish from a refreshed
   folder (46 scripts after the module split); live: the transport
   comparison on one inquiry, then a burst of 12 inquiries from distinct
   addresses timed from send to first card.

## 6. Expected result

| | Today (4.13.11) | 4.14.0 |
|---|---|---|
| Model time per inquiry | 30 to 90 s | 3 to 5 s |
| Claims per tick | 2 | up to 16, 8 at a time |
| Twenty inquiries at once | ~10 ticks, 20 min | one or two ticks, 2 to 4 min |
| Several renderings at once | one after another, ~40 s each | together, ~40 s |
| CLI use per tick | every model call | a few `kolo` card/notice calls |
