/**
 * Bounded main ⇄ voice-worker message protocol (Phase 6.4-b).
 *
 * The voice worker runs native speech inference in its own Electron
 * `utilityProcess`. This module is the ONLY way messages cross that
 * boundary: every main→worker request and every worker→main reply/event
 * is a plain JSON-cloneable structure (plus Float32Array PCM) with an
 * exact key set enforced here, so neither side can smuggle unbounded or
 * unexpected data through.
 *
 * Bounded-transport rules (mirrors shared/voice-engine.ts limits):
 *   - synthesized/recognized text ≤ VOICE_TEXT_MAX_LENGTH,
 *   - failure messages ≤ VOICE_FAILURE_MESSAGE_MAX_LENGTH,
 *   - outgoing synthesized PCM is chunked to ≤ VOICE_PCM_CHUNK_MAX_BYTES
 *     per message (the worker slices before posting),
 *   - incoming recognition feed is bounded too (a renderer-sized chunk),
 *   - sequence numbers are non-negative ints ≤ VOICE_SEQUENCE_MAX.
 */

import {
  VOICE_FAILURE_MESSAGE_MAX_LENGTH,
  VOICE_SEQUENCE_MAX,
  VOICE_TEXT_MAX_LENGTH,
  type PayloadValidation,
} from "./ipc-contract";
import { isVoiceErrorCode } from "./voice-engine";

/** 16-bit mono: 2 bytes per sample caps a transport chunk at 32_768. */
export const VOICE_PROTOCOL_FEED_MAX_SAMPLES = 65_536;
/** The worker emits synthesized PCM in fixed ≤32_768-sample chunks. */
export const VOICE_PROTOCOL_TTS_CHUNK_SAMPLES = 32_768;
/** Known-true STT input rate; TTS rate is always read from the model. */
export const VOICE_PROTOCOL_STT_SAMPLE_RATE = 16_000;

/* ------------------------------------------------------------------ */
/* Request side (main → worker)                                        */
/* ------------------------------------------------------------------ */

export type SttEnginePaths = {
  encoder: string;
  decoder: string;
  joiner: string;
  tokens: string;
};

export type TtsEnginePaths = {
  model: string;
  voices: string;
  tokens: string;
  dataDir: string;
  lexicon: string;
};

export type WorkerRequest =
  /** Lazy init (both legs optional — engine only loads what it needs). */
  | { type: "init"; stt?: SttEnginePaths; tts?: TtsEnginePaths }
  /** Open a recognition stream (STT leg must be initialized). */
  | { type: "stt-start" }
  /** Feed one bounded 16 kHz mono float32 chunk. */
  | { type: "stt-feed"; samples: Float32Array }
  /** End-of-stream: drain the decoder, return the final transcript. */
  | { type: "stt-end" }
  /** Synthesize bounded text; emits tts-chunk events then tts-ended. */
  | { type: "tts-speak"; text: string; sid: number; speed: number }
  /** Cancel in-flight synthesis at the next chunk boundary. */
  | { type: "tts-abort" }
  /** Graceful worker shutdown (releases native handles). */
  | { type: "shutdown" };

export function isSttEnginePaths(value: unknown): value is SttEnginePaths {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const v = value as Record<string, unknown>;
  const keys = Object.keys(v).sort();
  if (
    keys.length !== 4 ||
    keys[0] !== "decoder" ||
    keys[1] !== "encoder" ||
    keys[2] !== "joiner" ||
    keys[3] !== "tokens"
  ) {
    return false;
  }
  return (
    typeof v["encoder"] === "string" &&
    typeof v["decoder"] === "string" &&
    typeof v["joiner"] === "string" &&
    typeof v["tokens"] === "string"
  );
}

export function isTtsEnginePaths(value: unknown): value is TtsEnginePaths {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const v = value as Record<string, unknown>;
  const keys = Object.keys(v).sort();
  if (
    keys.length !== 5 ||
    keys[0] !== "dataDir" ||
    keys[1] !== "lexicon" ||
    keys[2] !== "model" ||
    keys[3] !== "tokens" ||
    keys[4] !== "voices"
  ) {
    return false;
  }
  return (
    typeof v["model"] === "string" &&
    typeof v["voices"] === "string" &&
    typeof v["tokens"] === "string" &&
    typeof v["dataDir"] === "string" &&
    typeof v["lexicon"] === "string"
  );
}

/**
 * Validate a main→worker request. Paths are only carried ON init and are
 * ordinary absolute paths inside the app's profile (they must pass the
 * isPlainString checks verbatim — no env interpolation is ever applied).
 */
