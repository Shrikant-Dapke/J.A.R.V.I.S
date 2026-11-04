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
