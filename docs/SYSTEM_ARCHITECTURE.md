# JARVIS — System Architecture
**Version:** 1.1 | **Style:** Modular desktop application with local service boundary

## 1. Proposed stack
- Electron desktop shell
- React + TypeScript UI
- Tailwind CSS
- Python + FastAPI backend
- SQLite persistence
- Configurable cloud model adapter (Gemini provider implemented)
- Ollama local adapter (Phase 1: Qwen 3.5 4B Q4_K_M, text generation only)

Versions and provider choices are pinned during implementation planning.

## 2. Components
1. **Desktop UI:** chat, task plan, approval, settings, memory controls, action history.
2. **Local API:** communication between UI and backend.
3. **Orchestrator:** intent handling, model calls, tool-call parsing, task state.
   Implemented as the Main Orchestrator (`backend/app/services/orchestrator.py`,
   Phase 4): it wraps the existing chat service and routes every turn through
   one bounded provider call whose output is parsed as a plain reply, a single
   tool proposal, or a bounded task plan (details in section 10).
4. **Model Router:** chooses configured cloud/local provider by policy and availability.
   Implemented as the provider factory (`backend/app/services/llm/factory.py`)
   with `fake` (offline default), `gemini` (cloud), and `ollama` (local Qwen 3.5
   4B via the loopback Ollama runtime) behind one `LLMProvider` text-in/text-out
   interface. Clients with pooled connections are cached; settings-driven
   selection stays test-overridable. The Ollama base URL is validated as a
   loopback http(s) endpoint, request timeouts are bounded, failures map to
   shared structured LLM errors, and thinking-capable model output is stripped
   of internal reasoning before it can reach any response path.
5. **Tool Registry:** maps tool IDs to typed implementations.
6. **Policy/Approval Manager:** checks permissions and binds approval to exact arguments.
7. **Execution Layer:** invokes narrow tools with bounded privileges.
8. **Verification Layer:** checks postconditions independently.
9. **Memory/Storage:** SQLite settings, conversations, approved memory, audit events.

## 3. Request lifecycle
1. UI submits request to local API (authentication and origin checks run first).
2. The orchestrator appends the user turn to the owned conversation in
   the persistent conversation store (shared SQLite state DB, section
   11), selects that conversation's bounded replay window (newest
   contiguous complete turns; at most 24 turns and 12,000 content
   characters; the just-appended user turn is never dropped), and
   calls the configured model once per turn: window plus a trusted,
   bounded decision instruction (never persisted).
3. The model's text is parsed narrowly as a decision: a plain reply, one
   tool proposal, or one bounded task plan. Non-decision-shaped text
   degrades to a plain reply; decision-shaped-but-invalid output is
   rejected with a structured error and nothing executes.
4. Tool proposals must name a registered tool; anything unknown is
   rejected before any service that could execute runs.
5. Policy determines allow, approval-required, or deny (fail closed).
6. If approval is required, the UI displays the exact action/target;
   nothing executes yet and the response carries the pending approval.
7. Approval is bound to exact validated arguments and expires.
8. Execution layer invokes exactly the approved registered tool once.
9. Verification checks actual postcondition; an executed action is
   summarized back by the model (failure of that call falls back to the
   tool-authored message so the action is never hidden).
10. Task plans are captured and validated but execute nothing on capture;
    each step is requested explicitly through the same policy/approval
    path (section 10). No loop advances plans automatically.
11. Audit event is persisted and structured outcome returned.

## 4. Trust boundaries
- Model cannot authorize itself.
- Model text and arguments are untrusted.
- Model output is parsed only against a narrow structured decision
  contract (`backend/app/schemas/orchestration.py`); malformed
  decision-shaped output, unknown tool names, malformed arguments, and
  invalid plans are rejected before anything can execute.
- Orchestrator instructions are trusted constant text appended by the
  backend as a trailing (never persisted) message; the model can only
  *request* actions, policy and the approval workflow remain the only
  execution authority.