export function validateWorkerRequest(value: unknown): PayloadValidation<WorkerRequest> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, reason: "Worker request must be an object." };
  }
  const type = (value as { type?: unknown }).type;
  const keys = Object.keys(value).sort();
  switch (type) {
    case "init": {
      // Sorted keys are ["stt", "tts", "type"] in some subset — anything
      // else (extra field, missing both legs) refuses.
      if (
        keys.length > 3 ||
        keys.some((key) => key !== "stt" && key !== "tts" && key !== "type")
      ) {
        return { ok: false, reason: "Worker 'init' accepts exactly stt and tts." };
      }
      const stt = keys.includes("stt") ? (value as { stt: unknown }).stt : undefined;
      const tts = keys.includes("tts") ? (value as { tts: unknown }).tts : undefined;
      if (stt !== undefined && !isSttEnginePaths(stt)) {
        return { ok: false, reason: "Worker 'init' stt paths are malformed." };
      }
      if (tts !== undefined && !isTtsEnginePaths(tts)) {
        return { ok: false, reason: "Worker 'init' tts paths are malformed." };
      }
      if (stt === undefined && tts === undefined) {
        return { ok: false, reason: "Worker 'init' needs at least one engine." };
      }
      return { ok: true, value: { type: "init", stt, tts } };
    }
    case "stt-start": {
      if (keys.length !== 1) {
        return { ok: false, reason: "Worker 'stt-start' takes no extra fields." };
      }
      return { ok: true, value: { type: "stt-start" } };
    }
    case "stt-feed": {
      if (keys.length !== 2 || keys[0] !== "samples" || keys[1] !== "type") {
        return { ok: false, reason: "Worker 'stt-feed' accepts exactly samples." };
      }
      const samples = (value as { samples: unknown }).samples;
      if (
        !(samples instanceof Float32Array) ||
        samples.length === 0 ||
        samples.length > VOICE_PROTOCOL_FEED_MAX_SAMPLES
      ) {
        return {
          ok: false,
          reason: `Worker 'stt-feed' samples must be a Float32Array of 1..${VOICE_PROTOCOL_FEED_MAX_SAMPLES}.`,
        };
      }
      return { ok: true, value: { type: "stt-feed", samples } };
    }
    case "stt-end": {
      if (keys.length !== 1) {
        return { ok: false, reason: "Worker 'stt-end' takes no extra fields." };
      }
      return { ok: true, value: { type: "stt-end" } };
    }
    case "tts-speak": {
      // Sorted keys: [sid, speed, text, type].
      if (keys.length !== 4 || keys[0] !== "sid" || keys[1] !== "speed" || keys[2] !== "text") {
        return { ok: false, reason: "Worker 'tts-speak' accepts exactly text, sid, speed." };
      }
      const text = (value as { text: unknown }).text;
      const sid = (value as { sid: unknown }).sid;
      const speed = (value as { speed: unknown }).speed;
      if (typeof text !== "string" || text.trim().length === 0 || text.length > VOICE_TEXT_MAX_LENGTH) {
        return {
          ok: false,
          reason: `Worker 'tts-speak' text must be non-empty and ≤ ${VOICE_TEXT_MAX_LENGTH} characters.`,
        };
      }
      if (typeof sid !== "number" || !Number.isInteger(sid) || sid < 0 || sid > 1023) {
        return { ok: false, reason: "Worker 'tts-speak' sid must be an integer in 0..1023." };
      }
      if (
        typeof speed !== "number" ||
        !Number.isFinite(speed) ||
        speed < 0.5 ||
        speed > 3
      ) {
        return { ok: false, reason: "Worker 'tts-speak' speed must be within 0.5..3." };
      }
      return { ok: true, value: { type: "tts-speak", text, sid, speed } };
    }
    case "tts-abort": {
      if (keys.length !== 1) {
        return { ok: false, reason: "Worker 'tts-abort' takes no extra fields." };
      }
      return { ok: true, value: { type: "tts-abort" } };
    }
    case "shutdown": {
      if (keys.length !== 1) {
        return { ok: false, reason: "Worker 'shutdown' takes no extra fields." };
      }
      return { ok: true, value: { type: "shutdown" } };
    }
    default:
      return { ok: false, reason: `Worker request type is not documented.` };
  }
}

/* ------------------------------------------------------------------ */
/* Response side (worker → main)                                       */
/* ------------------------------------------------------------------ */

