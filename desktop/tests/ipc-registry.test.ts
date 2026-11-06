/** IPC registry + preload bridge contract tests (24 channels, per-channel payloads). */

import test from "node:test";
import assert from "node:assert/strict";
import {
  IPC_CHANNELS,
  PAYLOAD_VALIDATORS,
  ipcError,
  ipcOk,
  PRELOAD_METHODS,
  type IpcChannel,
  type IpcPayloadMap,
  type IpcResult,
} from "../shared/ipc-contract";
import { VOICE_ERROR_COPY } from "../shared/voice-engine";
import { createIpcRegistry, type IpcHandlers } from "../electron/main/ipc/ipc-registry";

/** Every channel echoes a distinct marker so tests can identify routing. */
const HANDLERS: IpcHandlers = {
  "jarvis:backend-status": async () => ipcOk({ channel: "backend-status" }),
  "jarvis:check-health": async () => ipcOk({ channel: "check-health" }),
  "jarvis:get-system-info": async () => ipcOk({ channel: "get-system-info" }),
  "jarvis:send-chat": async (payload) => ipcOk({ channel: "send-chat", payload }),
  "jarvis:list-conversations": async (payload) => ipcOk({ channel: "list-conversations", payload }),
  "jarvis:get-conversation": async (payload) => ipcOk({ channel: "get-conversation", payload }),
  "jarvis:delete-conversation": async (payload) =>
    ipcOk({ channel: "delete-conversation", payload }),
  "jarvis:list-approvals": async (payload) => ipcOk({ channel: "list-approvals", payload }),
  "jarvis:get-approval": async (payload) => ipcOk({ channel: "get-approval", payload }),
  "jarvis:approve-approval": async (payload) => ipcOk({ channel: "approve-approval", payload }),
  "jarvis:reject-approval": async (payload) => ipcOk({ channel: "reject-approval", payload }),
  "jarvis:list-plans": async (payload) => ipcOk({ channel: "list-plans", payload }),
  "jarvis:get-plan": async (payload) => ipcOk({ channel: "get-plan", payload }),
  "jarvis:request-plan-step": async (payload) => ipcOk({ channel: "request-plan-step", payload }),
  "jarvis:resolve-plan-recovery": async (payload) =>
    ipcOk({ channel: "resolve-plan-recovery", payload }),
  // Phase 6.4 voice echo stubs (handlers.ts routes the real rows).
  "jarvis:voice-status": async (payload) => ipcOk({ channel: "voice-status", payload }),
  "jarvis:voice-prepare-models": async (payload) =>
    ipcOk({ channel: "voice-prepare-models", payload }),
  "jarvis:voice-arm": async (payload) => ipcOk({ channel: "voice-arm", payload }),
  "jarvis:voice-begin": async (payload) => ipcOk({ channel: "voice-begin", payload }),
  "jarvis:voice-end": async (payload) => ipcOk({ channel: "voice-end", payload }),
  "jarvis:voice-cancel": async (payload) => ipcOk({ channel: "voice-cancel", payload }),
  "jarvis:voice-feed": async (payload) => ipcOk({ channel: "voice-feed", payload }),
  "jarvis:tts-speak": async (payload) => ipcOk({ channel: "tts-speak", payload }),
  "jarvis:tts-stop": async (payload) => ipcOk({ channel: "tts-stop", payload }),
};

const VALID_UUID = "123e4567-e89b-12d3-a456-426614174000";
const trusted = () => true;
const untrusted = () => false;

function dispatch(channel: IpcChannel, payload: unknown, url = "http://127.0.0.1:5173/") {
  return createIpcRegistry(HANDLERS, trusted).dispatch(channel, payload, url);
}

test("preload bridge covers every registered channel exactly (24 channels)", () => {
  const channels = PRELOAD_METHODS.map((m) => m.channel);
  const registered = Object.values(IPC_CHANNELS);
  assert.equal(channels.length, registered.length);
  assert.equal(channels.length, 24);
  assert.deepEqual([...channels].sort(), [...registered].sort());
  for (const method of PRELOAD_METHODS) {
    assert.match(method.channel, /^jarvis:/);
    assert.match(method.method, /^[a-zA-Z][a-zA-Z0-9_]*$/);
  }
});