- Tools expose narrow operations, never a general shell.
- Backend policy is authoritative; UI is not the only enforcement point.
- Secrets stay outside source control and are redacted from logs; model
  reasoning is stripped at the provider boundary and never logged.
- Local API binding is restricted to literal loopback addresses.
- Protected chat, conversation, tool, and approval routes require a
  configured bearer token and an explicit client session identifier;
  approval is still a user-facing action, not an assumption made from
  authentication alone.
- Browser state-changing requests require an exact configured Origin. Native
  clients without an Origin remain supported.

## 5. Initial API
- `POST /api/chat` — submit request; receive response, proposed action, or proposed plan.
- `GET /api/approvals` — the session's approval inventory: metadata-only
  summaries (no raw arguments, digest, or retention flags; the
  `action_description` comes from the registered tool's identity and its
  static registry text, never from submitted argument values),
  deterministic `created_at DESC, approval_id DESC` ordering,
  `limit`/`offset` pagination with a paired total. Strictly read-only
  (advisory); an identity with no records observes `200` with an empty
  page and `total: 0`. See section 10.6.
- `GET /api/approvals/{approval_id}` — authoritative approval state with
  lazy TTL expiration applied. Unknown/foreign/purged approvals 404 —
  authorization runs before any payload parse, so this holds even for a
  foreign row whose payload is damaged; only the requesting identity's
  own corrupted record fails closed with a structured 500 and a fixed
  message.
- `POST /api/approvals/{approval_id}/approve` — approve and execute the
  exact pending request once.
- `POST /api/approvals/{approval_id}/reject` — reject a pending request.
- `GET /api/memory` — inspect approved memory.
- `DELETE /api/memory/{id}` — delete memory item.
- `GET /api/plans` — the session's plan inventory: metadata summaries plus
  attention flags (status_counts, awaiting_approval, recovery_required,
  needs_attention), deterministic `created_at DESC, plan_id DESC` ordering,
  `limit`/`offset` pagination with a paired total. Strictly read-only
  (advisory); an identity with no plans observes `200` with an empty page
  and `total: 0`. See section 10.6.
- `GET /api/plans/{plan_id}` — inspect the session's validated task plan
  (steps awaiting approval are reconciled with the approval store on read)
  plus derived status: `overall_status`, `ready_step_ids`, and the
  attention lists (section 10.6).
- `POST /api/plans/{plan_id}/steps/{step_id}/request` — request exactly one
  ready plan step through policy and the approval workflow: 200 returns the
  executed `ToolResult` (policy-allowed read-only tools), 202 returns the
  pending `ApprovalRequest` (mutating tools); 404 unknown plan/step,
  409 step not ready or already terminal, 422 invalid arguments/proposal,
  403 denied, 503 capacity. Steps are terminal once resolved
  (COMPLETED/EXECUTED/FAILED); a failed, rejected, expired, or
  unavailable step cannot be re-requested — capture a new plan instead.
  Approval outcomes (executed, rejected) are recorded on the owning step
  the moment they resolve, so they never depend on approval records that
  terminal retention may purge. No endpoint advances a plan automatically.
- `GET /api/conversations` — metadata listing for the authenticated
  identity with offset pagination (parameters, defaults, errors in
  section 11.5).
- `GET /api/conversations/{conversation_id}` — bounded history read;
  returns the most recent turns and the persisted total (section 11.5).
- `DELETE /api/conversations/{conversation_id}` — delete the session's
  conversation (section 11.5).
- `GET /api/health` — service health.

