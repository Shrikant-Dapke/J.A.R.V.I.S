#!/usr/bin/env node
/**
 * Phase 6.4-b voice smoke + benchmarks — REAL sherpa-onnx runtimes,
 * no mocks, no placeholder transcripts.
 *
 * 1. Ensures the pinned SEP models are installed (real network if missing)
 *    into `.voice-models` via the production model manager.
 * 2. Fetches the pinned smoke assets (test_wavs/*, trans.txt) with the
 *    same .part → verify → atomic-rename discipline.
 * 3. Boots the REAL worker core (`startVoiceWorker`) in this process —
 *    the exact code Electron forks into a utilityProcess — with the real
 *    native `sherpa-onnx-node` runtime.
 * 4. STT tier A (regression): recognizes the model's bundled test wavs
 *    and scores them against the `trans.txt` ground truth.
 * 4b. STT tier B (conversational): a fixed set of conversational commands
 *     is synthesized by the pinned Kokoro voices (rotating speakers,
 *     exact-by-construction ground truth), downsampled to 16 kHz, and run
 *     through the IDENTICAL streaming feed loop. This closes the bench's
 *     blind spot: model-bundled wavs only prove the model's own domain,
 *     not conversational speech. WER is reported alongside a whitespace/
 *     punctuation-insensitive CER (robust to tokenizer spacing
 *     differences when comparing models against the same audio).
 * 5. Legacy A/B: when the previous pinned STT model
 *    (stt-zipformer-en-2023-06-26-int8) is still installed on disk, the
 *    SAME synthesized audio is run through a second real worker against
 *    that model, and the two conversational WER/CER aggregates are
 *    reported side by side.
 * 6. TTS: synthesizes a sentence and measures load, first-audio latency,
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

/** Bounded streaming feed size — the exact production chunk bound. */
const FEED_MAX = protocol.VOICE_PROTOCOL_FEED_MAX_SAMPLES;

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

/**
 * Conversational scoring. Token-based WER inflates when two models
 * tokenize contractions/possessives differently ("WHAT S" vs "WHAT'S"),
 * so the comparative metric is a whitespace- and punctuation-insensitive
 * character error rate: both sides are stripped to bare alphanumerics
 * before a character-level edit distance. Word WER (on the same
 * normalization) is reported for continuity with the bundled-wav tier.
 */
const ALPHA_ONLY = /[^a-z0-9]+/gi;

function levenshtein(a, b) {
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = 0; i <= a.length; i += 1) dp[i][0] = i;
  for (let j = 0; j <= b.length; j += 1) dp[0][j] = j;
  for (let i = 1; i <= a.length; i += 1) {
    for (let j = 1; j <= b.length; j += 1) {
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
  }
  return dp[a.length][b.length];
}

function speechScores(reference, hypothesis) {
  const refTokens = reference.toLowerCase().replace(ALPHA_ONLY, " ").trim().split(/\s+/).filter(Boolean);
  const hypTokens = hypothesis.toLowerCase().replace(ALPHA_ONLY, " ").trim().split(/\s+/).filter(Boolean);
  const refChars = reference.toLowerCase().replace(ALPHA_ONLY, "");
  const hypChars = hypothesis.toLowerCase().replace(ALPHA_ONLY, "");
  const dp = Array.from({ length: refTokens.length + 1 }, () => new Array(hypTokens.length + 1).fill(0));
  for (let i = 0; i <= refTokens.length; i += 1) dp[i][0] = i;
  for (let j = 0; j <= hypTokens.length; j += 1) dp[0][j] = j;
  for (let i = 1; i <= refTokens.length; i += 1) {
    for (let j = 1; j <= hypTokens.length; j += 1) {
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (refTokens[i - 1] === hypTokens[j - 1] ? 0 : 1));
    }
  }
  return {
    distance: dp[refTokens.length][hypTokens.length],
    referenceWords: refTokens.length,
    charErrors: levenshtein(refChars, hypChars),
    referenceChars: refChars.length,
  };
}