test("every channel has a payload validator in the shared table", () => {
  for (const channel of Object.values(IPC_CHANNELS)) {
    const validator = PAYLOAD_VALIDATORS[channel as keyof IpcPayloadMap];
    assert.equal(typeof validator, "function", `missing validator for ${channel}`);
  }
  assert.equal(Object.keys(PAYLOAD_VALIDATORS).length, 24);
});

test("dispatch passes the handler envelope through without re-wrapping", async () => {
  const registry = createIpcRegistry(
    { ...HANDLERS, "jarvis:backend-status": async () => ipcOk({ nested: true }) },
    trusted,
  );
  const result = await registry.dispatch("jarvis:backend-status", {}, "http://127.0.0.1:5173/");
  assert.deepEqual(result, { ok: true, data: { nested: true } });
});

test("unknown and non-namespaced channels are rejected", async () => {
  const registry = createIpcRegistry(HANDLERS, trusted);
  const unknown = await registry.dispatch("jarvis:made-up", {}, "http://127.0.0.1:5173/");
  assert.equal(unknown.ok, false);
  assert.equal(unknown.ok ? null : unknown.code, "UNKNOWN_CHANNEL");
  const bare = await registry.dispatch("backend-status", {}, "http://127.0.0.1:5173/");
  assert.equal(bare.ok ? null : bare.code, "UNKNOWN_CHANNEL");
});

test("unauthorized calling frames are rejected before payload handling", async () => {
  const registry = createIpcRegistry(HANDLERS, untrusted);
  const result = await registry.dispatch("jarvis:check-health", {}, "https://evil.example/");
  assert.equal(result.ok, false);
  assert.equal(result.ok ? null : result.code, "UNAUTHORIZED_FRAME");
});

test("chat payload validation covers size, shape, and id rules", async () => {
  const happy = {
    message: "status report",
    conversationId: null,
  };
  const ok = (await dispatch("jarvis:send-chat", happy)) as IpcResult<{
    channel: string;
    payload: { message: string; conversationId: string | null };
  }>;
  assert.equal(ok.ok, true);
  if (ok.ok) {
    assert.equal(ok.data.payload.message, "status report");
    assert.equal(ok.data.payload.conversationId, null);
  }

  const badMessage = (await dispatch("jarvis:send-chat", {
    message: "   ",
    conversationId: null,
  })) as IpcResult<never>;
  assert.equal(badMessage.ok ? null : badMessage.code, "BAD_PAYLOAD");

  const oversized = (await dispatch("jarvis:send-chat", {
    message: "x".repeat(4001),
    conversationId: null,
  })) as IpcResult<never>;
  assert.equal(oversized.ok ? null : oversized.code, "BAD_PAYLOAD");

  const badId = (await dispatch("jarvis:send-chat", {
    message: "hello",
    conversationId: "not-a-uuid",
  })) as IpcResult<never>;
  assert.equal(badId.ok ? null : badId.code, "BAD_PAYLOAD");

  // Extra keys are rejected (payload smuggling guard).
  const extraKeys = (await dispatch("jarvis:send-chat", {
    message: "hello",
    conversationId: null,
    role: "system",
  })) as IpcResult<never>;
  assert.equal(extraKeys.ok ? null : extraKeys.code, "BAD_PAYLOAD");

  // Exactly the two contract keys, in any order, with a valid uuid.
  const withId = (await dispatch("jarvis:send-chat", {
    conversationId: VALID_UUID,
    message: "  hello  ",
  })) as IpcResult<{ payload: { message: string; conversationId: string } }>;
  assert.equal(withId.ok, true);
  if (withId.ok) {
    assert.equal(withId.data.payload.message, "hello", "message is trimmed at the wire");
    assert.equal(withId.data.payload.conversationId, VALID_UUID);
  }
});

