#!/usr/bin/env node
/**
 * Phase 6.4-b voice smoke + benchmarks — REAL sherpa-onnx runtimes, no
 * mocks, no synthetic audio, no placeholder transcripts.
 *
 * 1. Ensures the pinned SEP models are installed (real network if missing)
 *    into `.voice-models` via the production model manager.
 * 2. Fetches the pinned smoke assets (test_wavs/*, trans.txt) with the
 *    same .part → verify → atomic-rename discipline.
 * 3. Boots the REAL worker core (`startVoiceWorker`) in this process —
 *    the exact code Electron forks into a utilityProcess — with the real
 *    native `sherpa-onnx-node` runtime.
 * 4. STT: recognizes the LibriSpeech test wavs and scores them against
 *    the `trans.txt` ground truth (word error counts).
 * 5. TTS: synthesizes a sentence and measures load, first-audio latency,
 *    RTF, and peak RSS. The sample rate is always the MODEL-REPORTED one.
 *
 * Usage:  node scripts/voice-smoke.mjs [--download-only]
 * Exit:   0 on honest success, non-zero on any failure.
 */

import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { pipeline } from "node:stream/promises";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));

/* Production builds — the same code Electron ships. */
const manifest = require(path.join(here, "..", "dist", "electron", "main", "voice", "model-manifest.js"));
const managerModule = require(path.join(here, "..", "dist", "electron", "main", "voice", "model-manager.js"));
const workerModule = require(path.join(here, "..", "dist", "electron", "main", "voice", "voice-worker.js"));
const protocol = require(path.join(here, "..", "dist", "shared", "voice-protocol.js"));

const { STT_ZIPFORMER_EN_INT8, TTS_KOKORO_V11_INT8 } = manifest;
const { createModelManager, createNodeModelFs, createNodeSha256, createNodeDownloader, createNodeDiskSpace, isSherpaRuntimePresent } = {
  ...managerModule,
  isSherpaRuntimePresent: workerModule.isSherpaRuntimePresent,
};
const { startVoiceWorker } = workerModule;

const MODELS_BASE = path.join(here, "..", ".voice-models");
const MODELS_DIR = (model) => path.join(MODELS_BASE, model.id);

const hr = "=".repeat(64);
const say = (line) => process.stdout.write(`${line}\n`);

function sha256Of(absolute) {
  return new Promise((resolve, reject) => {
    const hash = createHash("sha256");
    const stream = createReadStream(absolute);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve(hash.digest("hex")));
  });
}

async function fetchSmokeAsset(entry) {
  const absolute = path.join(MODELS_DIR(STT_ZIPFORMER_EN_INT8), entry.path);
  try {
    const stat = fs.statSync(absolute);
    if (stat.isFile() && stat.size === entry.size) {
      const digest = await sha256Of(absolute);
      if (digest === entry.sha256) return absolute;
    }
  } catch {
    // absent or unreadable — download below
  }
  const downloader = createNodeDownloader();
  const temp = path.join(MODELS_BASE, ".tmp", path.basename(entry.path) + ".part");
  fs.mkdirSync(path.dirname(temp), { recursive: true });
  fs.mkdirSync(path.dirname(absolute), { recursive: true });
  say(`  ↓ ${entry.path} (${(entry.size / 1024).toFixed(1)} KiB)`);
  await downloader.download(entry.url, temp);
  const written = fs.statSync(temp).size;
  if (written !== entry.size) {
    fs.rmSync(temp, { force: true });
    throw new Error(`smoke asset size mismatch for ${entry.path}: ${written} of ${entry.size}`);
  }
  const digest = await sha256Of(temp);
  if (digest !== entry.sha256) {
    fs.rmSync(temp, { force: true });
    throw new Error(`smoke asset checksum mismatch for ${entry.path}`);
  }
  fs.renameSync(temp, absolute);
  return absolute;
}

