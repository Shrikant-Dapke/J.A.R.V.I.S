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
2. The orchestrator stores the user turn, loads the owned conversation
   history, and calls the configured model once per turn: history plus a
   trusted, bounded decision instruction (never persisted).
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
- `POST /api/actions/{id}/approve` — approve exact pending action.
- `POST /api/actions/{id}/deny` — deny pending action.
- `GET /api/actions` — recent action records.
- `GET /api/memory` — inspect approved memory.
- `DELETE /api/memory/{id}` — delete memory item.
- `GET /api/plans/{plan_id}` — inspect the session's validated task plan
  (steps awaiting approval are reconciled with the approval store on read).
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
- `GET /api/health` — service health.

`POST /api/chat` stays HTTP 200 for proposals and returns `status` one of
`success` (plain or tool-result reply, with `tool_result` when a tool
executed), `action_proposed` (pending approval under `action`), or
`plan_proposed` (captured plan under `plan`). Orchestration rejections
map to structured errors without executing anything:
422 (invalid proposal, unknown tool, invalid arguments, a registered
tool without an executable implementation, or a message beyond the
orchestrator instruction budget), 403
(policy deny / approval workflow state), 503 (approval or plan store at
capacity), plus the existing LLM mappings (502/504/500).

Mutating endpoints require `JARVIS_API_TOKEN` and `X-JARVIS-Session`. Configure
exact browser origins with `JARVIS_ALLOWED_ORIGINS`; do not expose the service
on a non-loopback interface. The current approval and conversation stores are
process-local and must be replaced with shared transactional storage before
multi-worker or multi-instance deployment.

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
  conversation history followed by a trusted decision instruction
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
  awaiting approval with the approval store on read and reports which
  steps are ready (all dependencies verifiably succeeded). COMPLETED,
  EXECUTED, and FAILED are final: a failed, rejected, expired, or
  unavailable step cannot be re-requested; address the cause and
  capture a new plan via a new chat turn.
- `GET /api/plans/{plan_id}` reconciles steps awaiting approval with the
  approval store on read and reports which steps are ready (all
  dependencies completed).
- Execution is explicit and single-step per call:
  `request_step_execution` clears exactly one ready step through the
  same policy + approval workflow as chat proposals, holding a plan lock
  so one step can never be requested twice concurrently. There is **no
  scheduler, no automatic advancement, and no autonomous loop**: the
  desktop UI decides which step to request next.

### 10.4 Known limitations
- Conversations remain process-local in-memory structures (same
  shared-storage caveat as section 5).
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
- All execution lifecycle state lives in one local SQLite database
  (`backend/data/jarvis_state.db`, `JARVIS_STATE_DB_PATH` overridable):
  plans and step states, approval records and their executed
  outcomes, execution attempts, and a bounded audit trail. It uses
  WAL + full synchronous commits, `BEGIN IMMEDIATE` transactions, and
  `PRAGMA user_version` migrations; a schema newer than the
  application fails closed at startup. It intentionally excludes
  conversations (still process-local) and never contains secrets,
  key material, or model instructions.
- Developments pending here remain the conversation store and the
  multi-step retry story; the execution lifecycle itself is durable.
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

## Architecture principle
**The model proposes; policy authorizes; tools execute; verification establishes the result.**
