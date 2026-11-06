/**
 * Worker protocol validation tests (Phase 6.4-b).
 *
 * The strict sorted-key/bounds rules that govern the main ⇄ worker
 * boundary are contract-tested here in plain Node: any message outside
 * these shapes must refuse BEFORE it carries data around.
 */

import test from "node:test";
import assert from "node:assert/strict";
import {
  validateWorkerRequest,
  validateWorkerResponse,
  VOICE_PROTOCOL_FEED_MAX_SAMPLES,
  VOICE_PROTOCOL_TTS_CHUNK_SAMPLES,
} from "../shared/voice-protocol";

const sttPaths = { encoder: "e.onnx", decoder: "d.onnx", joiner: "j.onnx", tokens: "t.txt" };
const ttsPaths = { model: "m.onnx", voices: "v.bin", tokens: "t.txt", dataDir: "d", lexicon: "l" };

test("init requests accept either leg, both legs, and nothing else", () => {
  assert.equal(validateWorkerRequest({ type: "init", stt: sttPaths }).ok, true);
  assert.equal(validateWorkerRequest({ type: "init", tts: ttsPaths }).ok, true);
  assert.equal(validateWorkerRequest({ type: "init", stt: sttPaths, tts: ttsPaths }).ok, true);

  for (const bad of [
    { type: "init" },
    { type: "init", stt: { ...sttPaths, joiner: undefined } },
    { type: "init", stt: { ...sttPaths, wrong: "keys" } },
    { type: "init", stt: sttPaths, tts: { ...ttsPaths, dataDir: undefined } },
    { type: "init", stt: sttPaths, tts: ttsPaths, rogue: 1 },
  ]) {
    assert.equal(validateWorkerRequest(bad).ok, false, JSON.stringify(bad));
  }
});

test("stt-feed demands a bounded Float32Array and exactly the samples key", () => {
  assert.equal(validateWorkerRequest({ type: "stt-feed", samples: new Float32Array(16) }).ok, true);
  assert.equal(
    validateWorkerRequest({ type: "stt-feed", samples: new Float32Array(VOICE_PROTOCOL_FEED_MAX_SAMPLES) }).ok,
    true,
    "65_536-sample ceiling passes",
  );
  for (const bad of [
    { type: "stt-feed", samples: new Float32Array(VOICE_PROTOCOL_FEED_MAX_SAMPLES + 1) },
    { type: "stt-feed", samples: new Float32Array(0) },
    { type: "stt-feed", samples: new Int16Array(8) },
    { type: "stt-feed", samples: "pcm" },
    { type: "stt-feed" },
    { type: "stt-feed", samples: new Float32Array(4), rogue: 1 },
  ]) {
    assert.equal(validateWorkerRequest(bad).ok, false, JSON.stringify(bad));
  }
});

test("tts-speak bounds: text, voice id, and speed lattice", () => {
  assert.equal(
    validateWorkerRequest({ type: "tts-speak", text: "hello", sid: 0, speed: 1 }).ok,
    true,
  );
  for (const bad of [
    { type: "tts-speak", text: "   " },
    { type: "tts-speak", text: "", sid: 0, speed: 1 },
    { type: "tts-speak", text: "ok", sid: -1, speed: 1 },
    { type: "tts-speak", text: "ok", sid: 1.5, speed: 1 },
    { type: "tts-speak", text: "ok", sid: 0, speed: 0.4 },
    { type: "tts-speak", text: "ok", sid: 0, speed: 3.1 },
    { type: "tts-speak", text: "ok", sid: 0, speed: NaN },
    { type: "tts-speak", text: "ok", sid: 0 },
    { type: "tts-speak", text: "ok", sid: 0, speed: 1, rogue: 1 },
  ]) {
    assert.equal(validateWorkerRequest(bad).ok, false, JSON.stringify(bad));
  }
  const fast = validateWorkerRequest({ type: "tts-speak", text: "ok", sid: 0, speed: 3 });
  const slow = validateWorkerRequest({ type: "tts-speak", text: "ok", sid: 0, speed: 0.5 });
  assert.equal(fast.ok && slow.ok, true, "lattice edges 0.5 and 3 are legal");
});

