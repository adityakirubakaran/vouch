# How Vouch works

This is the version for engineers. The README covers what Vouch does and how to run it.

## Processes and state

Vouch is one Python process (FastAPI + asyncio) and a SQLite database in `data/vouch.db`. The React app talks to it over REST and Server-Sent Events; calls use WebSockets.

Everything that matters is in the database, never only in memory:

| Table | Holds |
|---|---|
| `applications` | uploaded file, extracted application, applicant checks, decision, review, sync result |
| `references` | each trade reference and the bank: verification checks, flag, findings, outcome |
| `steps` | the workflow graph: status, dependencies, handler state, scheduled retry, wait timeout |
| `calls` | every call attempt: channel, status, transcript, live notes, final result |
| `emails` | bank requests and replies, requests for more information |
| `evidence` | citable facts (`call-ref-1`, `bank`, `reg-applicant`, `phone-ref-1`, `fraud-ref-3`...) that memo sentences point to |
| `events` | the activity log shown in the app |

## The engine (`engine.py`)

A step is a row with a `kind` (extract, verify, call, bank, memo, sync), a list of steps it depends on, and a JSON `state` its handler owns. A loop polls for steps that are pending and due, runs up to eight handlers concurrently, and records what each returned:

- `Done(result)`: finished. The workflow's `after_step` hook adds the next steps in the same transaction.
- `Wait(reason, timeout)`: park until `engine.signal(step_id, payload)` is called (call ended, email arrived) or the timeout passes. A signal that arrives while the handler is still running (a call that fails while it is being dialed) is held, and the step runs again as soon as the handler returns instead of parking.
- `Retry(after, reason)`: run again at a later time (the reference did not pick up). The time is stored, so it survives restarts.
- `Skip(reason)`: not needed (no bank on the application).
- Raising `StepFailed` fails immediately; any other exception retries with backoff up to `max_attempts`. Only handler errors count toward that limit, so a step that waits and wakes several times keeps its full retry budget.

A dependency can be soft (the memo runs even if a call ended unreachable) or required (`state.requires`: the memo never runs if verification failed).

On startup, steps that were `running` go back to `pending` (handlers are idempotent), and calls that were live are recorded as failed so their steps retry; any Twilio call still ringing is hung up. `tests/test_flow.py::test_restart_resumes_waiting_steps` stops the engine mid-application and finishes it in a new process.

Why not Temporal or Celery: the workload is a few steps per application that mostly wait. A 300-line engine over SQLite is easy to read, has no extra services to run for a demo, and its semantics are covered by `tests/test_engine.py`.

## The workflow (`workflow.py`)

```
extract ─┬─► verify ─► call(ref) × verified refs ─┐
         └─► bank ────────────────────────────────┴─► memo ─► [analyst] ─► sync
```

- **extract** sends the PDF (or image) to Claude as a `document` block with a forced `record_credit_application` tool whose input schema is the `CreditApplication` Pydantic model. The output is validated; on a validation error Claude gets the error back once to correct it.
- **verify** looks up the applicant and each reference (registry, domain registration date, phone numbers on the website), looks up who each reference's phone number belongs to (`providers/phone.py`: IPQualityScore, Twilio Lookup, Google Places, in parallel), and checks where Vouch has seen the number before. It runs the fraud rules and scores each reference (`agents/scoring.py`, a logistic scorecard with weights in the credit policy), writes display rows, the score with its findings, and evidence rows, then creates one call step per unflagged reference. A critical finding or a score under the policy's `flag_below` flags a reference.
- **call** places an attempt and waits. When the call ends it decides: completed (save findings), declined or wrong number (done), or no answer / voicemail / callback requested (retry until `CALL_MAX_ATTEMPTS`).
  - A call that ends before the agent hears a high credit figure (the line dropped, the reference hung up) counts as failed and is retried, not recorded as a finished reference.
  - If Claude errors mid-reply, the turn is retried once. If that fails too, the agent apologizes, hangs up, and the reference is called back later.
  - The Twilio relay only accepts live Twilio calls. A late answer on a call that already ended is hung up instead of interviewed.
- **bank** emails the request and waits for a reply or the timeout, then has Claude read the reply into `BankFindings`.
- **memo** runs the credit policy, then asks Claude for the narrative (falls back to a template if Claude is unavailable).
- **sync** runs after an analyst approves: QuickBooks or the local ledger.

## Calls (`calls.py`, `agents/voice.py`)

`RelaySession` is one conversation. It speaks Twilio's ConversationRelay messages:

| Direction | Message | Meaning |
|---|---|---|
| in | `setup` | call connected (carries `customParameters.call_id`) |
| in | `prompt {voicePrompt, last}` | what the reference said |
| in | `interrupt {utteranceUntilInterrupt}` | they talked over the agent; the agent stops streaming and history is trimmed to what they heard |
| out | `text {token, last}` | streamed agent speech |
| out | `end {handoffData}` | hang up |

Each reference turn triggers a streamed Claude turn (Haiku) with one tool, `end_call`, which the model uses for completed, callback requested, declined, wrong number, or voicemail. In parallel, a second Haiku call keeps live notes from the transcript so far, which stream to the review screen. When the call ends, Sonnet reads the full transcript into a `CallResult`, using numbers the reference said or confirmed (not the agent's read-back).

Channels:

- **twilio**: `POST /Calls` with inline TwiML `<Connect><ConversationRelay url="wss://…/twilio/relay" welcomeGreeting="…">`. Status callbacks map busy / no-answer / failed to outcomes; async answering machine detection replaces the call's TwiML with a short voicemail and hangs up. Webhooks are verified with Twilio's HMAC-SHA1 signature.
- **browser**: the `/phone` page connects to `/ws/phone/{call_id}`, uses the Web Speech API for recognition and synthesis, and sends the same messages.
- **simulated**: `PersonaBot` plays the reference over an in-memory transport, using Claude with the persona's facts and behavior (or scripted lines offline).

## Claude usage

| Job | Model | Mode |
|---|---|---|
| Read application | `MODEL_SMART` (Sonnet 5.5) | forced tool, PDF document block |
| Live call turns | `MODEL_FAST` (Haiku 5.5) | streamed, `end_call` tool |
| Live notes during a call | `MODEL_FAST` | forced tool |
| Final transcript read | `MODEL_SMART` | forced tool |
| Bank reply | `MODEL_SMART` | forced tool |
| Memo narrative | `MODEL_SMART` | forced tool, citations validated |
| Simulated reference | `MODEL_FAST` | forced tool |

`FakeLLM` implements the same interface with deterministic handlers registered next to each agent, so the full workflow runs offline and in tests.

## Privacy and compliance notes

- Only the last four digits of a bank account are extracted.
- The agent discloses that it is an AI and that the call is recorded, and never shares the applicant's requested amount.
- Declines produce a draft adverse action notice with the policy's principal reasons and the ECOA notice text, for a person to review and send.
- Calls are made only to references the applicant listed on a signed application that authorizes reference checks; an unsigned application stops at "ask for more information."
