# J.A.R.V.I.S.

**Just A Rather Very Intelligent System**

A practical, hybrid AI desktop assistant for Windows.

## Vision
JARVIS understands natural-language requests, plans bounded tasks, uses controlled desktop tools, retains user-approved context, and verifies whether actions actually succeeded.

## MVP
- Text-based assistant interface
- Hybrid cloud + local AI architecture
- Controlled desktop tools
- Approval gates for important actions
- Persistent, user-controlled memory
- Action logging and independent verification

## Initial tools
- `get_system_info`
- `open_application`
- `create_directory`

## Architecture principle
> **The model proposes; policy authorizes; tools execute; verification establishes the result.**

## Proposed stack
- Electron
- React + TypeScript
- Tailwind CSS
- Python + FastAPI
- SQLite
- Configurable cloud model adapter
- Optional local model adapter

## Scope discipline
JARVIS v0.1 excludes unrestricted shell execution, a custom operating system, a custom programming language, unattended external actions, and unrestricted computer control.

## Documentation
- [Project Charter](docs/PROJECT_CHARTER.md)
- [Software Requirements Specification](docs/SRS.md)
- [System Architecture](docs/SYSTEM_ARCHITECTURE.md)

## Backend Setup

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Set `JARVIS_API_TOKEN` in `.env` to a random value of at least 32
characters before calling protected routes.

Health check: `GET http://127.0.0.1:8000/api/health`

### Local API Security

The backend is loopback-only. `HOST` must be a literal loopback address and
the application rejects requests if its ASGI server reports a non-loopback
bind. Set `JARVIS_API_TOKEN` to a high-entropy secret before using protected
chat, conversation, tool, or approval routes:

```text
Authorization: Bearer <JARVIS_API_TOKEN>
X-JARVIS-Session: <random-per-process-session-id>
```

Native clients may alternatively pass the token as `X-JARVIS-Token`
(Swagger UI exposes both fields plus the session header); if both token
headers are present they must match. Until `JARVIS_API_TOKEN` is
configured on the server, protected requests fail closed with a safe
`503 API authentication is not configured.` response — supplied client
tokens cannot bypass that.

The token authenticates the local API client and the session binds approvals
and conversations to that client session. It is not evidence of informed user consent. A future
desktop approval experience must present the exact action and obtain the
user's decision before calling the approval endpoint. Browser clients must
also use an exact origin listed in `JARVIS_ALLOWED_ORIGINS`; native clients
may omit `Origin`.

Run tests:
```bash
pytest tests/ -v
```

## Intelligent Orchestration (Phase 4)

Chat turns are driven by the Main Orchestrator
(`backend/app/services/orchestrator.py`): each turn makes **one bounded
model call** whose output is parsed as a plain reply, a single tool
proposal, or a bounded task plan. The model can only *request* actions —
every proposal routes through the existing policy and approval workflow,
so model output can never bypass or weaken it.

`POST /api/chat` stays HTTP 200 and returns `status`:

| `status` | Meaning |
|----------|---------|
| `success` | plain reply, or an executed action with its `tool_result` |
| `action_proposed` | pending exact-argument approval under `action` |
| `plan_proposed` | captured, validated task plan under `plan` |

Rejected proposals map to structured errors **without executing
anything**: `422` (malformed decision, unknown tool, invalid arguments
or plan, or a message too large for the orchestrator instruction
budget), `403` (policy deny / approval workflow), `503` (approval or
plan store at capacity), plus the existing LLM error mappings
(`502`/`504`/`500`). The complete user request is never silently
truncated to fit the instruction budget; oversized requests fail
clearly instead.

### Task plans (Milestone 4.2)

Plans are validated, bounded representations — ≤ 8 steps, unique step
IDs, dependencies referencing only strictly earlier steps (cycles are
structurally impossible), registered tools only, string-only arguments.
Capturing a plan executes nothing; steps run **one at a time, explicitly**:

```text
GET  /api/plans/{plan_id}                              # inspect plan (approval state reconciled)
POST /api/plans/{plan_id}/steps/{step_id}/request      # run one ready step through policy/approval
```

A policy-allowed read-only step executes and returns the `ToolResult`
(200); a mutating step returns a pending approval (202) bound to the
exact arguments. There is no scheduler, no auto-advancement, and no
autonomous loop — the desktop UI decides which step to request next.

Steps are **terminal once resolved** and always carry their recorded
outcome: `COMPLETED`/`EXECUTED` mean the step's `ToolResult` succeeded
(approval outcomes are recorded on the step the moment they resolve,
so they survive approval-record retention), and `FAILED` covers every
other ending (execution failure, denial, approval expiry, or an
unavailable tool). A failed, rejected, or expired step cannot be
re-requested — address the cause and capture a new plan with a new
chat turn instead.

## LLM Providers