test("conversation list payload bounds mirror the backend page limits", async () => {
  const ok = (await dispatch("jarvis:list-conversations", { limit: 50, offset: 0 })) as IpcResult<{
    payload: { limit?: number; offset?: number };
  }>;
  assert.equal(ok.ok, true);
  if (ok.ok) assert.deepEqual(ok.data.payload, { limit: 50, offset: 0 });

  const empty = await dispatch("jarvis:list-conversations", {});
  assert.equal(empty.ok, true);
  const undefinedPayload = await dispatch("jarvis:list-conversations", undefined);
  assert.equal(undefinedPayload.ok, true);

  for (const badLimit of [0, 201, 12.5, "50", null]) {
    const result = (await dispatch("jarvis:list-conversations", { limit: badLimit })) as IpcResult<
      never
    >;
    assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `limit ${String(badLimit)}`);
  }
  const badOffset = (await dispatch("jarvis:list-conversations", {
    offset: -1,
  })) as IpcResult<never>;
  assert.equal(badOffset.ok ? null : badOffset.code, "BAD_PAYLOAD");
  const extra = (await dispatch("jarvis:list-conversations", {
    limit: 50,
    sneaky: true,
  })) as IpcResult<never>;
  assert.equal(extra.ok ? null : extra.code, "BAD_PAYLOAD");
});

test("conversation id payloads require a strict backend uuid", async () => {
  for (const channel of ["jarvis:get-conversation", "jarvis:delete-conversation"] as const) {
    const ok = (await dispatch(channel, { conversationId: VALID_UUID })) as IpcResult<{
      payload: { conversationId: string };
    }>;
    assert.equal(ok.ok, true, channel);
    if (ok.ok) assert.equal(ok.data.payload.conversationId, VALID_UUID);

    for (const bad of ["", "123", "not-a-uuid", null, 42]) {
      const result = (await dispatch(channel, { conversationId: bad })) as IpcResult<never>;
      assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `${channel} id ${String(bad)}`);
    }
    const emptyPayload = (await dispatch(channel, {})) as IpcResult<never>;
    assert.equal(emptyPayload.ok ? null : emptyPayload.code, "BAD_PAYLOAD");
  }
});

test("empty-payload channels reject anything but an empty object", async () => {
  for (const channel of [
    "jarvis:backend-status",
    "jarvis:check-health",
    "jarvis:get-system-info",
  ] as const) {
    for (const bad of [{ sneaky: "keys" }, [1, 2, 3], "text", 42, null]) {
      const result = (await dispatch(channel, bad)) as IpcResult<never>;
      assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `${channel} ${String(bad)}`);
    }
    const passes = await dispatch(channel, undefined);
    assert.equal(passes.ok, true, channel);
  }
});

test("handler crashes collapse to bounded UNEXPECTED failures", async () => {
  const registry = createIpcRegistry(
    {
      ...HANDLERS,
      "jarvis:send-chat": async () => {
        throw new Error("handler stack should never reach the renderer");
      },
    },
    trusted,
  );
  const result = await registry.dispatch(
    "jarvis:send-chat",
    { message: "hello", conversationId: null },
    "http://127.0.0.1:5173/",
  );
  assert.equal(result.ok, false);
  assert.equal(result.ok ? null : result.code, "UNEXPECTED");
  if (!result.ok) {
    assert.equal(
      result.message.includes("handler stack"),
      false,
      "handler internals must not reach the renderer",
    );
    assert.match(result.message, /unexpected failure/u, "fixed, non-sensitive text only");
  }
});

/* -------------------------------------------------------------------- */
/* Phase 6.3 workspace channels                                          */
/* -------------------------------------------------------------------- */