`POST /api/chat` stays HTTP 200 for proposals and returns `status` one of
`success` (plain or tool-result reply, with `tool_result` when a tool
executed), `action_proposed` (pending approval under `action`), or
`plan_proposed` (captured plan under `plan`). Orchestration rejections
map to structured errors without executing anything:
422 (invalid proposal, unknown tool, invalid arguments, a registered
tool without an executable implementation, or a message beyond the
orchestrator instruction budget), 403
(policy deny / approval workflow state), 503 (approval, plan, or
conversation capacity), plus the existing LLM mappings (502/504/500)
and 500 when persisted conversation state cannot be validated.
Conversation read endpoints fail closed identically: 404 (unknown or
foreign conversation — never distinguishable), 422 (invalid query
parameters), 500 (unvalidatable state; section 11).

Mutating endpoints require `JARVIS_API_TOKEN` and `X-JARVIS-Session`. Configure
exact browser origins with `JARVIS_ALLOWED_ORIGINS`; do not expose the service
on a non-loopback interface. Conversation, plan, execution, and approval
user-visible state all live in the same shared transactional SQLite
database (section 11); the approval store is persisted there too. The
deployment shape keeps its single-writer/process assumptions: one
application process owns writes, and this must be revisited before
any multi-worker or multi-instance deployment.

## 6. Tool interfaces
- `get_system_info()` — bounded read-only result.
- `open_application(app_id)` — ID must map to configured allowlist.
- `create_directory(relative_name, approved_root)` — validate resolved path, enforce containment, require exact-action approval, create, verify.

## 7. Suggested repository layout
```text
JARVIS/
  desktop/
    src/components/
    src/pages/
    src/services/
    electron/
  backend/
    app/api/
    app/core/
    app/models/
    app/memory/
    app/tools/
    app/security/
    app/verification/
    tests/
  docs/
  data/
  .env.example
  .gitignore
  README.md
```

## 8. Failure handling
- Model unavailable: show provider error; do not fabricate. This includes the
  local Ollama runtime being stopped or unreachable: the provider maps the
  failure to a safe structured error (connection/HTTP → 502, timeout → 504)
  and never falls back silently to another provider.
- Invalid model proposal (malformed decision text, unknown tool, invalid
  plan or arguments, registered tool without an implementation):
  structured 422; nothing executes.
- User request beyond the orchestrator instruction budget: structured
  422; the request is never silently truncated and the model is never
  called with a partial request.
- Policy denies a proposal: structured 403; nothing executes.
- Approval or plan store at capacity: structured 503; nothing executes
  (fail closed, never degrade into silent execution).
- Conversation capacity exhausted: structured 503; the turn fails
  closed, nothing is stored, and history is never trimmed FIFO to make
  room for identity conversation slots.
- Conversation state cannot be validated: structured 500
  ("Conversation history could not be validated."); damaged turns or
  metadata are never repaired, fabricated, or silently dropped on any
  read or write, while deletion (DELETE) stays possible for damaged
  rows (section 11.2).
- Validation failure: reject before execution.
- Approval denied/expired: do not execute.
- Tool exception: record failure and return safe message.
- Verification failure: do not report success.
- Persistence failure: disclose history/memory save failure.

### 8.1 Execution reliability (Phase 4.3)
Every approved or policy-allowed execution runs inside a persisted,
question-honest lifecycle (see section 10.5):

- Tool executions carry a bounded wall-clock budget
  (`TOOL_TIMEOUT_SECONDS`, 0.05–120) enforced at the execution
  boundary, separate from provider timeouts. A timeout is NEVER
  reported as "failed" (no side effect) or "succeeded": a mutating
  tool's attempt is recorded `TIMED_OUT` with NO outcome and its plan
  step becomes `RECOVERY_REQUIRED`; read-only tools are provably
  side-effect free and their timeouts end as ordinary `FAILED`
  executions with the recorded evidence.
- Uncertain steps resolve ONLY through explicit human reconciliation
  (`POST /api/plans/{plan_id}/steps/{step_id}/resolve-recovery`) to a
  `FAILED` step classified `execution_uncertain`; the standing attempt
  is finalized `RECONCILED`. There is no automatic retry: depends-on
  chains stay blocked until a human decides.
