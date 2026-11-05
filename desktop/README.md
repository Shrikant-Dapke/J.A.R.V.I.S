# JARVIS Desktop (Phase 6.3 — Plans & Approval Center)

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
  untrusted caller frames, and payloads failing that channel's validator.
  Handler crashes collapse to a fixed, non-sensitive `UNEXPECTED` message;
  internal error text stays in the main-process console only.
- Chat message bounds (1–4000 chars) are enforced at every layer: composer
  (hard cap), IPC payload validator, and a final client-side wire check.
- Conversation identifiers are validated as backend uuids before they can
  appear in any request path (client guard, not just UI selection).

---

## IPC contract

`desktop/shared/ipc-contract.ts` is the single source of truth, consumed by
the registry, the preload, the renderer, and the tests:

| Method               | Channel                       | Payload                                      | Result wire type            |
| -------------------- | ----------------------------- | -------------------------------------------- | --------------------------- |
| `backendStatus`      | `jarvis:backend-status`       | `{}`                                         | `BackendStatus`             |
| `checkHealth`        | `jarvis:check-health`         | `{}`                                         | `HealthCheck`               |
| `getSystemInfo`      | `jarvis:get-system-info`      | `{}`                                         | `SystemInfoSummary`         |
| `sendChat`           | `jarvis:send-chat`            | `{ message, conversationId \| null }`        | `ChatExchange`              |
| `listConversations`  | `jarvis:list-conversations`   | `{ limit? ≤200, offset? }`                   | `ConversationListView`      |
| `getConversation`    | `jarvis:get-conversation`     | `{ conversationId }` (uuid)                  | `ConversationHistoryView`   |
| `deleteConversation` | `jarvis:delete-conversation`  | `{ conversationId }` (uuid)                  | `ConversationDeletedView`   |
| `listApprovals`      | `jarvis:list-approvals`       | `{ limit? ≤200, offset? }`                   | `ApprovalListView`          |
| `getApproval`        | `jarvis:get-approval`         | `{ approvalId }` (uuid)                      | `ApprovalRequestView`       |
| `approveApproval`    | `jarvis:approve-approval`     | `{ approvalId }` (uuid)                      | `ToolExecutionView`         |
| `rejectApproval`     | `jarvis:reject-approval`      | `{ approvalId }` (uuid)                      | `ApprovalRequestView`       |
| `listPlans`          | `jarvis:list-plans`           | `{ limit? ≤200, offset? }`                   | `PlanListView`              |
| `getPlan`            | `jarvis:get-plan`             | `{ planId }` (`^[a-zA-Z0-9_-]{1,64}$`)       | `PlanDetailView`            |
| `requestPlanStep`    | `jarvis:request-plan-step`    | `{ planId, stepId }` (patterned)             | `PlanStepRequestOutcomeView`|
| `resolvePlanRecovery`| `jarvis:resolve-plan-recovery`| `{ planId, stepId }` (patterned)             | `PlanRecoveryResolvedView`  |

Every channel declares a validator in `PAYLOAD_VALIDATORS`; the registry
rejects anything the validator refuses (`BAD_PAYLOAD`) before a handler runs.

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

Tests (93) run in plain Node against the compiled **pure** modules — no
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

Phase 6.2 scope was honest by design: the Approvals and Plans surfaces were
placeholders until Phase 6.3 wired them. The chat console remains untouched.

## Chat console (Phase 6.2, fully functional)

The Console view is a complete AI conversation interface over the typed IPC
bridge:

- **Conversation lifecycle** — rail of past conversations (metadata only:
  time, turn count, id), New Chat, refresh, delete with inline confirmation.
  Switching or deleting is locked while a reply is in flight.
- **Chat UX** — asymmetric bubbles with timestamps, multiline composer
  (Enter sends, Shift+Enter newline, IME-safe), hard 4000-char counter with a
  near-limit warning, auto-resizing input, stick-to-bottom auto-scroll that
  yields when the user scrolls up, welcome screen with example prompts.