export type WorkerResponse =
  /** Immediate “renderer-independent worker is alive” beacon. */
  | { type: "worker-ready" }
  | { type: "init-ok"; stt: boolean; tts: boolean; ttsSampleRate: number; ttsNumSpeakers: number }
  | { type: "init-fail"; engine: "stt" | "tts"; message: string }
  /** Emitted while a recognition stream is open. May be empty text. */
  | { type: "stt-partial"; transcript: string }
  /** Emitted once per 'stt-end'. Non-empty, bounded. */
  | { type: "stt-final"; transcript: string }
  /** One bounded PCM chunk of synthesized audio (float32 samples). */
  | { type: "tts-chunk"; sequence: number; samples: Float32Array; sampleRate: number }
  | { type: "tts-ended"; totalSamples: number; sampleRate: number; aborted: boolean }
  /** Request-level failures carry fixed, non-sensitive copy. */
  | { type: "worker-error"; scope: "stt" | "tts" | "engine"; message: string; code?: string };

export function isSttInitOk(value: WorkerResponse): value is Extract<WorkerResponse, { type: "init-ok" }> {
  return value.type === "init-ok";
}

/**
 * Validate a worker→main reply. Every reply must be one of the
 * documented kinds, with bounded text/PCM and exact key sets.
 */
