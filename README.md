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

## Status
**Phase 0 — Foundation**