/**
 * Conversational command set — the phrases users actually speak
 * (names, contractions, commands), rotated across pinned Kokoro
 * voices for speaker diversity. Ground truth is exact by construction.
 *
 * KNOWN LIMITATION (Stage 1, retained deliberately): the pinned Kokoro
 * lexicon silently SKIPS OOV words — measured skips: 'Jarvis' (3 of these
 * prompts) and 'what's' (1) are logged as
 * "OOV word skipped" yet remain in the ground truth, so audio literally
 * lacks words the reference claims. This depresses the floor for BOTH the
 * new and legacy models equally (see 2026-11-08 run: conversational WER
 * 22.8% new / 35.9% legacy, CER 12.1% / 16.4%). The 26.1% CER
 * improvement therefore FAILED the ≥50% acceptance target and was NOT
 * claimed as passed; real-mic validation by the user is the binding
 * gate. If a future run needs a cleaner synthetic floor, pick prompts
 * the lexicon can phonemize — do not post-hoc edit ground truth.
 */
const CONVERSATIONAL_PROMPTS = [
  { text: "Hey Jarvis, what's on my schedule today?", sid: 0 },
  { text: "Hello Jarvis, can you hear me clearly now?", sid: 1 },
  { text: "Jarvis, stop the music in the living room.", sid: 2 },
  { text: "Set a reminder to review the quarterly report tomorrow morning.", sid: 3 },
  { text: "What's the weather going to be like this weekend?", sid: 4 },
  { text: "Open the mission control dashboard and show me the system status.", sid: 5 },
  { text: "I'd like to reschedule my meeting with Sarah to Friday afternoon.", sid: 0 },
  { text: "Summarize the latest engineering updates from the team.", sid: 1 },
  { text: "Jarvis, dim the lights to twenty percent and start focus mode.", sid: 2 },
  { text: "How long until my next appointment?", sid: 3 },
];

/** The previously pinned STT model — kept on disk for A/B rollback runs. */
const LEGACY_STT_ID = "stt-zipformer-en-2023-06-26-int8";

function legacySttPaths() {
  const dir = path.join(MODELS_BASE, LEGACY_STT_ID);
  const files = {
    decoder: path.join(dir, "decoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
    encoder: path.join(dir, "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
    joiner: path.join(dir, "joiner-epoch-99-avg-1-chunk-16-left-128.int8.onnx"),
    tokens: path.join(dir, "tokens.txt"),
  };
  for (const file of Object.values(files)) {
    if (!fs.existsSync(file)) return null;
  }
  return files;
}

/** Downsample mono float32 (e.g. Kokoro output) to the STT input rate. */
function resampleTo16k(samples, sourceRate) {
  if (sourceRate === 16_000) return samples;
  const ratio = sourceRate / 16_000;
  const outLen = Math.floor(samples.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i += 1) {
    const pos = i * ratio;
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, samples.length - 1);
    out[i] = samples[lo] + (samples[hi] - samples[lo]) * (pos - lo);
  }
  return out;
}

/**
 * Synthesizes one prompt through the pinned Kokoro engine, reassembles
 * the ordered tts-chunk samples (sequence continuity is asserted), and
 * returns the full utterance plus the model-reported rate.
 */
async function synthesizeSpeech(worker, text, sid, requestId) {
  const numSpeakers = worker.ttsNumSpeakers ?? 64;
  if (!Number.isInteger(sid) || sid < 0 || sid >= numSpeakers) {
    throw new Error(`speaker id ${sid} outside the engine-reported range (0..${numSpeakers - 1})`);
  }
  const countBefore = worker.events.length;
  worker.send({ type: "tts-speak", text, sid, speed: 1, requestId });
  const first = await worker.waitFor((m) => m.type === "tts-chunk" || m.type === "worker-error", 30_000, countBefore);
  if (first.type === "worker-error") throw new Error(`tts-speak failed: ${first.message}`);
  const ended = await worker.waitFor((m) => m.type === "tts-ended" || m.type === "worker-error", 60_000, countBefore);
  if (ended.type === "worker-error") throw new Error(`tts-speak failed: ${ended.message}`);
  const chunks = worker.events
    .slice(countBefore)
    .filter((m) => m.type === "tts-chunk" && m.requestId === requestId)
    .sort((a, b) => a.sequence - b.sequence);
  for (let i = 0; i < chunks.length; i += 1) {
    if (chunks[i].sequence !== i) throw new Error(`tts chunk sequence gap at ${i} for request ${requestId}`);
  }
  const total = chunks.reduce((sum, c) => sum + c.samples.length, 0);
  const samples = new Float32Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    samples.set(chunk.samples, offset);
    offset += chunk.samples.length;
  }
  return { samples, sampleRate: ended.sampleRate, numSamples: ended.totalSamples };
}

