/**
 * Engine ⇄ worker integration tests (Phase 6.4-b).
 *
 * A REAL voice worker (`startVoiceWorker`, Electron-free) runs against a
 * REAL engine (`createVoiceEngine`) through an in-memory transport, with
 * a fake native sherpa module injected. Covers state-machine truth,
 * failure honesty, empty-recognition honesty, worker crash recovery,
 * and TTS chunk bounding/format — with zero Electron or model downloads.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { startVoiceWorker, type WorkerHost } from "../electron/main/voice/voice-worker";
import { createVoiceEngine, type VoiceWorkerTransport } from "../electron/main/voice/voice-engine";
import type { VoiceModelManager } from "../electron/main/voice/model-manager";
import type { VoiceEventPayload } from "../shared/voice-engine";
import { validateVoiceEventPayload } from "../shared/voice-engine";
import {
  VOICE_PROTOCOL_FEED_MAX_SAMPLES,
} from "../shared/voice-protocol";

/* ---------------------------------------------------------------------- */
/* Fake native module — a controlled, honest stand-in for sherpa-onnx     */
/* ---------------------------------------------------------------------- */

const nativeControl = {
  recognizerShouldThrow: false,
  recognizerTranscript: "the quarterly report is ready",
  /** Total synthesized samples per utterance. */
  ttsTotalSamples: 80_000,
  /** How many incremental onProgress pieces tile that total. */
  ttsPieceCount: 1,
  ttsSampleRate: 24_000,
  loadCount: 0,
  decodeCount: 0,
  resetCount: 0,
  lastConfig: null as unknown,
  lastText: "" as string,
};

class FakeOnlineRecognizer {
  constructor(config: unknown) {
    nativeControl.lastConfig = config;
    if (nativeControl.recognizerShouldThrow) {
      throw new Error("Please check your config! (fake native failure)");
    }
  }
  createStream(): { text: string; acceptWaveform: (wave: { samples: Float32Array; sampleRate: number }) => void; inputFinished: () => void } | { text: string } {
    const stream: { text: string } = { text: "" };
    return Object.assign(stream, {
      acceptWaveform: (wave: { samples: Float32Array; sampleRate: number }): void => {
        void wave;
        stream.text = nativeControl.recognizerTranscript;
      },
      inputFinished: (): void => undefined,
    });
  }  isReady(): boolean {
    return false;
  }
  decode(): void {
    nativeControl.decodeCount += 1;
  }
  getResult(stream: { text: string }): { text: string; tokens: string[] } {
    return { text: stream.text, tokens: [] };
  }
  reset(): void {
    nativeControl.resetCount += 1;
  }
}

const fakeTtsInstance = {
  sampleRate: nativeControl.ttsSampleRate,
  numSpeakers: 1,
  generateAsync: async (request: {
    text: string;
    onProgress?: (info: { samples: Float32Array; progress: number }) => number;
  }) => {
    nativeControl.lastText = request.text;
    // Native behavior mirrored EXACTLY: onProgress delivers incremental
    // pieces; the returned audio is the whole utterance (a naive worker
    // that emits both would double-count — the totals below prove denial).
    let remaining = nativeControl.ttsTotalSamples;
    for (let piece = 0; piece < nativeControl.ttsPieceCount; piece += 1) {
      const size = piece === nativeControl.ttsPieceCount - 1
        ? remaining
        : Math.ceil(nativeControl.ttsTotalSamples / nativeControl.ttsPieceCount);
      remaining -= size;
      if (request.onProgress?.({ samples: new Float32Array(size), progress: (piece + 1) / nativeControl.ttsPieceCount }) === 0) {
        break; // abort path
      }
    }
    return {
      samples: new Float32Array(nativeControl.ttsTotalSamples),
      sampleRate: nativeControl.ttsSampleRate,
    };
  },
};

const fakeNativeModule = {
  OnlineRecognizer: FakeOnlineRecognizer,
  OfflineTts: {
    createAsync: async () => {
      nativeControl.loadCount += 1;
      return fakeTtsInstance;
    },
  },
};