test("approval channel payloads require a strict backend uuid", async () => {
  for (const channel of [
    "jarvis:list-approvals",
    "jarvis:get-approval",
    "jarvis:approve-approval",
    "jarvis:reject-approval",
  ] as const) {
    const listing = channel === "jarvis:list-approvals";
    // The listing page accepts bounded pages; the id channels want uuids.
    const ok = await dispatch(channel, listing ? {} : { approvalId: VALID_UUID });
    assert.equal(ok.ok, true, channel);
  }

  for (const channel of [
    "jarvis:get-approval",
    "jarvis:approve-approval",
    "jarvis:reject-approval",
  ] as const) {
    for (const bad of ["", VALID_UUID.slice(1), `X${VALID_UUID}`, null, 42]) {
      const result = (await dispatch(channel, { approvalId: bad })) as IpcResult<never>;
      assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `${channel} ${String(bad)}`);
    }
    // Exactly one key allowed — no argument smuggling on decision actions.
    const smuggled = (await dispatch(channel, {
      approvalId: VALID_UUID,
      arguments: { cmd: "rm -rf /" },
    })) as IpcResult<never>;
    assert.equal(smuggled.ok ? null : smuggled.code, "BAD_PAYLOAD");
    const missing = (await dispatch(channel, {})) as IpcResult<never>;
    assert.equal(missing.ok ? null : missing.code, "BAD_PAYLOAD");
  }
});

test("plan id and step id payloads enforce backend identifier patterns", async () => {
  const ok = (await dispatch("jarvis:get-plan", { planId: "travel-plan_01" })) as IpcResult<{
    payload: { planId: string };
  }>;
  assert.equal(ok.ok, true);
  if (ok.ok) assert.equal(ok.data.payload.planId, "travel-plan_01");

  for (const bad of ["", "travel plan", "plan;drop", `${"a".repeat(65)}`, null, 42]) {
    const result = (await dispatch("jarvis:get-plan", { planId: bad })) as IpcResult<never>;
    assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `planId ${String(bad)}`);
  }

  for (const channel of ["jarvis:request-plan-step", "jarvis:resolve-plan-recovery"] as const) {
    const ok = (await dispatch(channel, {
      planId: "clean-house",
      stepId: "step_1",
    })) as IpcResult<{ payload: { planId: string; stepId: string } }>;
    assert.equal(ok.ok, true, channel);
    if (ok.ok) {
      assert.equal(ok.data.payload.planId, "clean-house");
      assert.equal(ok.data.payload.stepId, "step_1");
    }

    for (const badStep of ["", "s-1", "step.1", `${"a".repeat(33)}`, null, 42]) {
      const result = (await dispatch(channel, {
        planId: "clean-house",
        stepId: badStep,
      })) as IpcResult<never>;
      assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `${channel} ${String(badStep)}`);
    }
    const partial = (await dispatch(channel, { planId: "clean-house" })) as IpcResult<never>;
    assert.equal(partial.ok ? null : partial.code, "BAD_PAYLOAD");
    const extra = (await dispatch(channel, {
      planId: "clean-house",
      stepId: "step_1",
      force: true,
    })) as IpcResult<never>;
    assert.equal(extra.ok ? null : extra.code, "BAD_PAYLOAD");
  }
});

test("the workspace listing channels share the conversation page bounds", async () => {
  for (const channel of ["jarvis:list-approvals", "jarvis:list-plans"] as const) {
    const empty = await dispatch(channel, {});
    assert.equal(empty.ok, true, channel);
    const undefinedPayload = await dispatch(channel, undefined);
    assert.equal(undefinedPayload.ok, true);
    const bounded = await dispatch(channel, { limit: 200, offset: 1 });
    assert.equal(bounded.ok, true, `${channel} accepts the 1..200 window`);
    for (const badLimit of [0, 201, 12.5, "50"]) {
      const result = (await dispatch(channel, { limit: badLimit })) as IpcResult<never>;
      assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `${channel} ${String(badLimit)}`);
    }
    const extra = (await dispatch(channel, { limit: 50, approvalId: VALID_UUID })) as IpcResult<
      never
    >;
    assert.equal(extra.ok ? null : extra.code, "BAD_PAYLOAD");
  }
});

/* -------------------------------------------------------------------- */
/* Phase 6.4 voice channels                                              */
/* -------------------------------------------------------------------- */

