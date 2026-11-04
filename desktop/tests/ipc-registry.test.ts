/** IPC registry + preload bridge contract tests. */

import test from "node:test";
import assert from "node:assert/strict";
import {
  IPC_CHANNELS,
  ipcOk,
  PRELOAD_METHODS,
  type EmptyPayload,
  type IpcChannel,
  type IpcResult,
} from "../shared/ipc-contract";
import {
  createIpcRegistry,
  isEmptyPayload,
  type IpcHandler,
} from "../electron/main/ipc/ipc-registry";

function override(handlers: Record<IpcChannel, IpcHandler>, key: IpcChannel, value: IpcHandler) {
  const next: Record<IpcChannel, IpcHandler> = { ...handlers };
  next[key] = value;
  return next;
}

function echo(result: unknown): IpcHandler {
  return async (_payload: EmptyPayload) => ipcOk(result);
}

const HANDLERS: Record<IpcChannel, IpcHandler> = {
  "jarvis:backend-status": echo({ apiOrigin: "http://127.0.0.1:8000" }),
  "jarvis:check-health": echo({ name: "JARVIS", status: "healthy", version: "0.1.0" }),
  "jarvis:get-system-info": echo({ toolName: "get_system_info" }),
};

function trusted(): boolean {
  return true;
}

function untrusted(): boolean {
  return false;
}

test("preload bridge covers every registered channel exactly", () => {
  const channels = PRELOAD_METHODS.map((m) => m.channel);
  const registered = Object.values(IPC_CHANNELS);
  assert.equal(channels.length, registered.length);
  assert.deepEqual([...channels].sort(), [...registered].sort());
  for (const method of PRELOAD_METHODS) {
    assert.match(method.channel, /^jarvis:/);
    assert.match(method.method, /^[a-zA-Z][a-zA-Z0-9_]*$/);
  }
});

test("dispatch passes the handler envelope through without re-wrapping", async () => {
  const registry = createIpcRegistry(
    override(HANDLERS, "jarvis:backend-status", async () => ipcOk({ nested: true })),
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

test("payloads must be empty, plain objects", async () => {
  const registry = createIpcRegistry(HANDLERS, trusted);
  for (const bad of [{ sneaky: "keys" }, [1, 2, 3], "text", 42, null]) {
    const result = await registry.dispatch("jarvis:check-health", bad, "http://127.0.0.1:5173/");
    assert.equal(result.ok, false);
    assert.equal(result.ok ? null : result.code, "BAD_PAYLOAD");
  }
  const passes: IpcResult<unknown> = await registry.dispatch(
    "jarvis:check-health",
    undefined,
    "http://127.0.0.1:5173/",
  );
  assert.equal(passes.ok, true);
});

test("handler crashes collapse to bounded UNEXPECTED failures", async () => {
  const registry = createIpcRegistry(
    override(
      HANDLERS,
      "jarvis:backend-status",
      async () => {
        throw new Error("handler stack should never reach the renderer");
      },
    ),
    trusted,
  );
  const result = await registry.dispatch("jarvis:backend-status", {}, "http://127.0.0.1:5173/");
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

test("isEmptyPayload is strict", () => {
  assert.equal(isEmptyPayload(undefined), true);
  assert.equal(isEmptyPayload({}), true);
  assert.equal(isEmptyPayload({ a: 1 }), false);
  assert.equal(isEmptyPayload([]), false);
  assert.equal(isEmptyPayload(null), false);
  assert.equal(isEmptyPayload("x"), false);
});