/* ---------------------------------------------------------------------- */
/* Harness: real worker + real engine over an in-memory transport         */
/* ---------------------------------------------------------------------- */

function createHarness(): { engine: ReturnType<typeof createVoiceEngine>; events: VoiceEventPayload[]; spawnCount: () => number } {
  const events: VoiceEventPayload[] = [];
  let spawned = 0;

  const factory = (): Promise<VoiceWorkerTransport> =>
    new Promise((resolve) => {
      spawned += 1;
      const handlers = new Set<(response: unknown) => void>();
      const exitHandlers = new Set<(info: { exitCode: number | null }) => void>();
      let queuedResponses: unknown[] = [];
      let killed = false;
      let flushScheduled = false;
      let requestHandler: ((request: unknown) => void) | null = null;

      const flush = (): void => {
        flushScheduled = false;
        const snapshot = queuedResponses;
        queuedResponses = [];
        for (const message of snapshot) {
          for (const handler of handlers) handler(message);
        }
      };

      // worker-ready posts BEFORE the main side attaches; real
      // utilityProcess delivers messages asynchronously, so the fake
      // queues and flushes on a macrotask (after the engine's continued
      // await attaches both handlers and its awaiting slot).
      const host: WorkerHost = {
        post: (message) => {
          if (handlers.size > 0) {
            for (const handler of handlers) handler(message);
            return;
          }
          queuedResponses.push(message);
          if (!flushScheduled) {
            flushScheduled = true;
            setTimeout(flush, 0);
          }
        },
        listen: (onRequest) => {
          requestHandler = onRequest as (request: unknown) => void;
        },
      };
      startVoiceWorker(host, fakeNativeModule);

      resolve({
        send: (request) => {
          if (killed) throw new Error("transport closed");
          requestHandler?.(request);
        },
        onResponse: (callback) => {
          handlers.add(callback as (response: unknown) => void);
          // Queued beacons flush on the scheduled macrotask (real
          // utilityProcess delivers messages as tasks, after the engine
          // has attached handlers AND set its awaiting slot).
        },
        onExit: (callback) => {
          exitHandlers.add(callback);
        },
        kill: () => {
          if (killed) return;
          killed = true;
          for (const exit of exitHandlers) exit({ exitCode: 1 });
        },
        running: () => !killed,
      });
    });

  // Path objects are exact-keyed by the protocol validators — insertion
  // order must be sorted, exactly like the real model manager produces.
  const sttPaths = { decoder: "d.onnx", encoder: "e.onnx", joiner: "j.onnx", tokens: "t.txt" };
  const ttsPaths = { dataDir: "d", lexicon: "l", model: "m.onnx", tokens: "t.txt", voices: "v.bin" };
  const models: VoiceModelManager = {
    baseDir: "C:\\fake\\models",
    smokePaths: () => ({ wavsDir: null, transTxt: null, missingAssets: [] }),
    status: () => ({
      sttPresent: true,
      ttsPresent: true,
      sttMissing: [],
      ttsMissing: [],
      sttPaths,
      ttsPaths,
    }),
    prepareModels: async () => ({ ok: true, downloaded: 0, verifiedFromCache: 0, bytes: 0 }),
    verify: async () => ({ ok: true, checked: 1 }),
  };

  const engine = createVoiceEngine({
    models,
    createTransport: factory,
    spawnTimeoutMs: 2_000,
    initTimeoutMs: 5_000,
    streamTimeoutMs: 2_000,
  });
  engine.subscribe((event) => {
    // Every event the engine emits must ALREADY pass the shared
    // validator; assert it as the events are collected.
    const validated = validateVoiceEventPayload(event);
    assert.equal(validated.ok, true, `${event.kind}: ${validated.ok ? "" : validated.reason}`);
    events.push(validated.value);
  });
  return { engine, events, spawnCount: () => spawned };
}

/* ---------------------------------------------------------------------- */
/* Tests                                                                  */
/* ---------------------------------------------------------------------- */