- A crash between the approval claim and the outcome is recovered at
  startup (`run_startup_recovery`, idempotent): claimed-but-never-
  started attempts resolve `FAILED(execution_interrupted)` (provably no
  side effect); started attempts whose outcome was not recorded become
  `RECOVERY_REQUIRED` (side effect unknown); durably recorded outcomes
  the step never observed are applied verbatim.
- Approval resolution is single-use and backend-enforced: the database
  itself forbids a second open attempt per approval, so double
  approval, replayed execution, and concurrent same-step claims are
  impossible — including across processes.
- Tool-layer failures are recorded as failed executions with fixed
  safe messages; stack traces, exception internals, sensitive argument
  values, and session identifiers never reach API responses or audit
  rows.

## 9. Decisions to revisit
Cloud provider/local model; backend process lifecycle; voice/wake-word; packaging and updates; memory retention/export; local API security mechanism.

## 10. Phase 4 — Main Orchestrator and task plans
### 10.1 Decision contract (Milestone 4.1)
- The orchestrator makes one bounded provider call per turn: the
  selected bounded replay window of the conversation's persisted
  history (section 11.4) followed by a trusted decision instruction
  (`[ORCHESTRATOR INSTRUCTION - JARVIS]`) that includes the tool catalog
  from the registry as metadata only. The instruction is appended as a
  trailing user message and is never stored in conversation history.
- The instruction is composed from bounded parts (constant rules, tool
  catalog, user-request slot), each with its own budget under
  `MAX_INSTRUCTION_LENGTH` (4000): the request slot is
  `MAX_USER_REQUEST_SLOT` (1500) and the catalog is
  `MAX_TOOL_CATALOG_LENGTH` (1300). The complete user request always
  reaches the model: a request larger than its slot fails closed with
  `OrchestrationInstructionBudgetError` (structured 422) before the
  provider is called, and catalog metadata outgrowing its budget drops
  whole catalog lines with an "(additional registered tools omitted)"
  note rather than ever cutting the request. Trusted parts that cannot
  fit at all also fail closed instead of truncating anything.
- The raw model text is parsed by `parse_orchestrator_decision`
  (`backend/app/schemas/orchestration.py`) into exactly one of:
  - a plain reply (`reply` key or any non-JSON/non-decision text),
  - a single tool proposal (`{"action": {"tool", "arguments", ...}}`),
  - a bounded task plan (`{"plan": {"goal", "steps": [...]}}`).
- Decision-shaped but invalid output (missing fields, extra steps,
  malformed arguments) raises `OrchestrationInvalidProposalError` and
  maps to structured 422 — it is never repaired or executed.
- Executed read-only actions get one additional bounded provider call
  that summarizes the verified `ToolResult`; if that summary call fails
  the tool-authored message is used so an executed action is never hidden.

### 10.2 Routing through existing Phase 1–3 services
- Reply → assistant turn on `status="success"`.
- Tool action → registry check, then the existing
  `ApprovalService.request_tool`: policy-allowed read-only tools execute
  exactly once and return `tool_result`; mutating tools return a pending
  exact-argument approval (`status="action_proposed"`, HTTP 200); policy
  denials and approval workflow errors surface as 403. A tool registered
  as metadata without an executable implementation fails closed as an
  invalid proposal (422) — nothing executes and no step is left
  mid-flight. Approval bindings
  remain bound to exact validated arguments, expiring and single-use.
- Plan → `PlanService.create_plan` validates and captures the plan and
  executes nothing on capture (`status="plan_proposed"`).

### 10.3 Task plan foundation (Milestone 4.2)
- `TaskPlan`/`PlanStep` (`backend/app/schemas/plan.py`): unique
  alphanumeric step IDs, registered tool per step, string-only bounded
  arguments, secret-like argument keys rejected, and dependencies that
  may only reference strictly earlier steps — cycles are structurally
  impossible (no DAG engine exists or is needed).
