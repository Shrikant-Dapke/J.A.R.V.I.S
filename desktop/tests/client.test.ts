/** API client contract tests (headers, loopback, failure mapping). */

import test from "node:test";
import assert from "node:assert/strict";
import {
  buildRequestHeaders,
  HttpBackendApiClient,
  isHealthSnapshot,
  isSystemInfoResult,
} from "../electron/main/api/client";
import type { SystemInfoSummary } from "../shared/ipc-contract";

const HEALTH_OK = { name: "JARVIS", status: "healthy", version: "0.1.0" };
const SYSTEMINFO_OK: SystemInfoSummary = {
  toolName: "get_system_info",
  status: "success",
  success: true,
  message: "System information retrieved successfully.",
  data: null,
  timestamp: "2099-01-01T00:00:00.000Z",
};

test("headers follow the backend auth contract exactly", () => {
  const withToken = buildRequestHeaders("SECRET-TOKEN-VALUE", "sess", "req-1");
  assert.equal(withToken.Authorization, "Bearer SECRET-TOKEN-VALUE");
  assert.equal(withToken["X-JARVIS-Session"], "sess");
  assert.equal(withToken["X-Request-ID"], "req-1");
  assert.equal(withToken.Accept, "application/json");

  const withoutToken = buildRequestHeaders(null, "sess", "req-2");
  assert.equal(withoutToken.Authorization, undefined);
  assert.equal(withoutToken["X-JARVIS-Session"], "sess");
  assert.equal(withoutToken["Origin"], undefined, "native clients send no Origin");
});

test("non-loopback API origins cannot be constructed", () => {
  for (const bad of ["http://example.com", "http://0.0.0.0:8000", "http://192.168.1.14:8000"]) {
    assert.throws(
      () => new HttpBackendApiClient({ baseUrl: new URL(bad), token: null, sessionId: "s" }),
      /loopback/,
    );
  }
  // Loopback origins of all supported spellings construct fine.
  for (const good of ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"]) {
    assert.doesNotThrow(() => new HttpBackendApiClient({ baseUrl: new URL(good), token: null, sessionId: "s" }));
  }
});

test("connection failures map to BACKEND_UNREACHABLE", async () => {
  const client = new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:9"),
    token: null,
    sessionId: "s",
  });
  const result = await client.getHealth(2000);
  assert.equal(result.ok, false);
  assert.equal(result.ok ? null : result.code, "BACKEND_UNREACHABLE");
});

test("aborted requests map to TIMEOUT, not failures", async () => {
  const hangingFetch = (_url: unknown, init?: { signal?: AbortSignal }) =>
    new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => {
        const abortError = new Error("aborted");
        abortError.name = "AbortError";
        reject(abortError);
      });
    });
  const client = new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:8000"),
    token: null,
    sessionId: "s",
    fetchImpl: hangingFetch as typeof fetch,
  });
  const result = await client.getHealth(20);
  assert.equal(result.ok, false);
  assert.equal(result.ok ? null : result.code, "TIMEOUT");
});

test("non-2xx responses surface HTTP_STATUS with the backend message", async () => {
  const stubFetch = () =>
    Promise.resolve(
      new Response(JSON.stringify({ error: { message: "Authentication is required." } }), {
        status: 401,
      }),
    );
  const client = new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:8000"),
    token: "t".repeat(40),
    sessionId: "s",
    fetchImpl: stubFetch as typeof fetch,
  });
  const result = await client.getHealth();
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "HTTP_STATUS");
    assert.equal(result.httpStatus, 401);
    assert.equal(result.message, "Authentication is required.");
  }
});

test("health responses are validated and latency measured", async () => {
  const stubFetch = () => Promise.resolve(new Response(JSON.stringify(HEALTH_OK), { status: 200 }));
  const client = new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:8000"),
    token: null,
    sessionId: "s",
    fetchImpl: stubFetch as typeof fetch,
    requestId: () => "fixed-req",
    now: (() => {
      let tick = 0;
      return () => 1000 + (tick += 37);
    })(),
  });
  const result = await client.getHealth();
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.deepEqual(result.data.snapshot, HEALTH_OK);
    assert.equal(result.data.latencyMs, 37);
  }
});