- **Honest request lifecycle** — optimistic pending user bubble → thinking
  indicator → real assistant turn (never a mock or simulated stream). Failed
  sends keep the bubble, mark it visibly failed, and restore the exact text
  to the composer: the user's message is never lost. Submissions are
  serialized by a gate (`shared/chat-engine.ts`); there is no automatic
  retry anywhere — a failed turn stays failed until the user resends.
- **Backend statuses rendered structurally** — `action_proposed` (proposal
  card: tool, risk level, approval state, bounded arguments, expiry) and
  `plan_proposed` (step list with statuses/dependencies), plus executed
  read-only tool results. Nothing executes from the chat surface; execution
  belongs to the backend approval/plan flows.
- **Error surfaces** — chat-specific copy per `IpcErrorCode` (policy denial,
  provider outage, capacity, validation, timeout, unreachable) composed from
  the backend's structured `error.message` + `HTTP_STATUS_HINTS` meaning.
- **Pure core** — `shared/chat-engine.ts` holds the reducer (session
  transitions), the submission gate, and message factories with zero
  React/Electron imports so all chat invariants are plain-Node testable
  (`tests/chat-engine.test.ts`); the React hooks only wire the engine to
  state and the bridge.

Chat sends are blocked with an explicit banner when the token is not
configured (`tokenConfigured === false` from the main-process status).

## Approvals center (Phase 6.3, fully functional)

The Approvals view is a complete decision workspace over the typed IPC bridge:

- **Inventory rail** — `GET /api/approvals` metadata page (newest first). Rows
  show tool, risk badge, lifecycle status, creation/resolution time and the
  description only — arguments are structurally absent from the listing.
- **Authoritative detail** — selecting a row always fetches
  `GET /api/approvals/{id}` (the endpoint that also applies lazy expiration),
  so what the user sees is the backend's current truth: exact bound
  arguments (read-only), risk explanation, TTL countdown (`expiresAt`),
  terminal states.
- **Decisive actions** — Approve/Reject behind explicit in-panel
  confirmation dialogs. Before a decisive POST, the exact approval is
  **re-read authoritatively**; the POST goes out only on a fresh read and
  then exactly **once** — never retried. Duplicate clicks are refused by an
  in-flight lock plus disabled controls.
- **Outcome honesty** — an approved execution renders the backend's
  ToolResult verbatim (status, message, bounded data map, error code) with
  the single-use receipt; rejects render the authoritative REJECTED state.
  404/409 conflicts surface the backend message + status meaning and
  converge by re-reading; terminal states disable the action panel.

## Plans center (Phase 6.3, fully functional)

The Plans view is a complete manual-execution workspace over the typed IPC
bridge:

- **Overview rail** — `GET /api/plans` metadata page with backend attention
  flags (`needs_attention`, recovery/awaiting counts, step counts).
- **Authoritative plan detail** — `GET /api/plans/{id}` rendered verbatim:
  `overall_status` banner (backend-derived), `ready_step_ids` ready
  indicators, dependency lists, bound arguments, recorded results. The
  client **never** computes step eligibility — the backend owns readiness.
- **Step requests** — one confirmed click = exactly one POST
  `.../steps/{step}/request`. A read-only step answers 200 with a verbatim
  ToolResult; a mutating step answers 202 with the created pending approval
  (nothing executed) surfaced as an "approval required" card pointing to the
  Approvals workspace. After any attempt the plan is re-read so the UI
  converges to persisted state.
- **Recovery reconciliation** — RECOVERY_REQUIRED steps expose an explicit
  acknowledge flow that states the uncertainty honestly: the backend
  finalizes the step FAILED (`execution_uncertain`), the side effect must be
  verified manually, and dependents stay blocked.

## Phase 6.4 backlog (not in this build)

- Packaging (electron-builder), code signing, auto-update policy.
