/**
 * Pure chat-engine tests: reducer transitions, serialization gate, and
 * projection helpers. These rules are the chat contract's core — every
 * UI behavior depends on exactly these transitions.
 */

import test from "node:test";
import assert from "node:assert/strict";
import {
  createChatGate,
  createChatMessageId,
  exchangeToAssistantMessage,
  historyTurnsToMessages,
  reduceChatSession,
  runExclusive,
  EMPTY_CHAT_SESSION,
  type ChatAction,
  type ChatSessionState,
} from "../shared/chat-engine";
import type { ChatExchange, ConversationTurnView } from "../shared/ipc-contract";

function sendStart(id = "u1"): ChatAction {
  return {
    type: "send-start",
    userMessage: {
      id,
      role: "user",
      content: "hello",
      createdAt: "2099-01-01T00:00:00.000Z",
      kind: "pending",
      proposal: null,
      failure: null,
    },
  };
}

const USER_TURN: ConversationTurnView = {
  role: "user",
  content: "who are you?",
  timestamp: "2099-01-01T00:00:00.000Z",
};
const ASSISTANT_TURN: ConversationTurnView = {
  role: "assistant",
  content: "JARVIS.",
  timestamp: "2099-01-01T00:00:01.000Z",
};

const PLAIN_EXCHANGE: ChatExchange = {
  conversationId: "123e4567-e89b-12d3-a456-426614174000",
  status: "success",
  reply: "Hello there.",
  occurredAt: "2099-01-01T00:00:02.000Z",
  action: null,
  toolResult: null,
  plan: null,
};

test("welcome state starts fresh and new-chat resets everything", () => {
  assert.deepEqual(EMPTY_CHAT_SESSION, {
    conversationId: null,
    loading: false,
    loadError: null,
    messages: [],
    sending: false,
    truncated: false,
  });
  let state: ChatSessionState = { ...EMPTY_CHAT_SESSION };
  for (const action of [
    sendStart(),
    {
      type: "send-success",
      userMessageId: "u1",
      assistantMessage: exchangeToAssistantMessage(PLAIN_EXCHANGE, () => "a1"),
      conversationId: PLAIN_EXCHANGE.conversationId,
    } satisfies ChatAction,
  ]) {
    state = reduceChatSession(state, action);
  }
  const reset = reduceChatSession(state, { type: "new-chat" });
  assert.deepEqual(reset, EMPTY_CHAT_SESSION);
});

test("conversation switching goes loading → ready and marks truncation", () => {
  let state = reduceChatSession(EMPTY_CHAT_SESSION, {
    type: "history-loading",
    conversationId: "123e4567-e89b-12d3-a456-426614174000",
  });
  assert.equal(state.loading, true);
  assert.equal(state.conversationId, "123e4567-e89b-12d3-a456-426614174000");

  state = reduceChatSession(state, {
    type: "history-ready",
    conversationId: "123e4567-e89b-12d3-a456-426614174000",
    messages: historyTurnsToMessages([USER_TURN, ASSISTANT_TURN], () => "m"),
    totalTurnCount: 20,
  });
  assert.equal(state.loading, false);
  assert.equal(state.messages.length, 2);
  assert.equal(state.truncated, true, "turn_count 20 > shown 2 means truncated");
  assert.equal(state.messages[0].content, "who are you?");
  assert.equal(state.messages[1].content, "JARVIS.");

  const untruncated = reduceChatSession(
    { ...state, truncated: true },
    {
      type: "history-ready",
      conversationId: "123e4567-e89b-12d3-a456-426614174000",
      messages: state.messages,
      totalTurnCount: 2,
    },
  );
  assert.equal(untruncated.truncated, false);
});

test("failed history loads surface a loadError and keep the conversation open", () => {
  let state = reduceChatSession(EMPTY_CHAT_SESSION, {
    type: "history-loading",
    conversationId: "123e4567-e89b-12d3-a456-426614174000",
  });
  state = reduceChatSession(state, {
    type: "history-failed",
    conversationId: "123e4567-e89b-12d3-a456-426614174000",
    failure: { code: "HTTP_STATUS", message: "not found", httpStatus: 404 },
  });
  assert.equal(state.loading, false);
  assert.equal(state.loadError?.httpStatus, 404);
  assert.equal(state.messages.length, 0);
  assert.equal(state.conversationId, "123e4567-e89b-12d3-a456-426614174000");
});

test("send-start appends an optimistic pending user bubble", () => {
  const state = reduceChatSession(
    { ...EMPTY_CHAT_SESSION },
    sendStart("u1"),
  );
  assert.equal(state.sending, true);
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].kind, "pending");
  assert.equal(state.messages[0].role, "user");
  assert.equal(state.loadError, null, "sending clears a stale history error");
});

test("send-success settles the user turn and appends the assistant turn", () => {
  let state = reduceChatSession({ ...EMPTY_CHAT_SESSION }, sendStart("u1"));
  state = reduceChatSession(state, {
    type: "send-success",
    userMessageId: "u1",
    assistantMessage: exchangeToAssistantMessage(PLAIN_EXCHANGE, () => "a1"),
    conversationId: PLAIN_EXCHANGE.conversationId,
  });
  assert.equal(state.sending, false);
  assert.equal(state.messages.length, 2);
  assert.equal(state.messages[0].kind, "sent");
  assert.equal(state.messages[1].role, "assistant");
  assert.equal(state.messages[1].content, "Hello there.");
  assert.equal(state.conversationId, PLAIN_EXCHANGE.conversationId, "first reply adopts its id");
});

