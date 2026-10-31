# JARVIS — System Architecture
**Version:** 1.0 | **Style:** Modular desktop application with local service boundary

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
1. UI submits request to local API.
2. Orchestrator calls configured model.
3. Output is parsed as response or structured tool intent.
4. Registry validates tool and schema.
5. Policy determines allow, approval-required, or deny.
6. If required, UI displays exact action/target; nothing executes yet.
7. Approval is bound to exact validated arguments and expires.
8. Execution layer invokes tool.
9. Verification checks actual postcondition.
10. Audit event is persisted and structured outcome returned.

## 4. Trust boundaries
- Model cannot authorize itself.
- Model text and arguments are untrusted.
- Tools expose narrow operations, never a general shell.
- Backend policy is authoritative; UI is not the only enforcement point.
- Secrets stay outside source control and are redacted from logs.
- Local API binding is restricted to literal loopback addresses.
- Protected chat, conversation, tool, and approval routes require a
  configured bearer token and an explicit client session identifier;
  approval is still a user-facing action, not an assumption made from
  authentication alone.
- Browser state-changing requests require an exact configured Origin. Native
  clients without an Origin remain supported.

## 5. Initial API
- `POST /api/chat` — submit request; receive response or proposed action.
- `POST /api/actions/{id}/approve` — approve exact pending action.
- `POST /api/actions/{id}/deny` — deny pending action.
- `GET /api/actions` — recent action records.
- `GET /api/memory` — inspect approved memory.
- `DELETE /api/memory/{id}` — delete memory item.
- `GET /api/health` — service health.

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
- Validation failure: reject before execution.
- Approval denied/expired: do not execute.
- Tool exception: record failure and return safe message.
- Verification failure: do not report success.
- Persistence failure: disclose history/memory save failure.

## 9. Decisions to revisit
Cloud provider/local model; backend process lifecycle; voice/wake-word; packaging and updates; memory retention/export; local API security mechanism.

## Architecture principle
**The model proposes; policy authorizes; tools execute; verification establishes the result.**