Three interchangeable providers live behind `backend/app/services/llm/`. The
provider contract is unchanged: providers receive the conversation history
plus a trailing trusted orchestrator instruction (never persisted) and
return plain assistant text only. **The model never executes tools and its
output is treated as untrusted** — the Phase 4 orchestrator parses it into
a reply, tool proposal, or plan, and all proposals still route through
policy and the approval workflow.

| Provider | Purpose | Requires |
|----------|---------|----------|
| `fake`   | Deterministic offline default, no network | nothing |
| `gemini` | Cloud generation via the Google Gemini API | `GEMINI_API_KEY` |
| `ollama` | Local generation via Qwen 3.5 4B on Ollama | local Ollama runtime |

Switch by setting `LLM_PROVIDER` in `backend/.env` (`fake`, `gemini`, or
`ollama`) and restarting the backend. The selected client is cached and reused
across requests; the test override (`set_llm_provider_override`) keeps taking
precedence.

### Ollama (local Qwen 3.5)

Phase 1 target: local **Qwen 3.5 4B at Q4_K_M** (`qwen3.5:4b-q4_K_M`,
~3.3 GB) through the Ollama runtime (`http://127.0.0.1:11434`), text
generation only, non-streaming. The 9B model is intentionally out of
scope for this phase.

1. **Install Ollama** (Windows): `winget install Ollama.Ollama` or download from
   [ollama.com/download](https://ollama.com/download). The service then listens
   on the loopback address `http://127.0.0.1:11434`.

2. **Download and run the model** (the only model documented for this phase):

   ```powershell
   ollama pull qwen3.5:4b-q4_K_M
   ollama run qwen3.5:4b-q4_K_M
   ```

   Verify with `ollama list`. Type `/bye` to exit the interactive run.

3. **Configure `backend/.env`** (copy from `.env.example` first):

   ```text
   LLM_PROVIDER=ollama
   OLLAMA_BASE_URL=http://127.0.0.1:11434
   OLLAMA_MODEL=qwen3.5:4b-q4_K_M
   OLLAMA_TIMEOUT_SECONDS=120
   OLLAMA_NUM_CTX=4096
   ```

4. **Start the backend** (see *Backend Setup* above) and chat via
   `POST /api/chat`. If Ollama is unavailable the API returns a safe structured
   error instead of failing silently.

**Switching providers:** set `LLM_PROVIDER` to `fake` for the
deterministic offline default (no Ollama needed) or `gemini` for the
cloud provider (requires `GEMINI_API_KEY`), then restart the backend;
the extra Ollama variables can stay in place — they are only used when
`ollama` is selected.

Configuration is validated at startup:

- `OLLAMA_BASE_URL` must be a **loopback http(s) endpoint** — the backend
  rejects remote hosts so prompts can never be silently sent off the machine.
- `OLLAMA_TIMEOUT_SECONDS` is bounded (1.0–600.0), `OLLAMA_NUM_CTX` is bounded
  (256–32768).

**Thinking-mode safety:** Qwen 3.5 is a thinking-capable model. The
backend requests thinking off and additionally strips Ollama's
reasoning shapes (the separate `thinking` field and legacy inline
thinking markers) so only the final assistant text is ever returned.
An answer with no visible text after stripping becomes a structured
error, never internal reasoning.

**Hardware note (RTX 3050 6GB / 16GB RAM):** the 4B Q4_K_M weights
(~3.3 GB) are expected to fit largely in the 6 GB VRAM at a 4096-token
context, with CPU hybrid offload as the automatic fallback. Pose
latency/memory is **not yet benchmarked**, so the 4096 ctx is a starting
point, not a proven optimum.

#### Troubleshooting Ollama

| Symptom | Cause | Fix |
|---------|-------|-----|
| `502 Language model service is unavailable` | Ollama not running | Start the Ollama desktop app, or run `ollama serve`; check `http://127.0.0.1:11434` |
| `ollama pull` fails | network or name typo | Confirm the exact tag with `ollama pull qwen3.5:4b-q4_K_M` and retry |
| Model not found at chat time | pull incomplete | `ollama list` must show `qwen3.5:4b-q4_K_M` before selecting `LLM_PROVIDER=ollama` |
| `504 Language model request timed out` | first load / cold model | Keep `ollama run qwen3.5:4b-q4_K_M` warm or raise `OLLAMA_TIMEOUT_SECONDS` (max 600) |
| Slow responses | low VRAM, CPU offload | Lower `OLLAMA_NUM_CTX` (e.g. 2048) and retest; keep the backend on loopback |

## Status
**Phase 4 — Intelligent Orchestration (Milestones 4.1–4.2)**
The Main Orchestrator routes chat turns through reply / tool-action /
task-plan decisions, captures bounded validated task plans, and executes
steps only one at a time through the existing policy and approval
workflow. Backend foundation, LLM providers (fake / gemini / ollama),
and the policy–approval–execution stack are complete and covered by the
test suite.