test("malformed payloads fail closed as UNEXPECTED, never render", async () => {
  const stubFetch = () =>
    Promise.resolve(new Response(JSON.stringify({ nonsense: true }), { status: 200 }));
  const client = new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:8000"),
    token: null,
    sessionId: "s",
    fetchImpl: stubFetch as typeof fetch,
  });
  const result = await client.getHealth();
  assert.equal(result.ok, false);
  assert.equal(result.ok ? null : result.code, "UNEXPECTED");
});

test("system-info requires a configured token before any network call", async () => {
  const client = new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:8000"),
    token: null,
    sessionId: "s",
  });
  const result = await client.getSystemInfo();
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "AUTH_NOT_CONFIGURED");
    assert.match(result.message, /JARVIS_API_TOKEN/);
  }
});

test("system-info responses are narrowed into the UI projection", async () => {
  const wire = {
    tool_name: "get_system_info",
    status: "success",
    success: true,
    message: "ok",
    data: { os: "Windows", ram: "16 GB" },
    timestamp: "2099-01-01T00:00:00.000Z",
  };
  const stubFetch = () => Promise.resolve(new Response(JSON.stringify(wire), { status: 200 }));
  const client = new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:8000"),
    token: "t".repeat(40),
    sessionId: "s",
    fetchImpl: stubFetch as typeof fetch,
  });
  const result = await client.getSystemInfo();
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.deepEqual(result.data, {
      toolName: "get_system_info",
      status: "success",
      success: true,
      message: "ok",
      data: { os: "Windows", ram: "16 GB" },
      timestamp: "2099-01-01T00:00:00.000Z",
    });
  }
});

test("shape guards defend the contract boundary", () => {
  assert.equal(isHealthSnapshot(HEALTH_OK), true);
  assert.equal(isHealthSnapshot({ name: 1, status: "x", version: "y" }), false);
  assert.equal(isHealthSnapshot(null), false);
  assert.equal(isSystemInfoResult({ ...SYSTEMINFO_OK, toolName: undefined }), false);
  const wire = {
    tool_name: "get_system_info",
    status: "failure",
    success: false,
    message: "boom",
    data: null,
    timestamp: "2099-01-01T00:00:00.000Z",
  };
  assert.equal(isSystemInfoResult(wire), true);
  const oversized: Record<string, string> = {};
  for (let index = 0; index < 40; index += 1) oversized[`k${index}`] = "v";
  assert.equal(isSystemInfoResult({ ...wire, data: oversized }), false, ">32 items rejected");
});

/* ------------------------------------------------------------------ */
/* Phase 6.2: chat + conversation client contracts                     */
/* ------------------------------------------------------------------ */

import {
  CLIENT_ERROR_CODES,
  isChatResponseWire,
  isConversationClearWire,
  isConversationHistoryWire,
  isConversationListWire,
} from "../electron/main/api/client";
import {
  CHAT_MESSAGE_MAX_LENGTH,
  CONVERSATION_ID_PATTERN,
  type ChatExchange,
  type ConversationDeletedView,
  type ConversationHistoryView,
  type ConversationListView,
} from "../shared/ipc-contract";

const TOKEN = "t".repeat(40);
const CONVERSATION_ID = "123e4567-e89b-12d3-a456-426614174000";

function recordingFetch(
  responder: (url: URL, init: RequestInit) => Response | Promise<Response>,
): { calls: Array<{ url: URL; init: RequestInit }>; fetchImpl: typeof fetch } {
  const calls: Array<{ url: URL; init: RequestInit }> = [];
  const fetchImpl = ((url: URL | string, init?: RequestInit) => {
    const record = {
      url: url instanceof URL ? url : new URL(String(url)),
      init: init ?? {},
    };
    calls.push(record);
    return Promise.resolve(responder(record.url, record.init));
  }) as typeof fetch;
  return { calls, fetchImpl };
}