test("voice command channels accept only the canonical empty payload", async () => {
  for (const channel of [
    "jarvis:voice-status",
    "jarvis:voice-prepare-models",
    "jarvis:voice-arm",
    "jarvis:voice-begin",
    "jarvis:voice-end",
    "jarvis:voice-cancel",
    "jarvis:tts-stop",
  ] as const) {
    const ok = await dispatch(channel, {});
    assert.equal(ok.ok, true, channel);
    const undefinedOk = await dispatch(channel, undefined);
    assert.equal(undefinedOk.ok, true, channel);

    for (const bad of [{ sneaky: "keys" }, [1, 2, 3], "text", 42, null]) {
      const result = (await dispatch(channel, bad)) as IpcResult<never>;
      assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", `${channel} ${String(bad)}`);
    }
  }
});

test("tts-speak payload bounds: exact shape, identity, trimmed text, 2000-char ceiling", async () => {
  const ok = (await dispatch("jarvis:tts-speak", { text: "  status ready  ", requestId: 4 })) as IpcResult<{
    payload: { text: string; requestId: number };
  }>;
  assert.equal(ok.ok, true);
  if (ok.ok) {
    assert.equal(ok.data.payload.text, "status ready", "text is trimmed at the wire");
    assert.equal(ok.data.payload.requestId, 4);
  }

  // Exactly the 2000-char ceiling passes.
  const atLimit = await dispatch("jarvis:tts-speak", { text: "x".repeat(2000), requestId: 4 });
  assert.equal(atLimit.ok, true, "2000 characters must be accepted");

  for (const bad of [
    { text: "   ", requestId: 4 },
    { text: "", requestId: 4 },
    { text: "x".repeat(2001), requestId: 4 },
    { text: 42, requestId: 4 },
    { text: null, requestId: 4 },
    {}, // missing keys
    { text: "hello", requestId: 4, voice: "alloy" }, // extra key smuggling
    { text: "hello" }, // missing identity
    { text: "hello", requestId: -1 },
    { text: "hello", requestId: 1.5 },
  ]) {
    const result = (await dispatch("jarvis:tts-speak", bad)) as IpcResult<never>;
    assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD", JSON.stringify(bad).slice(0, 40));
  }
});

test("voice handler stubs fail closed with honest ENGINE_UNAVAILABLE copy", async () => {
  // Mirrors handlers.ts behavior: presence of the channels in the registry
  // (so routing/validators work) while every answer is an explicit
  // engine-unavailable failure — never a fake status or synthetic audio.
  const voiceHandlers: IpcHandlers = Object.fromEntries(
    Object.entries(HANDLERS).map(([channel, handler]) => [
      channel,
      channel.startsWith("jarvis:voice-") || channel.startsWith("jarvis:tts-")
        ? async () => ipcError("ENGINE_UNAVAILABLE", VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"])
        : handler,
    ]),
  ) as IpcHandlers;
  const registry = createIpcRegistry(voiceHandlers, trusted);

  for (const channel of [
    "jarvis:voice-status",
    "jarvis:voice-prepare-models",
    "jarvis:voice-arm",
    "jarvis:voice-begin",
    "jarvis:voice-end",
    "jarvis:voice-cancel",
    "jarvis:tts-speak",
    "jarvis:tts-stop",
  ] as const) {
    const payload = channel === "jarvis:tts-speak" ? { text: "hello", requestId: 1 } : {};
    const result = await registry.dispatch(channel, payload, "http://127.0.0.1:5173/");
    assert.equal(result.ok, false, channel);
    if (!result.ok) {
      assert.equal(result.code, "ENGINE_UNAVAILABLE", channel);
      assert.equal(result.message, VOICE_ERROR_COPY["ENGINE_UNAVAILABLE"], channel);
    }
  }
});

test("the voice event channel is deliberately absent from the registry", async () => {
  assert.equal(
    Object.values(IPC_CHANNELS).includes("jarvis:voice-events" as IpcChannel),
    false,
    "events are main→renderer only and must never be invokable",
  );
  assert.equal(PAYLOAD_VALIDATORS["jarvis:voice-events" as keyof IpcPayloadMap], undefined);
});