test("full capture flow: arm → begin → feed → end with real worker semantics", async () => {
  nativeControl.recognizerTranscript = "the quarterly report is ready";
  const { engine, events } = createHarness();

  await engine.arm();
  assert.equal(engine.state(), "armed");

  const begin = await engine.begin();
  assert.equal(begin.ok, true, JSON.stringify(begin));
  assert.equal(engine.state(), "listening");

  const feed = engine.feed(new Float32Array(2048));
  assert.equal(feed.ok, true, JSON.stringify(feed));

  const ended = await engine.end();
  assert.equal(ended.ok, true, JSON.stringify(ended));
  if (ended.ok) {
    assert.equal(ended.data.transcript, "the quarterly report is ready");
    assert.equal(ended.data.state, "transcript_ready");
  }

  const kinds = events.map((event) => event.kind);
  assert.ok(kinds.includes("state"), "state events emitted");
  assert.ok(kinds.includes("partial"), "partial event emitted with interim text");
  assert.ok(kinds.includes("final"), "final event emitted");
  const finalEvent = events.find((event): event is Extract<VoiceEventPayload, { kind: "final" }> => event.kind === "final");
  assert.equal(finalEvent?.transcript, "the quarterly report is ready");
});

test("interim text only re-emits when the transcript CHANGES", async () => {
  nativeControl.recognizerTranscript = "same words every time";
  const { engine, events } = createHarness();
  await engine.arm();
  await engine.begin();
  engine.feed(new Float32Array(256));
  engine.feed(new Float32Array(256));
  const partials = events.filter((event): event is Extract<VoiceEventPayload, { kind: "partial" }> => event.kind === "partial");
  assert.equal(partials.length, 1, "identical interim text is not re-sent");
  await engine.end();
});

test("empty recognition is HONEST: end() fails STT_FAILURE instead of faking", async () => {
  nativeControl.recognizerTranscript = "";
  const { engine } = createHarness();
  await engine.arm();
  await engine.begin();
  engine.feed(new Float32Array(256));
  const ended = await engine.end();
  assert.equal(ended.ok, false);
  if (!ended.ok) {
    assert.equal(ended.code, "STT_FAILURE");
    assert.match(ended.message, /No speech was recognized/u);
  }
  assert.equal(engine.state(), "failed");
  assert.equal(engine.failure()?.code, "STT_FAILURE");
});

test("unavailable native runtime → honest failure → arm resets and recovers", async () => {
  nativeControl.recognizerShouldThrow = true;
  const { engine } = createHarness();
  await engine.arm();
  const begin = await engine.begin();
  assert.equal(begin.ok, false);
  if (!begin.ok) {
    assert.equal(begin.code, "MODEL_MISSING");
    assert.match(begin.message, /fake native failure/u);
  }
  assert.equal(engine.state(), "failed");
  nativeControl.recognizerShouldThrow = false; // "runtime fixed" — recovery works
  const arm = await engine.arm();
  assert.equal(arm.ok, true, "arm acknowledges the failure (reset) and re-arms");
  assert.equal(engine.state(), "armed");
  const beginAgain = await engine.begin();
  assert.equal(beginAgain.ok, true, JSON.stringify(beginAgain));
});

test("worker crash during listening fails the session; next arm respawns it", async () => {
  nativeControl.recognizerTranscript = "the quarterly report is ready"; // don't inherit a prior test's empty transcript
  const { engine, spawnCount } = createHarness();
  await engine.arm();
  assert.equal((await engine.begin()).ok, true);
  assert.equal(engine.state(), "listening");

  engine.dispose(); // kills the live transport → the engine sees an exit
  const feed = engine.feed(new Float32Array(64));
  assert.equal(feed.ok, false, "audio into a dead worker is refused honestly");
  assert.equal(engine.state(), "failed");

  const arm = await engine.arm();
  assert.equal(arm.ok, true);
  assert.equal(engine.state(), "armed");
  const beginAgain = await engine.begin();
  assert.equal(beginAgain.ok, true, "a NEW worker is spawned for the next session");
  assert.ok(spawnCount() >= 2);
  // Real worker honesty: recognition is earned by audio. Feed after the
  // respawn so the stream legitimately produces a transcript.
  engine.feed(new Float32Array(256));
  assert.equal((await engine.end()).ok, true);
});