function makeClient(fetchImpl: typeof fetch, token: string | null = TOKEN) {
  return new HttpBackendApiClient({
    baseUrl: new URL("http://127.0.0.1:8000"),
    token,
    sessionId: "session-header-value",
    fetchImpl,
    requestId: () => "fixed-req",
    now: () => 1_700_000_000_000,
  });
}

const CHAT_WIRE_PLAIN = {
  conversation_id: CONVERSATION_ID,
  response: "All systems nominal.",
  status: "success",
  timestamp: "2099-01-01T00:00:00.000Z",
};

const CHAT_WIRE_ACTION = {
  conversation_id: CONVERSATION_ID,
  response: "I can create that directory.",
  status: "action_proposed",
  action: {
    approval_id: "apr-1",
    tool_name: "create_directory",
    arguments: { path: "notes" },
    action_description: "Create the notes directory.",
    risk_level: "LOW_RISK",
    approval_status: "PENDING",
    expires_at: "2099-01-01T01:00:00.000Z",
  },
};

const CHAT_WIRE_PLAN = {
  conversation_id: CONVERSATION_ID,
  response: "Here is the plan.",
  status: "plan_proposed",
  plan: {
    plan_id: "plan-1",
    goal: "Organize project notes.",
    steps: [
      {
        step_id: "s1",
        description: "Create directory.",
        tool_name: "create_directory",
        status: "PENDING",
        depends_on: [],
        approval_id: "apr-2",
      },
      { step_id: "s2", description: "Write README.", tool_name: "write_file" },
    ],
  },
};

test("chat success narrows the wire payload into the UI projection", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () => new Response(JSON.stringify(CHAT_WIRE_PLAIN), { status: 200 }),
  );
  const client = makeClient(fetchImpl);
  const result = await client.sendChat({ message: "status?", conversationId: null });
  assert.equal(result.ok, true);
  if (result.ok) {
    const exchange: ChatExchange = result.data;
    assert.equal(exchange.conversationId, CONVERSATION_ID);
    assert.equal(exchange.status, "success");
    assert.equal(exchange.reply, "All systems nominal.");
    assert.equal(exchange.occurredAt, "2099-01-01T00:00:00.000Z");
    assert.equal(exchange.action, null);
    assert.equal(exchange.toolResult, null);
    assert.equal(exchange.plan, null);
  }
  // POST wire contract: URL, method, headers, JSON body (no conversation_id).
  assert.equal(calls.length, 1);
  const { url, init } = calls[0];
  assert.equal(url.pathname, "/api/chat");
  assert.equal(init.method, "POST");
  assert.deepEqual(JSON.parse(String(init.body)), { message: "status?" });
  const headers = init.headers as Record<string, string>;
  assert.equal(headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(headers["X-JARVIS-Session"], "session-header-value");
  assert.equal(headers["Content-Type"], "application/json");
});

test("chat carries conversation_id for existing conversations", async () => {
  let body: unknown = null;
  const rec = recordingFetch((_url, init) => {
    body = JSON.parse(String(init.body));
    return new Response(JSON.stringify(CHAT_WIRE_PLAIN), { status: 200 });
  });
  const client = makeClient(
    rec.fetchImpl,
  );
  await client.sendChat({ message: "again", conversationId: CONVERSATION_ID });
  assert.deepEqual(body, { message: "again", conversation_id: CONVERSATION_ID });
});