test("send-success attaches proposal payloads for action/plan exchanges", () => {
  const exchange: ChatExchange = {
    ...PLAIN_EXCHANGE,
    status: "action_proposed",
    action: {
      approvalId: "apr-1",
      toolName: "create_directory",
      arguments: { path: "notes" },
      description: "Create notes.",
      riskLevel: "LOW_RISK",
      approvalStatus: "PENDING",
      expiresAt: "2099-01-01T01:00:00.000Z",
    },
  };
  const assistant = exchangeToAssistantMessage(exchange, () => "a2");
  assert.notEqual(assistant.proposal, null);
  assert.equal(assistant.proposal?.status, "action_proposed");
  assert.equal(assistant.proposal?.action?.toolName, "create_directory");

  const plain = exchangeToAssistantMessage(PLAIN_EXCHANGE, () => "a3");
  assert.equal(plain.proposal, null, "plain replies carry no proposal");
});

test("send-failed marks only the failed turn and keeps its text readable", () => {
  let state = reduceChatSession({ ...EMPTY_CHAT_SESSION }, sendStart("u1"));
  state = reduceChatSession(state, {
    type: "send-failed",
    userMessageId: "u1",
    failure: { code: "BACKEND_UNREACHABLE", message: "unreachable" },
  });
  assert.equal(state.sending, false);
  assert.equal(state.messages.length, 1);
  assert.equal(state.messages[0].kind, "failed");
  assert.equal(state.messages[0].content, "hello", "the user's text is preserved");
  assert.equal(state.messages[0].failure?.code, "BACKEND_UNREACHABLE");
  assert.equal(state.conversationId, null, "conversation is NOT adopted on failure");
});

test("sending blocks every history transition mid-flight", () => {
  const inFlight = reduceChatSession({ ...EMPTY_CHAT_SESSION }, sendStart("u1"));
  const switched = reduceChatSession(inFlight, { type: "new-chat" });
  assert.equal(switched.sending, true, "new-chat ignored while sending");
  const loading = reduceChatSession(inFlight, {
    type: "history-loading",
    conversationId: "123e4567-e89b-12d3-a456-426614174000",
  });
  assert.equal(loading.conversationId, null, "history-loading ignored while sending");
  const settled = reduceChatSession(inFlight, {
    type: "send-success",
    userMessageId: "u1",
    assistantMessage: exchangeToAssistantMessage(PLAIN_EXCHANGE, () => "a1"),
    conversationId: PLAIN_EXCHANGE.conversationId,
  });
  assert.equal(settled.sending, false, "completion is never blocked");
});

test("duplicate send-start is refused while a request is in flight", () => {
  const inFlight = reduceChatSession({ ...EMPTY_CHAT_SESSION }, sendStart("first"));
  const second = reduceChatSession(
    inFlight,
    sendStart("second"),
  );
  assert.equal(second.messages.length, 1, "only one optimistic bubble exists");
  assert.equal(second.messages[0].id, "first");
  assert.equal(second.sending, true);
});

test("the gate refuses shared submission slots and always releases", async () => {
  const gate = createChatGate();
  assert.equal(gate.busy, false);
  assert.equal(gate.tryBegin(), true);

  const second = await runExclusive(gate, async () => "never");
  assert.deepEqual(second, { ran: false, reason: "busy" });
  assert.equal(gate.busy, true);

  gate.end();
  assert.equal(gate.busy, false);
});

test("runExclusive serializes tasks and reports who actually ran", async () => {
  const gate = createChatGate();
  const events: string[] = [];

  const first = runExclusive(gate, async () => {
    events.push("first-begin");
    await new Promise((resolve) => setTimeout(resolve, 10));
    events.push("first-end");
    return 1;
  });
  const second = runExclusive(gate, async () => {
    events.push("second-begin");
    events.push("second-end");
    return 2;
  });

  assert.deepEqual(await first, { ran: true, value: 1 });
  assert.deepEqual(await second, { ran: false, reason: "busy" });
  assert.deepEqual(events, ["first-begin", "first-end"]);
});

test("runExclusive releases the gate even when the task throws — no retries", async () => {
  const gate = createChatGate();
  let attempts = 0;
  await assert.rejects(
    runExclusive(gate, async () => {
      attempts += 1;
      throw new Error("boom");
    }),
    /boom/,
  );
  assert.equal(gate.busy, false, "finally always releases");
  assert.equal(attempts, 1, "the task is never re-invoked");
  // The slot is usable again.
  assert.deepEqual(await runExclusive(gate, async () => "ok"), { ran: true, value: "ok" });
});

test("historyTurnsToMessages builds settled, chronological messages", () => {
  const messages = historyTurnsToMessages([USER_TURN, ASSISTANT_TURN], () => "fixed");
  assert.equal(messages.length, 2);
  assert.deepEqual(messages[0], {
    id: "fixed",
    role: "user",
    content: "who are you?",
    createdAt: "2099-01-01T00:00:00.000Z",
    kind: "sent",
    proposal: null,
    failure: null,
  });
  assert.equal(messages[1].role, "assistant");
});

test("createChatMessageId produces unique identifiers", () => {
  const ids = new Set(Array.from({ length: 200 }, () => createChatMessageId()));
  assert.equal(ids.size, 200);
  for (const id of ids) {
    assert.equal(typeof id, "string");
    assert.ok(id.length > 0);
  }
});