test("simple requests take no extra fields; unknown types refuse", () => {
  for (const good of [{ type: "stt-start" }, { type: "stt-end" }, { type: "tts-abort" }, { type: "shutdown" }]) {
    assert.equal(validateWorkerRequest(good).ok, true, JSON.stringify(good));
  }
  for (const bad of [
    { type: "stt-start", rogue: 1 },
    { type: "stt-end", rogue: 1 },
    { type: "tts-abort", force: true },
    { type: "shutdown", now: true },
    { type: "quantum_listening" },
    { type: 42 },
    "stt-start",
    null,
  ]) {
    assert.equal(validateWorkerRequest(bad).ok, false, JSON.stringify(bad));
  }
});

test("every worker response kind validates with exact keys", () => {
  const samples = new Float32Array(1024);
  const good = [
    { type: "worker-ready" },
    { type: "init-ok", stt: true, tts: true, ttsSampleRate: 24_000, ttsNumSpeakers: 1 },
    // stt-only init reports the HONEST tts-absent markers (0/0).
    { type: "init-ok", stt: true, tts: false, ttsSampleRate: 0, ttsNumSpeakers: 0 },
    { type: "init-fail", engine: "stt", message: "broken" },
    { type: "stt-partial", transcript: "" },
    { type: "stt-final", transcript: "hello there" },
    { type: "tts-chunk", sequence: 0, samples, sampleRate: 24_000 },
    { type: "tts-ended", totalSamples: samples.length, sampleRate: 24_000, aborted: false },
    { type: "worker-error", scope: "stt", message: "boom" },
    { type: "worker-error", scope: "tts", message: "boom", code: "TTS_FAILURE" },
  ];
  for (const row of good) {
    assert.equal(validateWorkerResponse(row).ok, true, JSON.stringify(row));
  }
});

test("worker responses refuse bad shapes and bounds", () => {
  const samples = new Float32Array(VOICE_PROTOCOL_TTS_CHUNK_SAMPLES);
  for (const bad of [
    { type: "worker-ready", extra: 1 },
    { type: "init-ok", stt: "yes", tts: true, ttsSampleRate: 24_000, ttsNumSpeakers: 1 },
    { type: "init-ok", stt: true, tts: true, ttsSampleRate: 4000, ttsNumSpeakers: 1 },
    { type: "init-ok", stt: true, tts: true, ttsSampleRate: 24_000, ttsNumSpeakers: 0 },
    // A speaker count without a loaded tts leg is a fabricated rate.
    { type: "init-ok", stt: true, tts: false, ttsSampleRate: 24_000, ttsNumSpeakers: 1 },
    { type: "init-ok", stt: true, tts: false, ttsSampleRate: 0, ttsNumSpeakers: 1 },
    // A loaded leg reporting rate 0 hides the model's rate — refused.
    { type: "init-ok", stt: true, tts: true, ttsSampleRate: 0, ttsNumSpeakers: 1 },
    { type: "init-fail", engine: "gpu", message: "broken" },
    { type: "init-fail", engine: "stt", message: "" },
    { type: "stt-final", transcript: "   " },
    { type: "stt-partial", transcript: "x".repeat(4000) },
    { type: "stt-partial", transcript: "ok", rogue: true },
    { type: "tts-chunk", sequence: 0, samples: new Float32Array(0), sampleRate: 24_000 },
    {
      type: "tts-chunk",
      sequence: 0,
      samples: new Float32Array(VOICE_PROTOCOL_TTS_CHUNK_SAMPLES + 1),
      sampleRate: 24_000,
    },
    { type: "tts-chunk", sequence: 0, samples: new Int16Array(8), sampleRate: 24_000 },
    { type: "tts-chunk", sequence: -1, samples, sampleRate: 24_000 },
    { type: "tts-chunk", sequence: 0, samples, sampleRate: 7999 },
    { type: "tts-chunk", sequence: 0, samples, sampleRate: 96_001 },
    { type: "tts-chunk", sequence: 0, samples }, // missing rate
    { type: "tts-ended", totalSamples: -1, sampleRate: 24_000, aborted: false },
    { type: "tts-ended", totalSamples: 8, sampleRate: 24_000, aborted: "no" },
    { type: "worker-error", scope: "cpu", message: "boom" },
    { type: "worker-error", scope: "stt", message: "" },
    { type: "worker-error", scope: "stt", message: "boom", code: "NOT_A_CODE" },
  ]) {
    assert.equal(validateWorkerResponse(bad).ok, false, JSON.stringify(bad));
  }
});