test("action_proposed narrowings include risk, approval state, and args", async () => {
  const client = makeClient(
    recordingFetch(() => new Response(JSON.stringify(CHAT_WIRE_ACTION), { status: 200 })).fetchImpl,
  );
  const result = await client.sendChat({ message: "mkdir notes", conversationId: null });
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.equal(result.data.status, "action_proposed");
    assert.notEqual(result.data.action, null);
    assert.deepEqual(result.data.action, {
      approvalId: "apr-1",
      toolName: "create_directory",
      arguments: { path: "notes" },
      description: "Create the notes directory.",
      riskLevel: "LOW_RISK",
      approvalStatus: "PENDING",
      expiresAt: "2099-01-01T01:00:00.000Z",
    });
  }
});

test("plan_proposed narrowings default missing step fields", async () => {
  const client = makeClient(
    recordingFetch(() => new Response(JSON.stringify(CHAT_WIRE_PLAN), { status: 200 })).fetchImpl,
  );
  const result = await client.sendChat({ message: "plan", conversationId: null });
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.equal(result.data.status, "plan_proposed");
    const plan = result.data.plan;
    assert.equal(plan?.planId, "plan-1");
    assert.equal(plan?.steps.length, 2);
    assert.deepEqual(plan?.steps[0], {
      stepId: "s1",
      description: "Create directory.",
      toolName: "create_directory",
      status: "PENDING",
      dependsOn: [],
      approvalId: "apr-2",
    });
    // Missing optional fields collapse to defaults, not undefined.
    assert.deepEqual(plan?.steps[1], {
      stepId: "s2",
      description: "Write README.",
      toolName: "write_file",
      status: "PENDING",
      dependsOn: [],
      approvalId: null,
    });
  }
});

test("chat fills an absent backend timestamp with the local clock", async () => {
  const withoutTs = { ...CHAT_WIRE_PLAIN };
  delete (withoutTs as Record<string, unknown>)["timestamp"];
  const client = makeClient(
    recordingFetch(() => new Response(JSON.stringify(withoutTs), { status: 200 })).fetchImpl,
  );
  const result = await client.sendChat({ message: "hi", conversationId: null });
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.equal(result.data.occurredAt, new Date(1_700_000_000_000).toISOString());
  }
});

test("oversized chat messages fail closed before any network call", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () => new Response(JSON.stringify(CHAT_WIRE_PLAIN), { status: 200 }),
  );
  const client = makeClient(fetchImpl);
  const result = await client.sendChat({
    message: "x".repeat(CHAT_MESSAGE_MAX_LENGTH + 1),
    conversationId: null,
  });
  assert.equal(result.ok, false);
  assert.equal(result.ok ? null : result.code, "UNEXPECTED");
  assert.equal(calls.length, 0, "no request may leave the process");
});

test("every backend HTTP error status reaches the renderer with its meaning", async () => {
  for (const status of [403, 404, 422, 500, 502, 503, 504]) {
    const client = makeClient(
      recordingFetch(() =>
        new Response(
          JSON.stringify({ error: { message: `policy failure ${status} details` } }),
          { status },
        ),
      ).fetchImpl,
    );
    const result = await client.sendChat({ message: "hi", conversationId: null });
    assert.equal(result.ok, false, `status ${status}`);
    if (!result.ok) {
      assert.equal(result.code, "HTTP_STATUS");
      assert.equal(result.httpStatus, status);
      assert.equal(result.message, `policy failure ${status} details`);
    }
  }
});

test("chat makes exactly one attempt — never retried, even on failure", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () => new Response(JSON.stringify({ error: { message: "provider down" } }), { status: 502 }),
  );
  const client = makeClient(fetchImpl);
  const result = await client.sendChat({ message: "hi", conversationId: null });
  assert.equal(result.ok, false);
  assert.equal(calls.length, 1, "a failed chat must not be re-attempted");
});