async function resolveSmokeAssets() {
  say(hr);
  say("Smoke assets (pinned, opt-in)");
  say(hr);
  const resolved = {};
  for (const entry of STT_ZIPFORMER_EN_INT8.smokeFiles) {
    resolved[path.basename(entry.path)] = await fetchSmokeAsset(entry);
  }
  say("  ✓ all pinned smoke assets verified");
  return resolved;
}

async function prepareModels() {
  say(hr);
  say("Models (userData-equivalent dir: .voice-models)");
  say(hr);
  const manager = createModelManager({
    baseDir: MODELS_BASE,
    fs: createNodeModelFs(),
    downloader: createNodeDownloader(),
    hash: createNodeSha256(),
    disk: createNodeDiskSpace(),
  });
  const result = await manager.prepareModels((progress) => {
    say(`  [${progress.phase}] ${progress.doneFiles}/${progress.totalFiles} ${progress.file}`);
  });
  if (!result.ok) throw new Error(`Prepare failed: ${result.error}`);
  say(`  ✓ prepared: downloaded=${result.downloaded} verifiedFromCache=${result.verifiedFromCache} bytes=${result.bytes}`);
  const status = manager.status();
  say(`  ✓ stt present: ${status.sttPresent}  tts present: ${status.ttsPresent}`);
  if (!status.sttPresent || !status.ttsPresent) {
    throw new Error(`Model files missing: stt=${status.sttMissing.length} tts=${status.ttsMissing.length}`);
  }
  return status;
}

/* ------------------------------------------------------------------ */
/* Local worker host (in-process, utilityProcess-like task delivery)   */
/* ------------------------------------------------------------------ */

function bootLocalWorker() {
  const events = [];
  const waiters = [];

  const deliver = (message) => {
    const validated = protocol.validateWorkerResponse(message);
    if (!validated.ok) throw new Error(`worker emitted invalid reply: ${validated.reason}`);
    const value = validated.value;
    events.push(value);
    for (let i = 0; i < waiters.length; i += 1) {
      if (waiters[i].match(value)) {
        clearTimeout(waiters[i].timer);
        const waiter = waiters.splice(i, 1)[0];
        waiter.resolve(value);
        return;
      }
    }
  };

  const api = { send: null, events };
  const host = {
    post: (message) => {
      // utilityProcess delivers messages as tasks.
      setImmediate(() => deliver(message));
    },
    listen: (onRequest) => {
      api.send = (request) => {
        const validated = protocol.validateWorkerRequest(request);
        if (!validated.ok) throw new Error(`refused request: ${validated.reason}`);
        setImmediate(() => onRequest(validated.value));
      };
    },
  };
  startVoiceWorker(host); // lazy-loads the REAL native module

  api.waitFor = (match, timeoutMs, sinceIndex = 0) =>
    new Promise((resolve, reject) => {
      // Only events after sinceIndex are eligible — past events from an
      // earlier request must never satisfy a new wait.
      const existing = events.find((event, index) => index >= sinceIndex && match(event));
      if (existing) return resolve(existing);
      const entry = {
        match,
        timer: setTimeout(() => {
          const index = waiters.indexOf(entry);
          if (index >= 0) waiters.splice(index, 1);
          reject(new Error("worker wait timeout"));
        }, timeoutMs),
        resolve,
      };
      waiters.push(entry);
    });

  return api;
}

/* ------------------------------------------------------------------ */
/* WAV decode (PCM16/PCM8 → 16 kHz mono float32)                       */
/* ------------------------------------------------------------------ */

/**
 * LibriSpeech smoke wavs are mono PCM16 @ 16 kHz; 8k.wav is @ 8 kHz and
 * is linearly upsampled (honest, documented resampling for smoke only).
 */