- Bounds: ≤ 8 steps, goal/description ≤ 512 chars, ≤ 4 dependencies per
  step; `PlanService` (`backend/app/services/plan_service.py`) keeps at
  most 256 plans owner/session scoped.
- Step statuses are outcome-accurate: `PENDING`, `AWAITING_APPROVAL`,
  `COMPLETED` (a successful policy-allowed non-approval `ToolResult`
  recorded on the step), `EXECUTED` (an approved action executed
  successfully; its `ToolResult` is recorded on the step), and `FAILED`
  (any other terminal outcome — execution failure, denial, approval
  expiry, unavailable tool — always with its recorded result).
- Approval outcomes are attached to the owning step **immediately** via
  the approval-service outcome listener (approve→execute and reject are
  recorded the moment they resolve), so plan state never depends on
  re-reading approval records that terminal retention may purge. The
  read-time reconcile remains only as a fallback for approvals that
  expired or were purged while still pending (`approval_expired`).
- Dependency readiness requires *proof of success*: a step's
  dependencies are satisfied only by a recorded `ToolResult` with
  `success=True` and status `COMPLETED`/`EXECUTED`. An outcome-blind
  status can never unblock a dependent step — a failed or rejected
  parent leaves its dependents permanently not-ready.
- Terminal semantics: `GET /api/plans/{plan_id}` reconciles steps
  awaiting approval with the approval store on read, reports which
  steps are ready (all dependencies verifiably succeeded), and derives
  the whole-plan status (section 10.6). COMPLETED, EXECUTED, and FAILED
  are final: a failed, rejected, expired, or unavailable step cannot be
  re-requested; address the cause and capture a new plan via a new
  chat turn.
- Execution is explicit and single-step per call:
  `request_step_execution` clears exactly one ready step through the
  same policy + approval workflow as chat proposals, holding a plan lock
  so one step can never be requested twice concurrently. There is **no
  scheduler, no automatic advancement, and no autonomous loop**: the
  desktop UI decides which step to request next.

### 10.4 Known limitations
- Conversation history is now durable bounded storage (section 11):
  FIFO turns within a conversation and a fixed identity capacity, but
  there is no summarization, semantic recall, or long-term memory yet.
- Unknown-tool proposals surface as 422 at proposal time rather than 404
  because registration is checked before anything executes.
- Step arguments are `dict[str, str]`, matching the existing
  `ToolInvokeRequest` contract; richer argument typing is future work.
- The orchestrator trusts the configured provider boundary for thinking
  avoidance; reasoning is additionally stripped by the Ollama provider.
- Execution is deliberately synchronous: each plan-step request and
  approval resolution runs inline within the calling request path under
  plan locks, with no background supervisor. This keeps every execution
  on the audited policy/approval boundary, but a slow tool serializes
  callers of the same plan; offloading execution to bounded background
  workers is future performance work, not a correctness gap.

### 10.5 Execution lifecycle and state (Phase 4.3)
- All execution lifecycle state AND conversation history live in one
  local SQLite database (`backend/data/jarvis_state.db`,
  `JARVIS_STATE_DB_PATH` overridable; schema version 2, section 11):
  plans and step states, approval records and their executed
  outcomes, execution attempts, a bounded audit trail, and
  conversations with their ordered turns. It uses WAL + full
  synchronous commits, `BEGIN IMMEDIATE` transactions, and
  `PRAGMA user_version` migrations; a schema newer than the
  application fails closed at startup. It never contains secrets,
  key material, or model instructions.
- Developments pending here remain the multi-step retry story; approvals,
  like conversation history and the execution lifecycle, are durable
  (section 11).
- Execution attempts are first-class rows with unique ids and a
  central state machine: `CLAIMED → STARTED → SUCCEEDED | FAILED |
  TIMED_OUT`, `CLAIMED → FAILED` (interrupted), `TIMED_OUT →
  RECONCILED` only. Every transition is validated centrally;
  terminal states absorb; a `TIMED_OUT` attempt carries NO outcome.