test("all new chat endpoints require the token before any network call", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () => new Response(JSON.stringify(CHAT_WIRE_PLAIN), { status: 200 }),
  );
  const client = makeClient(fetchImpl, null);
  const [chat, list, history, deleted] = await Promise.all([
    client.sendChat({ message: "hi", conversationId: null }),
    client.listConversations(),
    client.getConversation({ conversationId: CONVERSATION_ID }),
    client.deleteConversation({ conversationId: CONVERSATION_ID }),
  ]);
  for (const result of [chat, list, history, deleted]) {
    assert.equal(result.ok, false);
    assert.equal(result.ok ? null : result.code, "AUTH_NOT_CONFIGURED");
  }
  assert.equal(calls.length, 0, "auth gates run before any request");
});

test("conversation list narrows metadata and builds the query contract", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () =>
      new Response(
        JSON.stringify({
          conversations: [
            {
              conversation_id: CONVERSATION_ID,
              created_at: "2099-01-01T00:00:00.000Z",
              updated_at: "2099-01-01T01:00:00.000Z",
              turn_count: 4,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
        }),
        { status: 200 },
      ),
  );
  const client = makeClient(fetchImpl);
  const result = await client.listConversations();
  assert.equal(result.ok, true);
  if (result.ok) {
    const view: ConversationListView = result.data;
    assert.equal(view.total, 1);
    assert.deepEqual(view.conversations, [
      {
        conversationId: CONVERSATION_ID,
        createdAt: "2099-01-01T00:00:00.000Z",
        updatedAt: "2099-01-01T01:00:00.000Z",
        turnCount: 4,
      },
    ]);
  }
  assert.equal(calls.length, 1);
  const { url } = calls[0];
  assert.equal(url.pathname, "/api/conversations");
  assert.equal(url.searchParams.get("limit"), "50");
  assert.equal(url.searchParams.get("offset"), "0");
});

test("conversation history widens to the backend ceiling and narrows turns", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () =>
      new Response(
        JSON.stringify({
          conversation_id: CONVERSATION_ID,
          turns: [
            { role: "user", content: "hello", timestamp: "2099-01-01T00:00:00.000Z" },
            { role: "assistant", content: "hi", timestamp: "2099-01-01T00:00:01.000Z" },
          ],
          turn_count: 2,
          total_turn_count: 500,
        }),
        { status: 200 },
      ),
  );
  const client = makeClient(fetchImpl);
  const result = await client.getConversation({ conversationId: CONVERSATION_ID });
  assert.equal(result.ok, true);
  if (result.ok) {
    const history: ConversationHistoryView = result.data;
    assert.equal(history.conversationId, CONVERSATION_ID);
    assert.equal(history.turnCount, 2);
    assert.equal(history.totalTurnCount, 500);
    assert.equal(history.turns[0].role, "user");
    assert.equal(history.turns[1].content, "hi");
  }
  assert.equal(calls.length, 1);
  const { url } = calls[0];
  assert.equal(url.pathname, `/api/conversations/${CONVERSATION_ID}`);
  assert.equal(url.searchParams.get("limit"), "400");
});

test("conversation delete builds the DELETE contract and narrows the receipt", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () =>
      new Response(
        JSON.stringify({ conversation_id: CONVERSATION_ID, status: "deleted" }),
        { status: 200 },
      ),
  );
  const client = makeClient(fetchImpl);
  const result = await client.deleteConversation({ conversationId: CONVERSATION_ID });
  assert.equal(result.ok, true);
  if (result.ok) {
    const receipt: ConversationDeletedView = result.data;
    assert.equal(receipt.conversationId, CONVERSATION_ID);
  }
  assert.equal(calls.length, 1);
  const { url, init } = calls[0];
  assert.equal(url.pathname, `/api/conversations/${CONVERSATION_ID}`);
  assert.equal(init.method, "DELETE");
  assert.equal(init.body, undefined);
});

