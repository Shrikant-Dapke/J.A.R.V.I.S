import test from "node:test";
import assert from "node:assert/strict";
import { createJarvisIpcRegistry, type IpcHandlerDeps } from "../electron/main/ipc/handlers";
import { createMicSequenceGate } from "../electron/main/voice/mic-sequence-gate";
import type { VoiceEngine } from "../electron/main/voice/voice-engine";
import type { HttpBackendApiClient } from "../electron/main/api/client";
import {
  VOICE_MIC_CHUNK_SAMPLES,
  VOICE_PCM_CHUNK_MAX_BYTES,
  ipcOk,
} from "../shared/ipc-contract";

test("microphone sequence gate accepts increasing chunks and drops replays", () => {
  const gate = createMicSequenceGate();
  assert.deepEqual(gate.last(), null);
  assert.deepEqual(gate.accept(0), { accepted: true, sequence: 0 });
  assert.deepEqual(gate.accept(0), {
    accepted: false,
    sequence: 0,
    reason: "duplicate-or-replay",
  });
  assert.deepEqual(gate.accept(2), { accepted: true, sequence: 2 });
  assert.deepEqual(gate.accept(1), {
    accepted: false,
    sequence: 1,
    reason: "duplicate-or-replay",
  });
  assert.equal(gate.last(), 2);
  gate.reset();
  assert.equal(gate.last(), null);
  assert.deepEqual(gate.accept(0), { accepted: true, sequence: 0 });
});

function createVoiceStub(feeds: Float32Array[]): VoiceEngine {
  return {
    status: () => ({
      engineAvailable: true,
      sttModelPresent: true,
      ttsModelPresent: false,
      vadModelPresent: false,
      message: "test",
    }),
    prepareModels: async () => ipcOk({ downloaded: 0, bytes: 0 }),
    state: () => "listening",
    failure: () => null,
    arm: async () => ({ ok: true, state: "armed" }),
    begin: async () => ({ ok: true, state: "listening" }),
    feed: (samples) => {
      feeds.push(samples);
      return { ok: true, state: "listening" };
    },
    end: async () => ipcOk({ state: "transcript_ready", transcript: "test" }),
    cancel: () => ({ ok: true, state: "cancelled" }),
    speak: async () => ({ ok: true, state: "speaking" }),
    stop: () => ({ ok: true, state: "cancelled" }),
    acknowledge: () => ({ ok: true, state: "idle" }),
    dispose: () => undefined,
    subscribe: () => () => undefined,
  };
}

function createRegistry(feeds: Float32Array[]) {
  const deps: IpcHandlerDeps = {
    client: {} as HttpBackendApiClient,
    getStatus: () => ({
      apiOrigin: "http://127.0.0.1:8000",
      tokenConfigured: true,
      tokenSource: "env",
      sessionIdMasked: "test",
      appVersion: "test",
      runtime: { electron: "test", node: "test" },
    }),
    navigationTrust: { devServerUrl: "http://127.0.0.1:5173", isDistHosting: false },
    voice: createVoiceStub(feeds),
  };
  return createJarvisIpcRegistry(deps);
}

function pcmBytes(samples = 1): ArrayBuffer {
  return new ArrayBuffer(samples * 2);
}

test("voice-feed validates bounds, gates sequence order, and resets per begin", async () => {
  const feeds: Float32Array[] = [];
  const registry = createRegistry(feeds);
  const dispatch = (sequence: number, pcm = pcmBytes(VOICE_MIC_CHUNK_SAMPLES)) =>
    registry.dispatch("jarvis:voice-feed", { sequence, pcm }, "http://127.0.0.1:5173/");

  assert.equal((await dispatch(0)).ok, true);
  assert.equal(feeds.length, 1);
  assert.equal((await dispatch(0)).ok, true, "duplicate is an idempotent drop");
  assert.equal(feeds.length, 1);
  assert.equal((await dispatch(2)).ok, true, "strictly increasing gaps are safe");
  assert.equal(feeds.length, 2);
  assert.equal((await dispatch(1)).ok, true, "out-of-order replay is an idempotent drop");
  assert.equal(feeds.length, 2);

  const begin = await registry.dispatch("jarvis:voice-begin", {}, "http://127.0.0.1:5173/");
  assert.equal(begin.ok, true);
  assert.equal((await dispatch(0)).ok, true, "new capture starts at sequence zero");
  assert.equal(feeds.length, 3);

  const oversized = await dispatch(1, pcmBytes(VOICE_PCM_CHUNK_MAX_BYTES / 2 + 1));
  assert.equal(oversized.ok, false);
  if (!oversized.ok) assert.equal(oversized.code, "BAD_PAYLOAD");
  assert.equal(feeds.length, 3);

  const odd = await dispatch(1, new ArrayBuffer(3));
  assert.equal(odd.ok, false);
  if (!odd.ok) assert.equal(odd.code, "BAD_PAYLOAD");
});

test("voice-feed rejects unauthorized frames before handling PCM", async () => {
  const feeds: Float32Array[] = [];
  const registry = createRegistry(feeds);
  const result = await registry.dispatch(
    "jarvis:voice-feed",
    { sequence: 0, pcm: pcmBytes() },
    "https://attacker.example/",
  );
  assert.equal(result.ok, false);
  if (!result.ok) assert.equal(result.code, "UNAUTHORIZED_FRAME");
  assert.equal(feeds.length, 0);
});
