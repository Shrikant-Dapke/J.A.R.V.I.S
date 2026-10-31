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

## Status
**Phase 0 — Foundation**