test("malformed conversation identifiers are refused before any network call", async () => {
  const { calls, fetchImpl } = recordingFetch(
    () => new Response(JSON.stringify(CHAT_WIRE_PLAIN), { status: 200 }),
  );
  const client = makeClient(fetchImpl);
  for (const bad of ["", "not-a-uuid", "../../etc/passwd"]) {
    assert.equal(CONVERSATION_ID_PATTERN.test(bad), false);
    const history = await client.getConversation({ conversationId: bad });
    assert.equal(history.ok, false, `history ${bad}`);
    assert.equal(history.ok ? null : history.code, "UNEXPECTED");
    const deleted = await client.deleteConversation({ conversationId: bad });
    assert.equal(deleted.ok, false, `delete ${bad}`);
    assert.equal(deleted.ok ? null : deleted.code, "UNEXPECTED");
  }
  assert.equal(calls.length, 0);
});

test("conversation 404s surface the backend message", async () => {
  const client = makeClient(
    recordingFetch(() =>
      new Response(JSON.stringify({ error: { message: "Conversation not found." } }), {
        status: 404,
      }),
    ).fetchImpl,
  );
  const result = await client.getConversation({ conversationId: CONVERSATION_ID });
  assert.equal(result.ok, false);
  if (!result.ok) {
    assert.equal(result.code, "HTTP_STATUS");
    assert.equal(result.httpStatus, 404);
    assert.equal(result.message, "Conversation not found.");
  }
});

test("wire guards defend the chat + conversation contract boundary", () => {
  assert.equal(isChatResponseWire(CHAT_WIRE_PLAIN), true);
  assert.equal(isChatResponseWire(CHAT_WIRE_ACTION), true);
  assert.equal(isChatResponseWire(CHAT_WIRE_PLAN), true);
  assert.equal(isChatResponseWire({ ...CHAT_WIRE_PLAIN, status: "streaming" }), false);
  assert.equal(isChatResponseWire({ ...CHAT_WIRE_PLAIN, response: 42 }), false);
  assert.equal(
    isChatResponseWire({ ...CHAT_WIRE_PLAIN, response: "x".repeat(20_001) }),
    false,
    "pathological text sizes are rejected",
  );
  assert.equal(
    isChatResponseWire({ ...CHAT_WIRE_ACTION, action: { ...CHAT_WIRE_ACTION.action, risk_level: "MEDIUM" } }),
    false,
  );
  assert.equal(
    isChatResponseWire({ ...CHAT_WIRE_PLAN, plan: { ...CHAT_WIRE_PLAN.plan, steps: [] } }),
    true,
    "backend allows a zero-step plan payload",
  );

  assert.equal(
    isConversationListWire({
      conversations: [],
      total: 0,
      limit: 50,
      offset: 0,
    }),
    true,
  );
  assert.equal(isConversationListWire({ conversations: [], total: -1, limit: 50, offset: 0 }), false);
  assert.equal(
    isConversationListWire({
      conversations: [{ conversation_id: "x", created_at: "t", updated_at: "t", turn_count: 0 }],
      total: 1,
      limit: 50,
      offset: 0,
    }),
    false,
    "non-uuid conversation ids rejected",
  );

  assert.equal(
    isConversationHistoryWire({
      conversation_id: CONVERSATION_ID,
      turns: [],
      turn_count: 0,
      total_turn_count: 0,
    }),
    true,
  );
  assert.equal(
    isConversationHistoryWire({
      conversation_id: CONVERSATION_ID,
      turns: [{ role: "system", content: "x", timestamp: "t" }],
      turn_count: 1,
      total_turn_count: 1,
    }),
    false,
  );

  assert.equal(isConversationClearWire({ conversation_id: CONVERSATION_ID, status: "deleted" }), true);
  assert.equal(isConversationClearWire({ conversation_id: CONVERSATION_ID }), true);
  assert.equal(
    isConversationClearWire({ conversation_id: CONVERSATION_ID, status: "archived" }),
    false,
  );
});

test("client error envelope stays at the documented closed set", () => {
  assert.deepEqual([...CLIENT_ERROR_CODES].sort(), [
    "AUTH_NOT_CONFIGURED",
    "BACKEND_UNREACHABLE",
    "HTTP_STATUS",
    "TIMEOUT",
    "UNEXPECTED",
  ]);
});