/**
 * Runs a full streaming STT session over a mono 16 kHz buffer through the
 * REAL worker: stt-start → bounded stt-feed chunks → stt-end → stt-final.
 */
async function recognizeSamples(worker, mono) {
  rssTrack.sample();
  const started = performance.now();
  worker.send({ type: "stt-start" });
  for (let offset = 0; offset < mono.length; offset += FEED_MAX) {
    worker.send({ type: "stt-feed", samples: mono.subarray(offset, Math.min(offset + FEED_MAX, mono.length)) });
  }
  const countBefore = worker.events.length;
  worker.send({ type: "stt-end" });
  await worker.waitFor((m) => m.type === "stt-final" || m.type === "worker-error", 60_000, countBefore);
  const eventsAfterEnd = worker.events.slice(countBefore);
  const finalEvent = eventsAfterEnd.find((m) => m.type === "stt-final");
  const errorEvent = eventsAfterEnd.find((m) => m.type === "worker-error");
  if (!finalEvent) {
    throw new Error(`stt-end failed: ${errorEvent ? errorEvent.message : "no final produced"}`);
  }
  const elapsedMs = performance.now() - started;
  const audioSeconds = mono.length / 16_000;
  return {
    transcript: finalEvent.transcript,
    recognizeMs: elapsedMs,
    audioSeconds,
    rtf: elapsedMs / 1000 / audioSeconds,
  };
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
  say("STT tier A — bundled model test wavs (regression)");
  say(hr);
  const sttRows = [];
  const decodedWavs = {};
  for (const name of ["0.wav", "1.wav", "8k.wav"]) {
    const { samples: mono, sourceRate } = decodeWavToMono16k(smokeAssets[name]);
    decodedWavs[name] = mono;
    const result = await recognizeSamples(worker, mono);
    const reference = truth[name] ?? "";
    const { distance, referenceWords } = wordErrors(reference, result.transcript);
    sttRows.push({
      file: name,
      sourceRate,
      audioSeconds: result.audioSeconds,
      recognizeMs: result.recognizeMs,
      rtf: result.rtf,
      transcript: result.transcript,
      reference,
      errors: distance,
      referenceWords,
      wer: referenceWords > 0 ? distance / referenceWords : Infinity,
    });
    say(`  ✓ ${name} (src ${sourceRate} Hz → ${result.audioSeconds.toFixed(2)} s @16k, ${result.recognizeMs.toFixed(0)} ms, RTF ${result.rtf.toFixed(2)})`);
    say(`    REF: ${reference}`);
    say(`    HYP: ${result.transcript}`);
    say(`    ${distance} word error(s) / ${referenceWords} reference words`);
  }

  /* --------------------- STT conversational tier ------------------ */

  say(hr);
  say("STT tier B — conversational commands, Kokoro voices, exact ground truth");
  say(hr);
  const convRows = [];
  const convSamples = [];
  let convRequestId = 2; // 1 is reserved for the TTS bench below
  for (const prompt of CONVERSATIONAL_PROMPTS) {
    const synth = await synthesizeSpeech(worker, prompt.text, prompt.sid, convRequestId);
    convRequestId += 1;
    const mono = resampleTo16k(synth.samples, synth.sampleRate);
    convSamples.push(mono);
    const result = await recognizeSamples(worker, mono);
    const { distance, referenceWords, charErrors, referenceChars } = speechScores(prompt.text, result.transcript);
    convRows.push({
      prompt: prompt.text,
      sid: prompt.sid,
      audioSeconds: result.audioSeconds,
      recognizeMs: result.recognizeMs,
      rtf: result.rtf,
      transcript: result.transcript,
      reference: prompt.text,
      errors: distance,
      referenceWords,
      wer: referenceWords > 0 ? distance / referenceWords : Infinity,
      charErrors,
      referenceChars,
      cer: referenceChars > 0 ? charErrors / referenceChars : Infinity,
    });
    say(`  ✓ [${prompt.sid}] "${prompt.text}"`);
    say(`    HYP: ${result.transcript}`);
  say(`    WER ${distance}/${referenceWords} words, ${charErrors}/${referenceChars} chars`);
  }
  const convTotals = {
    wordErrors: convRows.reduce((sum, r) => sum + r.errors, 0),
    referenceWords: convRows.reduce((sum, r) => sum + r.referenceWords, 0),
    charErrors: convRows.reduce((sum, r) => sum + r.charErrors, 0),
    referenceChars: convRows.reduce((sum, r) => sum + r.referenceChars, 0),
    avgRtf: convRows.reduce((sum, r) => sum + r.rtf, 0) / convRows.length,
  };
  convTotals.wer = convTotals.wordErrors / convTotals.referenceWords;
  convTotals.cer = convTotals.charErrors / convTotals.referenceChars;
  say(`  ✓ conversational aggregate: WER ${(convTotals.wer * 100).toFixed(1)}%, CER ${(convTotals.cer * 100).toFixed(1)}%, avg RTF ${convTotals.avgRtf.toFixed(3)}`);


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

  /* --------------------- STT legacy A/B --------------------------- */

  say(hr);
  say("STT — legacy pinned model A/B (rollback comparison)");
  say(hr);
  const legacyPaths = legacySttPaths();
  let legacy = { present: false, id: LEGACY_STT_ID };
  if (legacyPaths) {
    const legacyWorker = bootLocalWorker();
    const legacyInitStarted = performance.now();
    legacyWorker.send({ type: "init", stt: legacyPaths });
    const legacyInit = await legacyWorker.waitFor((m) => m.type === "init-ok" || m.type === "init-fail", 120_000);
    if (legacyInit.type === "init-fail") {
      throw new Error(`legacy init-fail (${legacyInit.engine}): ${legacyInit.message}`);
    }
    const legacyInitMs = performance.now() - legacyInitStarted;
    say(`  ✓ legacy init-ok in ${legacyInitMs.toFixed(0)} ms (stt only)`);

    const legacyAggregate = () => ({ wordErrors: 0, referenceWords: 0, charErrors: 0, referenceChars: 0, rtfSum: 0 });
    const legacyConv = legacyAggregate();
    const legacyLibri = legacyAggregate();
    const legacyConvRows = [];
    for (let i = 0; i < CONVERSATIONAL_PROMPTS.length; i += 1) {
      const prompt = CONVERSATIONAL_PROMPTS[i];
      const result = await recognizeSamples(legacyWorker, convSamples[i]);
      const scores = speechScores(prompt.text, result.transcript);
      legacyConv.wordErrors += scores.distance;
      legacyConv.referenceWords += scores.referenceWords;
      legacyConv.charErrors += scores.charErrors;
      legacyConv.referenceChars += scores.referenceChars;
      legacyConv.rtfSum += result.rtf;
      legacyConvRows.push({
        prompt: prompt.text,
        transcript: result.transcript,
        rtf: result.rtf,
        ...scores,
      });
    }
    const legacyLibriRows = [];
    for (const name of ["0.wav", "1.wav", "8k.wav"]) {
      const result = await recognizeSamples(legacyWorker, decodedWavs[name]);
      const reference = truth[name] ?? "";
      const { distance, referenceWords } = wordErrors(reference, result.transcript);
      legacyLibri.wordErrors += distance;
      legacyLibri.referenceWords += referenceWords;
      legacyLibriRows.push({ file: name, transcript: result.transcript, rtf: result.rtf, errors: distance, referenceWords });
    }
    legacy = {
      present: true,
      id: LEGACY_STT_ID,
      initMs: legacyInitMs,
      conversational: {
        wer: legacyConv.wordErrors / legacyConv.referenceWords,
        cer: legacyConv.charErrors / legacyConv.referenceChars,
        avgRtf: legacyConv.rtfSum / CONVERSATIONAL_PROMPTS.length,
        rows: legacyConvRows,
      },
      bundled: {
        wer: legacyLibri.wordErrors / legacyLibri.referenceWords,
        rows: legacyLibriRows,
      },
    };
    say(`  ✓ legacy bundled-WER ${(legacy.bundled.wer * 100).toFixed(1)}%  conversational WER ${(legacy.conversational.wer * 100).toFixed(1)}%  CER ${(legacy.conversational.cer * 100).toFixed(1)}%  avg RTF ${legacy.conversational.avgRtf.toFixed(3)}`);
    say(`  ✓ improvement (new vs legacy, CER): ${((1 - convTotals.cer / legacy.conversational.cer) * 100).toFixed(1)}% lower error rate`);
  } else {
    say("  (legacy model directory not found under .voice-models — A/B comparison skipped)");
  }


  /* ----------------------------- Report --------------------------- */

  const report = {
    platform: `${process.platform}-${process.arch}`,
    node: process.version,
    stt: {
      model: STT_ZIPFORMER_EN_INT8.id,
      rows: sttRows,
      conversational: { rows: convRows, ...convTotals },
    },
    tts: {
      model: TTS_KOKORO_V11_INT8.id,
      sampleRate: tts.sampleRate,
      numSpeakers: initReply.ttsNumSpeakers,
      firstAudioMs: tts.firstAudioMs,
      rtf: tts.rtf,
      audioSeconds: tts.audioSeconds,
      chunkCount: tts.chunkCount,
    },
    legacy,
    initMs,
    rssBeforeBytes: process.memoryUsage().rss,
    peakRssBytes: rssTrack.peak(),
  };
  say(hr);
  say(`MEASURED SUMMARY (${process.platform}-${process.arch}, node ${process.version})`);
  say(hr);
  say(`  init (both legs): ${initMs.toFixed(0)} ms`);
  for (const row of sttRows) {
    say(`  STT bundled ${row.file}: ${row.recognizeMs.toFixed(0)} ms, RTF ${row.rtf.toFixed(2)}, ${row.errors}/${row.referenceWords} word errors`);
  }
  say(`  STT conversation (new ${STT_ZIPFORMER_EN_INT8.id}): WER ${(convTotals.wer * 100).toFixed(1)}%, CER ${(convTotals.cer * 100).toFixed(1)}%, avg RTF ${convTotals.avgRtf.toFixed(3)}`);
  if (legacy.present) {
    say(`  STT conversation (legacy ${legacy.id}):    WER ${(legacy.conversational.wer * 100).toFixed(1)}%, CER ${(legacy.conversational.cer * 100).toFixed(1)}%, avg RTF ${legacy.conversational.avgRtf.toFixed(3)}`);
    say(`  Acceptance: CER improvement ${((1 - convTotals.cer / legacy.conversational.cer) * 100).toFixed(1)}% (target ≥ 50%), max new-model RTF ${Math.max(...convRows.map((r) => r.rtf)).toFixed(3)} (target < 0.5)`);
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