export function validateWorkerResponse(value: unknown): PayloadValidation<WorkerResponse> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, reason: "Worker response must be an object." };
  }
  const type = (value as { type?: unknown }).type;
  const keys = Object.keys(value).sort();
  switch (type) {
    case "worker-ready": {
      if (keys.length !== 1) return { ok: false, reason: "Bad worker-ready." };
      return { ok: true, value: { type: "worker-ready" } };
    }
    case "init-ok": {
      // Sorted: [stt, tts, ttsNumSpeakers, ttsSampleRate, type]
      if (keys.length !== 5 || keys[0] !== "stt" || keys[1] !== "tts" || keys[3] !== "ttsSampleRate") {
        return { ok: false, reason: "Bad init-ok." };
      }
      const v = value as {
        stt: unknown;
        tts: unknown;
        ttsSampleRate: unknown;
        ttsNumSpeakers: unknown;
      };
      if (typeof v.stt !== "boolean" || typeof v.tts !== "boolean") {
        return { ok: false, reason: "Bad init-ok flags." };
      }
      // The sample rate is ALWAYS the model-reported value — a rate of 0
      // is the HONEST marker for "the tts leg was not loaded" and is
      // legal only alongside tts:false; a loaded tts leg must report a
      // real rate, never a fabricated one.
      const ttsAbsenceRate = v.tts === false;
      const rateOk =
        (ttsAbsenceRate &&
          v.ttsSampleRate === 0) ||
        (!ttsAbsenceRate &&
          typeof v.ttsSampleRate === "number" &&
          Number.isInteger(v.ttsSampleRate) &&
          v.ttsSampleRate >= 8000 &&
          v.ttsSampleRate <= 96_000);
      if (!rateOk) {
        return { ok: false, reason: "Bad init-ok sample rate." };
      }
      const speakersOk =
        (ttsAbsenceRate &&
          v.ttsNumSpeakers === 0) ||
        (!ttsAbsenceRate &&
          typeof v.ttsNumSpeakers === "number" &&
          Number.isInteger(v.ttsNumSpeakers) &&
          v.ttsNumSpeakers >= 1 &&
          v.ttsNumSpeakers <= 1024);
      if (!speakersOk) {
        return { ok: false, reason: "Bad init-ok speaker count." };
      }
      return {
        ok: true,
        value: {
          type: "init-ok",
          stt: v.stt,
          tts: v.tts,
          // The boolean guards above narrowed these to numbers; the
          // conditional form makes TS's narrowing lose them.
          ttsSampleRate: ttsAbsenceRate ? 0 : (v.ttsSampleRate as number),
          ttsNumSpeakers: ttsAbsenceRate ? 0 : (v.ttsNumSpeakers as number),
        },
      };
    }
    case "init-fail": {
      // Sorted keys: [engine, message, type].
      if (keys.length !== 3 || keys[0] !== "engine" || keys[2] !== "type") {
        return { ok: false, reason: "Bad init-fail." };
      }
      const v = value as { engine: unknown; message: unknown };
      if (v.engine !== "stt" && v.engine !== "tts") {
        return { ok: false, reason: "Bad init-fail engine." };
      }
      const message = typeof v.message === "string" ? v.message : "";
      if (message.length === 0 || message.length > VOICE_FAILURE_MESSAGE_MAX_LENGTH) {
        return { ok: false, reason: "Bad init-fail message." };
      }
      return { ok: true, value: { type: "init-fail", engine: v.engine, message } };
    }
    case "stt-partial": {
      if (keys.length !== 2 || keys[0] !== "transcript") {
        return { ok: false, reason: "Bad stt-partial." };
      }
      const transcript = (value as { transcript: unknown }).transcript;
      if (typeof transcript !== "string" || transcript.length > VOICE_TEXT_MAX_LENGTH) {
        return { ok: false, reason: "Bad stt-partial transcript." };
      }
      return { ok: true, value: { type: "stt-partial", transcript } };
    }
    case "stt-final": {
      if (keys.length !== 2 || keys[0] !== "transcript") {
        return { ok: false, reason: "Bad stt-final." };
      }
      const transcript = (value as { transcript: unknown }).transcript;
      if (typeof transcript !== "string" || transcript.trim().length === 0 || transcript.length > VOICE_TEXT_MAX_LENGTH) {
        return { ok: false, reason: "Bad stt-final transcript." };
      }
      return { ok: true, value: { type: "stt-final", transcript } };
    }
    case "tts-chunk": {
      // Sorted keys: [sampleRate, samples, sequence, type]
      // ('R' in sampleRate sorts before 's' in samples).
      if (
        keys.length !== 4 ||
        keys[0] !== "sampleRate" ||
        keys[1] !== "samples" ||
        keys[2] !== "sequence"
      ) {
        return { ok: false, reason: "Bad tts-chunk." };
      }
      const v = value as { sequence: unknown; samples: unknown; sampleRate: unknown };
      const samples = v.samples;
      if (
        !(samples instanceof Float32Array) ||
        samples.length === 0 ||
        samples.length > VOICE_PROTOCOL_TTS_CHUNK_SAMPLES
      ) {
        return { ok: false, reason: "Bad tts-chunk samples." };
      }
      if (
        typeof v.sequence !== "number" ||
        !Number.isInteger(v.sequence) ||
        v.sequence < 0 ||
        v.sequence > VOICE_SEQUENCE_MAX
      ) {
        return { ok: false, reason: "Bad tts-chunk sequence." };
      }
      if (
        typeof v.sampleRate !== "number" ||
        !Number.isInteger(v.sampleRate) ||
        v.sampleRate < 8000 ||
        v.sampleRate > 96_000
      ) {
        return { ok: false, reason: "Bad tts-chunk sample rate." };
      }
      return {
        ok: true,
        value: { type: "tts-chunk", sequence: v.sequence, samples, sampleRate: v.sampleRate },
      };
    }
    case "tts-ended": {
      if (keys.length !== 4 || keys[2] !== "totalSamples") {
        return { ok: false, reason: "Bad tts-ended." };
      }
      const v = value as { totalSamples: unknown; sampleRate: unknown; aborted: unknown };
      if (
        typeof v.totalSamples !== "number" ||
        !Number.isInteger(v.totalSamples) ||
        v.totalSamples < 0
      ) {
        return { ok: false, reason: "Bad tts-ended samples." };
      }
      if (
        typeof v.sampleRate !== "number" ||
        !Number.isInteger(v.sampleRate) ||
        v.sampleRate < 8000 ||
        v.sampleRate > 96_000
      ) {
        return { ok: false, reason: "Bad tts-ended sample rate." };
      }
      if (typeof v.aborted !== "boolean") {
        return { ok: false, reason: "Bad tts-ended flag." };
      }
      return {
        ok: true,
        value: { type: "tts-ended", totalSamples: v.totalSamples, sampleRate: v.sampleRate, aborted: v.aborted },
      };
    }
    case "worker-error": {
      // Sorted keys: [code (optional), message, scope] + discriminator.
      if (keys.length !== 3 && keys.length !== 4) {
        return { ok: false, reason: "Bad worker-error." };
      }
      const v = value as { scope: unknown; message: unknown; code?: unknown };
      if (v.scope !== "stt" && v.scope !== "tts" && v.scope !== "engine") {
        return { ok: false, reason: "Bad worker-error scope." };
      }
      let code: string | undefined;
      if (keys.length === 4) {
        if (keys[0] !== "code") return { ok: false, reason: "Bad worker-error code key." };
        if (typeof v.code !== "string" || !isVoiceErrorCode(v.code)) {
          return { ok: false, reason: "Bad worker-error code." };
        }
        code = v.code;
      }
      const message = typeof v.message === "string" ? v.message : "";
      if (message.length === 0 || message.length > VOICE_FAILURE_MESSAGE_MAX_LENGTH) {
        return { ok: false, reason: "Bad worker-error message." };
      }
      return {
        ok: true,
        value: { type: "worker-error", scope: v.scope, message, ...(code ? { code } : {}) },
      };
    }
    default:
      return { ok: false, reason: "Worker response type is not documented." };
  }
}