function decodeWavToMono16k(absolute) {
  const buffer = fs.readFileSync(absolute);
  if (buffer.toString("latin1", 0, 4) !== "RIFF" || buffer.toString("latin1", 8, 12) !== "WAVE") {
    throw new Error(`not a RIFF/WAVE file: ${path.basename(absolute)}`);
  }
  let offset = 12;
  let sampleRate = 0;
  let channels = 0;
  let bits = 0;
  let audioFormat = 0;
  let data = null;
  while (offset + 8 <= buffer.length) {
    const id = buffer.toString("latin1", offset, offset + 4);
    const size = buffer.readUInt32LE(offset + 4);
    const body = offset + 8;
    if (id === "fmt ") {
      audioFormat = buffer.readUInt16LE(body + 0);
      channels = buffer.readUInt16LE(body + 2);
      sampleRate = buffer.readUInt32LE(body + 4);
      bits = buffer.readUInt16LE(body + 14);
    } else if (id === "data") {
      data = buffer.subarray(body, body + size);
    }
    offset = body + size + (size % 2);
  }
  if (!data || audioFormat !== 1 || (bits !== 16 && bits !== 8)) {
    throw new Error(`unsupported wav (format=${audioFormat} bits=${bits})`);
  }
  const bytesPerSample = bits / 8;
  const frameCount = Math.floor(data.length / (bytesPerSample * channels));
  const mono = new Float32Array(frameCount);
  for (let i = 0; i < frameCount; i += 1) {
    let sum = 0;
    for (let ch = 0; ch < channels; ch += 1) {
      const pos = (i * channels + ch) * bytesPerSample;
      sum += bits === 16 ? data.readInt16LE(pos) / 32_768 : (data.readUInt8(pos) - 128) / 128;
    }
    mono[i] = sum / channels;
  }
  if (sampleRate === 16_000) return { samples: mono, sourceRate: sampleRate };
  const ratio = sampleRate / 16_000;
  const outLen = Math.floor(mono.length / ratio);
  const resampled = new Float32Array(outLen);
  for (let i = 0; i < outLen; i += 1) {
    const pos = i * ratio;
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, mono.length - 1);
    resampled[i] = mono[lo] + (mono[hi] - mono[lo]) * (pos - lo);
  }
  return { samples: resampled, sourceRate: sampleRate };
}

function parseGroundTruth(transPath) {
  const truth = {};
  for (const line of fs.readFileSync(transPath, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed.length === 0) continue;
    const space = trimmed.indexOf(" ");
    const id = space >= 0 ? trimmed.slice(0, space) : trimmed;
    truth[id] = (space >= 0 ? trimmed.slice(space + 1) : "").trim();
  }
  return truth;
}

function wordErrors(reference, hypothesis) {
  const r = reference.toLowerCase().split(/\s+/).filter(Boolean);
  const h = hypothesis.toLowerCase().split(/\s+/).filter(Boolean);
  const dp = Array.from({ length: r.length + 1 }, () => new Array(h.length + 1).fill(0));
  for (let i = 0; i <= r.length; i += 1) dp[i][0] = i;
  for (let j = 0; j <= h.length; j += 1) dp[0][j] = j;
  for (let i = 1; i <= r.length; i += 1) {
    for (let j = 1; j <= h.length; j += 1) {
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (r[i - 1] === h[j - 1] ? 0 : 1));
    }
  }
  return { distance: dp[r.length][h.length], referenceWords: r.length };
}

/* ------------------------------------------------------------------ */
/* Smoke + benchmarks                                                  */
/* ------------------------------------------------------------------ */

const rssTrack = (() => {
  let peak = process.memoryUsage().rss;
  return {
    peak: () => peak,
    sample: () => {
      peak = Math.max(peak, process.memoryUsage().rss);
      return peak;
    },
  };
})();