- Plan-step states gained `RECOVERY_REQUIRED`: a mutating step whose
  side effect could not be proven (crash after invocation started, or
  a mutating timeout). It is non-terminal, never satisfies dependency
  readiness, and resolves only via explicit reconciliation to
  `FAILED(execution_uncertain)`.
- Startup recovery is idempotent and honest: provable no-side-effect
  crashes resolve interrupted; provable outcomes are replayed
  verbatim; unknown outcomes require a human. Recovery counters are
  logged and audited (`recovery_required`, `interrupted`,
  `outcome_applied`, `errors`).

### 10.6 Inventories and derived plan status (Phase 5.2; cleanup isolation Phase 5.3)
- `GET /api/plans` and `GET /api/approvals` are the session identity's
  read-only inventories: deterministic `created_at DESC, id DESC`
  ordering, `limit`/`offset` pagination served with a paired identity-
  scoped total from ONE `BEGIN IMMEDIATE` transaction, and explicit
  capacity errors (422 for bad query parameters). An identity with no
  records observes `200` with an empty page and `total: 0` — other
  identities' resources are never listed and never distinguishable from
  unknown ones. Both summaries are metadata-only: approval listings
  carry tool name, action description, risk level, persisted status, and
  timestamps — never raw arguments, digests, or retention flags. The
  `action_description` in inventories is built from the registered
  tool's identity and its static registry description (never from
  submitted argument values), so no execution argument value can reach
  a listing — flat or nested values alike.
- Inventory reads are strictly **advisory**: no reconcile, no lazy
  expiration, no state transition, no audit events, no attempts. The
  **authoritative** reads are `GET /api/plans/{plan_id}` (reconciles
  steps awaiting approval with the approval store, reports ready steps
  and derived status) and `GET /api/approvals/{approval_id}` (applies
  lazy TTL expiration). Advisory listings may therefore trail the
  authoritative state until a detail read.
