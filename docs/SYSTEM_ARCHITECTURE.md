# JARVIS — System Architecture
**Version:** 1.0 | **Style:** Modular desktop application with local service boundary

## 1. Proposed stack
- Electron desktop shell
- React + TypeScript UI
- Tailwind CSS
- Python + FastAPI backend
- SQLite persistence
- Configurable cloud model adapter
- Optional Ollama local adapter, subject to compatibility/performance testing

Versions and provider choices are pinned during implementation planning.

## 2. Components
1. **Desktop UI:** chat, task plan, approval, settings, memory controls, action history.
2. **Local API:** communication between UI and backend.
3. **Orchestrator:** intent handling, model calls, tool-call parsing, task state.
4. **Model Router:** chooses configured cloud/local provider by policy and availability.
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
- Local API is restricted to loopback and protected against unintended cross-origin access.

## 5. Initial API
- `POST /api/chat` — submit request; receive response or proposed action.
- `POST /api/actions/{id}/approve` — approve exact pending action.
- `POST /api/actions/{id}/deny` — deny pending action.
- `GET /api/actions` — recent action records.
- `GET /api/memory` — inspect approved memory.
- `DELETE /api/memory/{id}` — delete memory item.
- `GET /api/health` — service health.

Implement API authentication/origin protections before enabling mutating endpoints.

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
- Model unavailable: show provider error; do not fabricate.
- Validation failure: reject before execution.
- Approval denied/expired: do not execute.
- Tool exception: record failure and return safe message.
- Verification failure: do not report success.
- Persistence failure: disclose history/memory save failure.

## 9. Decisions to revisit
Cloud provider/local model; backend process lifecycle; voice/wake-word; packaging and updates; memory retention/export; local API security mechanism.

## Architecture principle
**The model proposes; policy authorizes; tools execute; verification establishes the result.**