test("compression honesty: PCM16 chunks are bounded, ordered, rate-tagged, and NOT double-counted", async () => {
  nativeControl.ttsTotalSamples = 80_000; // exercises worker-side >32k slicing
  nativeControl.ttsPieceCount = 1;
  nativeControl.ttsSampleRate = 24_000;
  const { engine, events } = createHarness();

  const outcome = await engine.speak("All systems nominal.");
  assert.equal(outcome.ok, true, JSON.stringify(outcome));
  assert.equal(engine.state(), "idle");

  const chunks = events.filter(
    (event): event is Extract<VoiceEventPayload, { kind: "tts-chunk" }> => event.kind === "tts-chunk",
  );
  assert.equal(chunks.length, 3, "80 000 samples slice into 32 768-sample worker chunks");
  let totalSamples = 0;
  for (const [index, chunk] of chunks.entries()) {
    assert.equal(chunk.sequence, index, "sequence is dense and ordered");
    assert.equal(chunk.sampleRate, 24_000, "the MODEL-reported rate rides every chunk");
    assert.ok(chunk.pcm.byteLength <= 65_536, "renderer bound respected");
    totalSamples += chunk.pcm.byteLength / 2;
  }
  assert.equal(totalSamples, 80_000, "progress pieces feed everything — the whole-utterance audio object is NOT re-emitted");
  assert.ok(events.some((event) => event.kind === "tts-ended"));

  // bytes all even (PCM16): spot-check the final tail size.
  assert.equal(chunks[2].pcm.byteLength, 14_464 * 2);
});

test("multi-piece synthesis keeps totals exact across piece boundaries", async () => {
  nativeControl.ttsTotalSamples = 65_536;
  nativeControl.ttsPieceCount = 3; // pieces smaller than the chunk max
  const { engine, events } = createHarness();

  const outcome = await engine.speak("hello");
  assert.equal(outcome.ok, true, JSON.stringify(outcome));
  const total = events
    .filter((event): event is Extract<VoiceEventPayload, { kind: "tts-chunk" }> => event.kind === "tts-chunk")
    .reduce((sum, chunk) => sum + chunk.pcm.byteLength / 2, 0);
  assert.equal(total, 65_536);
});

test("speech is refused outside its legal states with VOICE_STATE", async () => {
  const { engine } = createHarness();
  await engine.arm();
  const outcome = await engine.speak("no");
  assert.equal(outcome.ok, false);
  if (!outcome.ok) assert.equal(outcome.code, "VOICE_STATE");
});

test("feed refuses oversized PCM without killing the session", async () => {
  const { engine } = createHarness();
  await engine.arm();
  await engine.begin();
  const refused = engine.feed(new Float32Array(VOICE_PROTOCOL_FEED_MAX_SAMPLES + 1));
  assert.equal(refused.ok, false);
  if (!refused.ok) assert.equal(refused.code, "BAD_PAYLOAD");
  assert.equal(engine.state(), "listening", "the session survives a refused chunk");
  assert.equal(engine.feed(new Float32Array(64)).ok, true);
});

test("status reports lazily-initialized engine with model presence", () => {
  const { engine } = createHarness();
  const status = engine.status();
  assert.equal(status.engineAvailable, false, "worker not spawned yet (lazy)");
  assert.equal(status.sttModelPresent, true);
  assert.equal(status.ttsModelPresent, true);
  assert.equal(status.vadModelPresent, false);
});

test("prepareModels wraps the manager outcome in the IPC envelope", async () => {
  const { engine } = createHarness();
  const prepared = await engine.prepareModels();
  assert.equal(prepared.ok, true);
  if (prepared.ok) {
    assert.equal(typeof prepared.data.bytes, "number");
  }
});
