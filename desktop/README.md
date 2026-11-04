# JARVIS Desktop (Phase 6.1 — Foundation)

Electron desktop shell for J.A.R.V.I.S. The renderer has **no network access**:
every backend call runs in the Electron main process over a narrow, typed IPC
bridge. Authentication never crosses the bridge.

Stack: Electron 33 · React 18 · TypeScript 5.6 · Vite 5 · Tailwind CSS 3.

---

## Setup

Prerequisites: Node ≥ 20, npm ≥ 10. The backend must be importable from the
repository root for contract generation (`backend/app/main.py`).

```powershell
cd desktop
npm install

# Binary postinstalls (electron, esbuild) require one-time approval
npm install-scripts approve electron esbuild
npm rebuild electron esbuild

# Regenerate the committed OpenAPI snapshot + typed client (read-only import
# of backend/app/main via the backend venv; no server, no backend edits)
npm run generate:api
```

### Configuration

The main process resolves, in order:

| Setting  | Sources                                                            | Default       |
| -------- | ------------------------------------------------------------------ | ------------- |
| Token    | `JARVIS_API_TOKEN` env → `API_TOKEN` env → `backend/.env`           | (none)        |
| Port     | `JARVIS_BACKEND_PORT` env → `backend/.env` `PORT`                   | `8000`        |
| Host     | `JARVIS_BACKEND_HOST` env → `backend/.env` `HOST`                   | `127.0.0.1`   |
| Devtools | `JARVIS_DEVTOOLS=1` (development builds only)                       | off           |

Fail-closed rules mirror the backend (`app/core/config.py`,
`app/core/security.py`): the host must be loopback (`127.0.0.0/8`, `::1`,
`localhost`), and a token shorter than 32 characters aborts startup. With no
token configured, unauthenticated routes (health) still work and protected
routes report `AUTH_NOT_CONFIGURED` honestly.

### Development

```powershell
npm run dev:renderer   # Vite dev server on http://127.0.0.1:5173 (strict port)
npm run dev            # Electron against the dev server (second terminal)
```

### Production / dist hosting

```powershell
npm run build          # build:main (tsc) + build:renderer (vite → dist/renderer)
npm start              # electron . — loads dist/renderer/index.html on file:
```

---

## Security model

Invariants, enforced by construction and pinned by contract tests (`desktop/tests/security.test.ts`):

- `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`,
  `webSecurity: true` for every webContents.
- Navigation is allowlisted: the Vite dev origin in development, or the built
  `index.html` on `file:` in dist hosting. Everything else is `preventDefault`-ed.
- `window.open` and every permission request/check are denied.
- Strict production CSP in `index.html` (`connect-src 'self'`, no remote
  origins); the dev-server plugin relaxes `connect-src` only in `serve` mode.
- The preload bridge exposes exactly the methods in `PRELOAD_METHODS`
  (shared contract) — never raw `ipcRenderer`, never Node.
- **Auth stays in the main process**: the renderer sees a
  `tokenConfigured` boolean and a masked session identifier only. The bearer
  token and full `X-JARVIS-Session` value are attached inside
  `electron/main/api/client.ts` and never serialized across the bridge.
- Session identifiers are generated per process (64 hex chars) and match the
  backend pattern `^[A-Za-z0-9_-]{16,128}$` (tested as a mirror of
  `app/core/security.py`).
- Backend communication is loopback-only, validated client-side
  (`electron/main/net.ts`) before any socket is opened. No `Origin` header is
  sent (native client semantics expected by `require_safe_origin`).
- IPC dispatch (pure registry, plain-Node testable) rejects: unknown channels,
  untrusted caller frames, non-empty payloads. Handler crashes collapse to a
  fixed, non-sensitive `UNEXPECTED` message; internal error text stays in the
  main-process console only.

---

## IPC contract

`desktop/shared/ipc-contract.ts` is the single source of truth, consumed by
the registry, the preload, the renderer, and the tests:

| Method          | Channel                    | Payload | Result wire type        |
| --------------- | -------------------------- | ------- | ----------------------- |
| `backendStatus` | `jarvis:backend-status`    | `{}`    | `BackendStatus`         |
| `checkHealth`   | `jarvis:check-health`      | `{}`    | `HealthCheck`           |
| `getSystemInfo` | `jarvis:get-system-info`   | `{}`    | `SystemInfoSummary`     |

All results share the `IpcResult<T>` envelope (`{ ok: true, data }` or
`{ ok: false, code, message, httpStatus? }`) with the error-code table
`IPC_ERROR_COPY` for renderer copy.

Wire types are narrowed at the boundary: the client validates raw response
shapes (health snapshot, backend `ToolResult`) before anything reaches the
renderer, and projects snake_case backend fields into camelCase UI types.

API types are generated from the committed backend snapshot
`desktop/openapi.json` (15 paths) into
`desktop/electron/main/api/schema.gen.ts` via `npm run generate:api`
(openapi-typescript). The snapshot is contract-pinned by
`desktop/tests/schema.test.ts` (exact path set, auth headers per protected
route, unauthenticated health/root, schema presence).

---

## Scripts

| Script                 | Purpose                                                        |
| ---------------------- | -------------------------------------------------------------- |
| `generate:api`         | Re-dump OpenAPI from backend import + regenerate typed module   |
| `dev:renderer` / `dev` | Vite dev server / Electron against it                          |
| `start`                | Run Electron against the built dist                             |
| `typecheck`            | `main` + `node` + `renderer` tsc projects                       |
| `build`                | `build:main` (tsc) + `build:renderer` (vite)                    |
| `test` 🔎              | compile pure modules → `node --test` contract suite             |
| `verify`               | typecheck + test + build (the acceptance gate)                  |

Tests (36) run in plain Node against the compiled **pure** modules — no
Electron runtime needed. Electron glue (`main.ts`, `handlers.ts`, preload) is
kept thin on purpose so everything security-relevant is testable.

---

## UI foundation

Dark futuristic HUD shell (`src/renderer`):

- `App.tsx` — sidebar navigation (System / Console / Approvals / Plans),
  responsive mobile drawer, scrollable workspace, toggleable right context
  panel (live status pills, session keys, contract reference).
- Design system in `styles.css` + `tailwind.config.js` (void/abyss/hull
  surfaces, cyan/violet accents, glow, grid backdrop, HUD scrollbars).
- Reusable kit: `components/primitives.tsx`, `controls.tsx` (buttons require
  explicit `onClick` + `disabled` — no dead buttons), `states.tsx`,
  `icons.tsx`.
- Live data: `useBackendMonitor` (status + health, 30 s poll, manual refresh)
  and `useSystemInfo` (auth-gated `get_system_info` tool read).

Phase 6.1 scope is honest by design: Console, Approvals, and Plans surfaces
are explicit placeholders that state their Phase 6.2 contracts — no fake
data, no streaming, no voice, no non-functional controls.

---

## Phase 6.2 backlog (not in this build)

- Chat turn pipeline over `POST /api/chat` (success | action_proposed |
  plan_proposed), bounded message/replay envelopes.
- Approval workspace: inventory (advisory) → authoritative detail reads →
  approve/reject single-use, TTL-bound actions; unchanged backend policy.
- Plans workspace: authoritative plan/steps, one-step-per-request execution,
  recovery reconciliation.
- Conversations inventory + clear.
- Packaging (electron-builder), code signing, auto-update policy.