- Both detail reads authorize (owner + session) BEFORE parsing payloads:
  unknown, foreign, and purged resources observe 404 and never reveal
  themselves — including a foreign row whose payload is damaged, which
  is answered exactly like any other foreign resource (404). A corrupted
  persisted plan or approval payload of the REQUESTING identity fails
  closed with a structured 500 and a fixed, non-sensitive message
  ("Plan state could not be validated." / "Approval state could not be
  validated.") — no raw payload, arguments, digest, or identifiers in
  responses; never repaired or served.
- The approval maintenance scan (TTL expiry + terminal-retention purge)
  is row-local and metadata-driven (Phase 5.3): expiry and retention
  decisions come from the persisted status/deadline columns only, the
  payload is read exactly once and only to rewrite a record expiring
  right now, and every row is handled independently — one record with
  invalid metadata or a damaged payload is skipped (logged, lifecycle
  untouched: no invented timestamps, no invented transitions, no
  purging) instead of aborting the scan. Corrupt terminal rows still
  purge purely by metadata once their retention window passes, so
  capacity keeps its retention guarantee; healthy TTL, retention,
  single-use claims, lifecycle transitions, and the expiry audit event
  are preserved exactly, and one identity's damage can never disrupt
  unrelated identities' approval operations.
- `GET /api/plans/{plan_id}` additionally derives deterministic status
  from persisted step state: `overall_status` with the exact precedence
  `recovery_required > awaiting_approval > actionable > failed >
  in_progress > completed`, `ready_step_ids` (PENDING steps whose
  dependencies all have recorded successful results), and the attention
  lists. `needs_attention` is true while any step is RECOVERY_REQUIRED
  or AWAITING_APPROVAL. A plan with unresolvable malformed state fails
  closed instead of inventing a status.
- Uncertain mutating executions surface as `RECOVERY_REQUIRED` steps in
  inventory, detail, and the derived status. They require humans: the
  resolution path `POST /api/plans/{plan_id}/steps/{step_id}/resolve-
  recovery` resolves the step FAILED with `execution_uncertain` and
  finalizes the standing mutation attempt (RECONCILED). There is NO
  automatic retry and no automatic recovery decision anywhere.
- `recovery_scan_completed` audit events are process-level summaries:
  detail carries exactly the counters `interrupted`,
  `recovery_required`, `outcome_applied`, `errors` (integers), with no
  identity fields and one event per scan. Per-work recovery events
  (`execution_interrupted`, `recovery_required`,
  `recovery_outcome_applied`) remain identity-scoped and reference the
  concrete plan/approval rows they touched.

## 11. Conversation persistence and bounded context (Phase 5.1)

### 11.1 Storage
- `PersistentConversationStore`
  (`backend/app/services/conversation_store.py`) is the single
  conversation authority for chat, orchestration, and the API routes;
  it wraps the shared `ExecutionStateDb` façade. There is exactly one
  conversation persistence implementation, and no conversation state
  lives in process memory.
- Conversations join every other durable structure in one local SQLite
  database (`backend/data/jarvis_state.db`, `JARVIS_STATE_DB_PATH`
  overridable), migrated to schema version 2 (`PRAGMA user_version`;
  a newer schema fails closed at startup). Version 2 adds:
  - `conversations` — `conversation_id`, `owner_id`, `session_id`,
    `created_at`, `updated_at`, non-negative `turn_count`; the
    identity columns make every read identity-scoped, and an index
    makes the listing order `updated_at DESC, conversation_id DESC`
    deterministic.
  - `conversation_messages` — one row per turn: identity +
    conversation, a per-conversation monotone sequence number (`seq`),
    `role` (CHECK: user/assistant), bounded `content`
    (CHECK: non-empty, ≤ 4,000 characters), and a preserved verbatim
    timestamp. Turns are read back strictly in `seq` order and
    returned exactly as stored.
- Every mutation is ONE `BEGIN IMMEDIATE` transaction on the façade:
  the capacity check and conversation creation commit atomically, and
  append + FIFO retention + metadata updates commit or roll back
  together. Listing pagination is served by one façade transaction
  (`list_conversations_page`) that pairs the page rows with the
  identity-scoped total, so a response never mixes instants under
  concurrent chat traffic.
- History survives backend restarts; the state file is gitignored.

### 11.2 Identity and security
- Every conversation is bound at insertion to its creator's `(owner_id,
  session_id)` — the authenticated bearer-token owner identity plus
  the explicit session header — and ownership is never rebound.
- Reads, writes, listings, and deletions are always scoped to the
  caller's exact identity. Another identity's conversation is
  indistinguishable from an unknown one (structured 404, identical
  response), listing never contains foreign metadata, and no response
  ever leaks cross-identity content.
- All conversation endpoints require token authentication; DELETE
  additionally requires a safe configured browser Origin; GET
  endpoints are read-only and never validate Origin.
- Reads validate strictly: a turn's role, content bounds, and
  timestamp, and a conversation's `created_at`/`updated_at`, are
  checked on every data-serving read. Identity rejection runs BEFORE
  validation, keeping foreign damaged state indistinguishable from
  unknown state. Damaged state raises
  `ConversationCorruptError` ("Conversation history could not be
  validated."): it is never repaired, fabricated, or silently
  dropped — the affected request fails closed — while DELETE stays
  possible for damaged rows because deletion never has to interpret
  content. Error messages and logs carry identifiers only, never
  conversation content or credentials.

### 11.3 Retention and capacity
- At most 400 turns per conversation (`MAX_CONVERSATION_TURNS`):
  appending beyond that drops the OLDEST turns FIFO. The per-turn
  content bound is 4,000 characters.
- At most 256 conversations per identity (`MAX_CONVERSATIONS_PER_IDENTITY`,
  owner/session scoped). A further creation fails closed with
  `ConversationCapacityError` (structured 503): no summaries, no
  silent replacement, and history is never trimmed to create a slot.
- Listing/order helpers exist independently of these ceilings; the API
  documents its own page bounds in 11.5.

### 11.4 Bounded context retrieval (replay window)
- Per turn, the orchestrator sends the newest contiguous span of
  complete turns that fits both hard budgets — at most 24 turns
  (`MAX_HISTORY_REPLAY_TURNS`) and 12,000 content characters
  (`MAX_HISTORY_REPLAY_CHARS`; equality fits) — selected by the pure
  function `select_replay_window`
  (`backend/app/services/orchestrator.py`). The backward walk stops at
  the FIRST turn that does not fit, so the provider sees a contiguous
  newest span and never a hole-punched history, and the
  just-appended current user turn is always kept even if it alone
  exhausts the remaining budget.
- Turns are sent chronologically (oldest → newest). Selection never
  mutates persistent history: storage keeps up to the full 400 turns
  and only the provider view is bounded.
- Formal maximum prompt contribution of persisted context: at most
  12,000 characters of history plus the bounded trusted instruction
  overhead — at most 4,000 characters per applicable trusted
  instruction part (`MAX_INSTRUCTION_LENGTH` = 4000 for the whole
  decision instruction, itself composed of the bounded parts in 10.1).
- These are character limits enforced by this codebase. They are
  distinct from provider-specific token accounting (providers translate
  the same characters into different token counts) and from the current
  user request: the request slot inside the instruction (1,500 of its
  4,000 characters) bounds the user request, which always reaches the
  model or fails closed as a 422 (never silently truncated).

### 11.5 Conversation API
All three endpoints require authentication (bearer `JARVIS_API_TOKEN`
+ session header); GET endpoints are read-only; DELETE additionally
validates the browser Origin. Errors are the shared structured error
envelope; foreign and unknown conversations always share one 404.

`GET /api/conversations`
- Query: `limit` (default 50; allowed 1–200), `offset` (default 0;
  allowed ≥ 0). Invalid values → 422 `VALIDATION_ERROR`.
- Response 200: `{"conversations": [{"conversation_id",
  "created_at", "updated_at", "turn_count"}, ...], "total", "limit",
  "offset"}` — metadata only, never message content or identity
  fields beyond the ordering itself.
- Ordering: `updated_at DESC, conversation_id DESC` with the most
  recently active conversation first; a repeated page is identical
  while rows are unchanged, and page rows plus `total` come from one
  consistent transaction (11.1).
- Errors: 401 (unauthenticated), 422 (invalid parameters), 500
  (metadata that cannot be validated).

`GET /api/conversations/{conversation_id}`
- Query: `limit` (default 100; allowed 1–400) — returns the MOST
  RECENT `limit` turns while preserving chronological (oldest →
  newest) ordering; reading never modifies stored history.
- Response 200: `{"conversation_id", "turns": [{"role", "content",
  "timestamp"}, ...], "turn_count", "total_turn_count"}` where
  `turn_count` counts the turns in THIS response (backward-compatible
  windowed view) and `total_turn_count` is the persisted total.
- Errors: 401 (unauthenticated), 404 (unknown or foreign —
  indistinguishable), 422 (invalid parameters), 500 (state that
  cannot be validated).

`DELETE /api/conversations/{conversation_id}`
- Removes the identity's conversation and every stored turn in one
  transaction; it also succeeds on rows whose stored state cannot be
  validated because deletion never interprets content.
- Response 200: `{"conversation_id", "status": "deleted"}`.
- Errors: 401 (unauthenticated), 403 (missing/foreign Origin), 404
  (unknown or foreign). This pre-existing contract is unchanged by
  Phase 5.1.

## Architecture principle
**The model proposes; policy authorizes; tools execute; verification establishes the result.**