async function runSmoke(downloadOnly) {
  const status = await prepareModels();
  if (downloadOnly) {
    say(hr);
    say("download-only → done.");
    return 0;
  }

  const smokeAssets = await resolveSmokeAssets();
  const truth = parseGroundTruth(smokeAssets["trans.txt"]);

  say(hr);
  say("Native runtime");
  say(hr);
  const runtimePresent = isSherpaRuntimePresent();
  say(`  sherpa-onnx-node require: ${runtimePresent ? "OK" : "MISSING"}`);
  if (!runtimePresent) throw new Error("The native sherpa-onnx runtime could not be required.");

  say(hr);
  say("Worker boot + initialize (REAL native inference)");
  say(hr);
  const worker = bootLocalWorker();
  const initStarted = performance.now();
  worker.send({ type: "init", stt: status.sttPaths, tts: status.ttsPaths });
  const initReply = await worker.waitFor((m) => m.type === "init-ok" || m.type === "init-fail", 120_000);
  const initMs = performance.now() - initStarted;
  if (initReply.type === "init-fail") {
    throw new Error(`init-fail (${initReply.engine}): ${initReply.message}`);
  }
  say(`  ✓ init-ok in ${initMs.toFixed(0)} ms — stt:${initReply.stt} tts:${initReply.tts}`);
  say(`  ✓ TTS model-reported rate: ${initReply.ttsSampleRate} Hz (${initReply.ttsNumSpeakers} speaker(s))`);
  if (!initReply.stt || !initReply.tts) throw new Error("A required engine leg failed to initialize.");

  /* ------------------------------- STT ---------------------------- */

  say(hr);
  say("STT — streaming Zipformer int8, pinned LibriSpeech smoke wavs");
  say(hr);
  const feedMax = protocol.VOICE_PROTOCOL_FEED_MAX_SAMPLES;
  const sttRows = [];
  for (const name of ["0.wav", "1.wav", "8k.wav"]) {
    const { samples: mono, sourceRate } = decodeWavToMono16k(smokeAssets[name]);
    rssTrack.sample();
    const started = performance.now();
    worker.send({ type: "stt-start" });
    for (let offset = 0; offset < mono.length; offset += feedMax) {
      worker.send({ type: "stt-feed", samples: mono.subarray(offset, Math.min(offset + feedMax, mono.length)) });
    }
    const countBefore = worker.events.length;
    worker.send({ type: "stt-end" });
    await worker.waitFor((m) => m.type === "stt-final" || m.type === "worker-error", 60_000, countBefore);
    const eventsAfterEnd = worker.events.slice(countBefore);
    const finalEvent = eventsAfterEnd.find((m) => m.type === "stt-final");
    const errorEvent = eventsAfterEnd.find((m) => m.type === "worker-error");
    if (!finalEvent) {
      throw new Error(`stt-end failed for ${name}: ${errorEvent ? errorEvent.message : "no final produced"}`);
    }
    const elapsedMs = performance.now() - started;
    const reference = truth[name] ?? "";
    const { distance, referenceWords } = wordErrors(reference, finalEvent.transcript);
    const audioSeconds = mono.length / 16_000;
    sttRows.push({
      file: name,
      sourceRate,
      audioSeconds,
      recognizeMs: elapsedMs,
      rtf: elapsedMs / 1000 / audioSeconds,
      transcript: finalEvent.transcript,
      reference,
      errors: distance,
      referenceWords,
      wer: referenceWords > 0 ? distance / referenceWords : Infinity,
    });
    say(`  ✓ ${name} (src ${sourceRate} Hz → ${audioSeconds.toFixed(2)} s @16k, ${elapsedMs.toFixed(0)} ms, RTF ${(elapsedMs / 1000 / audioSeconds).toFixed(2)})`);
    say(`    REF: ${reference}`);
    say(`    HYP: ${finalEvent.transcript}`);
    say(`    ${distance} word error(s) / ${referenceWords} reference words`);
  }

  /* ------------------------------- TTS ---------------------------- */

  say(hr);
  say("TTS — Kokoro-82M int8");
  say(hr);
  const ttsText = "The bridge is down. Standing by for your next instruction.";
  const ttsRequestId = 1;
  const speakStarted = performance.now();
  const countBefore = worker.events.length;
  worker.send({ type: "tts-speak", text: ttsText, sid: 0, speed: 1, requestId: ttsRequestId });
  await worker.waitFor((m) => m.type === "tts-chunk" || m.type === "worker-error", 30_000, countBefore);
  const eventsSoFar = worker.events.slice(countBefore);
  const firstErrorEarly = eventsSoFar.find((m) => m.type === "worker-error");
  if (firstErrorEarly) throw new Error(`tts-speak failed early: ${firstErrorEarly.message}`);
  const firstAudioMs = performance.now() - speakStarted;
  await worker.waitFor((m) => m.type === "tts-ended" || m.type === "worker-error", 120_000, countBefore);
  const allEvents = worker.events.slice(countBefore);
  const ended = allEvents.find((m) => m.type === "tts-ended");
  const lateError = allEvents.find((m) => (m.type === "worker-error" ? m : null) && !ended);
  if (!ended) throw new Error(`tts-speak failed: ${lateError ? lateError.message : "no tts-ended"}`);
  const firstChunk = allEvents.find((m) => m.type === "tts-chunk");
  const chunkCount = allEvents.filter((m) => m.type === "tts-chunk").length;
  if (!firstChunk) throw new Error("tts-speak produced no audio chunks.");
  const synthMs = performance.now() - speakStarted;
  const audioSeconds = ended.totalSamples / ended.sampleRate;
  const tts = {
    requestId: ended.requestId,
    sampleRate: ended.sampleRate,
    totalSamples: ended.totalSamples,
    audioSeconds,
    chunkCount,
    firstAudioMs,
    synthMs,
    rtf: synthMs / 1000 / audioSeconds,
  };
  if (ended.requestId !== ttsRequestId) throw new Error("tts-ended request identity mismatch.");
  if (firstChunk.requestId !== ttsRequestId) throw new Error("tts-chunk request identity mismatch.");
  say(`  ✓ ${ended.totalSamples} samples @ ${ended.sampleRate} Hz → ${audioSeconds.toFixed(2)} s audio`);
  say(`  ✓ first audio in ${firstAudioMs.toFixed(0)} ms; total wall ${synthMs.toFixed(0)} ms → RTF ${tts.rtf.toFixed(3)}`);
  say(`  ✓ ${chunkCount} chunk(s); model-reported rate rides every chunk (${firstChunk.sampleRate} Hz)`);

  /* ----------------------------- Report --------------------------- */

  const report = {
    platform: `${process.platform}-${process.arch}`,
    node: process.version,
    stt: { model: STT_ZIPFORMER_EN_INT8.id, rows: sttRows },
    tts: {
      model: TTS_KOKORO_V11_INT8.id,
      sampleRate: tts.sampleRate,
      numSpeakers: initReply.ttsNumSpeakers,
      firstAudioMs: tts.firstAudioMs,
      rtf: tts.rtf,
      audioSeconds: tts.audioSeconds,
      chunkCount: tts.chunkCount,
    },
    initMs,
    rssBeforeBytes: process.memoryUsage().rss,
    peakRssBytes: rssTrack.peak(),
  };
  say(hr);
  say(`MEASURED SUMMARY (${process.platform}-${process.arch}, node ${process.version})`);
  say(hr);
  say(`  init (both legs): ${initMs.toFixed(0)} ms`);
  for (const row of sttRows) {
    say(`  STT ${row.file}: ${row.recognizeMs.toFixed(0)} ms, RTF ${row.rtf.toFixed(2)}, ${row.errors}/${row.referenceWords} word errors`);
  }
  say(`  TTS: first audio ${tts.firstAudioMs.toFixed(0)} ms, RTF ${tts.rtf.toFixed(3)} (${tts.synthMs.toFixed(0)} ms → ${tts.audioSeconds.toFixed(2)} s @ ${tts.sampleRate} Hz)`);
  say(`  peak RSS: ${(report.peakRssBytes / 1024 / 1024).toFixed(1)} MB`);
  fs.writeFileSync(path.join(here, "..", ".voice-bench.json"), JSON.stringify(report, null, 2));
  say("  → full report written to .voice-bench.json");
  return 0;
}

/* ------------------------------------------------------------------ */

const downloadOnly = process.argv.includes("--download-only");
runSmoke(downloadOnly)
  .then((code) => process.exit(code))
  .catch((error) => {
    say(`✖ smoke failed: ${error instanceof Error ? error.message : String(error)}`);
    if (process.env.VOICE_SMOKE_DEBUG === "1" && error instanceof Error && error.stack) {
      say(error.stack);
    }
    process.exit(1);
  });
