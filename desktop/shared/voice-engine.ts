/**
 * Pure voice interaction state machine (Phase 6.4-a) — zero React or
 * Electron imports so every invariant here is plain-Node testable, the
 * same discipline as `chat-engine.ts`.
 *
 * Honest-by-construction rules:
 *  - the machine NEVER produces audio or transcript text itself; it only
 *    accepts/records text and PCM chunk metadata that an actual engine
 *    (Phase 6.4-b) reported,
 *  - `cancelled` and `failed` are terminal-until-reset states: recovery
 *    to `idle` requires an explicit `reset` (acknowledgment), never an
 *    automatic retry,
 *  - every dispatch returns a typed result; illegal or malformed actions
 *    are refused and leave the state exactly as it was.
 */

import {
  VOICE_FAILURE_MESSAGE_MAX_LENGTH,
  VOICE_PCM_CHUNK_MAX_BYTES,
  VOICE_SEQUENCE_MAX,
  VOICE_TEXT_MAX_LENGTH,
  type PayloadValidation,
} from "./ipc-contract";

/* ------------------------------------------------------------------ */
/* States                                                              */
/* ------------------------------------------------------------------ */

export type VoiceState =
  /** No session; TTS may still start from here (speaking is a session of its own). */
  | "idle"
  /** Microphone armed, capture not yet begun (PTT pressed, waiting for first audio). */
  | "armed"
  /** Capture streaming; the engine may emit partial transcripts. */
  | "listening"
  /** Capture ended; awaiting the engine's final transcript. */
  | "transcribing"
  /** Authoritative final transcript recorded; composer holds it. */
  | "transcript_ready"
  /** TTS synthesis/playback of a reply text is in progress. */
  | "speaking"
  /** A session was cancelled by the user; requires reset. */
  | "cancelled"
  /** An engine failure was recorded; requires reset. */
  | "failed";

export const VOICE_STATES: ReadonlyArray<VoiceState> = [
  "idle",
  "armed",
  "listening",
  "transcribing",
  "transcript_ready",
  "speaking",
  "cancelled",
  "failed",
];

/* ------------------------------------------------------------------ */
/* Failure taxonomy (closed set, bounded copy)                         */
/* ------------------------------------------------------------------ */

export type VoiceErrorCode =
  /** The OS/browser refused microphone access (permission denied). */
  | "MICROPHONE_DENIED"
  /** Speech model files are absent or failed verification. */
  | "MODEL_MISSING"
  /** The speech-to-text engine errored during capture or decoding. */
  | "STT_FAILURE"
  /** The text-to-speech engine errored during synthesis or playback. */
  | "TTS_FAILURE"
  /** The voice runtime is not installed/loaded in this build. */
  | "ENGINE_UNAVAILABLE"
  /** The recording or synthesis was cancelled at the user's request. */
  | "RECORDING_CANCELLED";

export const VOICE_ERROR_CODES: ReadonlyArray<VoiceErrorCode> = [
  "MICROPHONE_DENIED",
  "MODEL_MISSING",
  "STT_FAILURE",
  "TTS_FAILURE",
  "ENGINE_UNAVAILABLE",
  "RECORDING_CANCELLED",
];

const VOICE_ERROR_CODES_SET: ReadonlySet<string> = new Set(VOICE_ERROR_CODES);

export function isVoiceErrorCode(value: unknown): value is VoiceErrorCode {
  return typeof value === "string" && VOICE_ERROR_CODES_SET.has(value);
}

/** Human, honest one-liners per voice failure kind. */
export const VOICE_ERROR_COPY: Record<VoiceErrorCode, string> = {
  MICROPHONE_DENIED:
    "Microphone access is denied — allow the microphone for JARVIS in Windows settings and try again.",
  MODEL_MISSING: "Voice models are not installed yet — use the model manager to add them.",
  STT_FAILURE: "Speech recognition failed — try again; nothing was sent to the chat.",
  TTS_FAILURE: "Speech synthesis failed — the reply is still visible in the transcript.",
  ENGINE_UNAVAILABLE:
    "The voice engine is not available yet — it arrives with the speech runtime phase.",
  RECORDING_CANCELLED: "You cancelled the recording — nothing was captured or sent.",
};

export type VoiceFailure = {
  code: VoiceErrorCode;
  message: string;
};

/* ------------------------------------------------------------------ */
/* Actions                                                             */
/* ------------------------------------------------------------------ */

export type VoiceActionType =
  | "arm"
  | "begin_capture"
  | "end_capture"
  | "partial"
  | "finalize"
  | "speak_begin"
  | "speak_end"
  | "cancel"
  | "fail"
  | "reset";

export type VoiceAction =
  | { type: "arm" }
  | { type: "begin_capture" }
  | { type: "end_capture" }
  /** Interim recognition text (may be empty; bounded). */
  | { type: "partial"; transcript: string }
  /** The engine's final transcript (non-empty, bounded). */
  | { type: "finalize"; transcript: string }
  | { type: "speak_begin" }
  | { type: "speak_end" }
  | { type: "cancel" }
  | { type: "fail"; code: VoiceErrorCode; message: string }
  /** Acknowledge cancelled/failed and return to idle. */
  | { type: "reset" };

/** Which of these actions is legal in each state (documentation AND test oracle). */
export const VOICE_LEGAL_ACTIONS: Readonly<
  Record<VoiceState, ReadonlyArray<VoiceActionType>>
> = {
  idle: ["arm", "speak_begin"],
  armed: ["begin_capture", "cancel", "fail"],
  listening: ["end_capture", "partial", "cancel", "fail"],
  transcribing: ["finalize", "cancel", "fail"],
  transcript_ready: ["arm", "speak_begin", "cancel", "fail"],
  speaking: ["speak_end", "cancel", "fail"],
  cancelled: ["reset"],
  failed: ["reset"],
};

/** Documented, human-oriented transition table (mirrors the machine). */
export const VOICE_TRANSITIONS: Readonly<
  Record<VoiceActionType, ReadonlyArray<VoiceState>>
> = {
  arm: ["idle", "transcript_ready"],
  begin_capture: ["armed"],
  end_capture: ["listening"],
  partial: ["listening"],
  finalize: ["transcribing"],
  speak_begin: ["idle", "transcript_ready"],
  speak_end: ["speaking"],
  cancel: ["armed", "listening", "transcribing", "transcript_ready", "speaking"],
  fail: ["armed", "listening", "transcribing", "transcript_ready", "speaking"],
  reset: ["cancelled", "failed"],
};

export type VoiceTransition =
  | { ok: true; state: VoiceState }
  | { ok: false; state: VoiceState; reason: string };

/* ------------------------------------------------------------------ */
/* Action validation (shape + bounds; independent of the state)         */
/* ------------------------------------------------------------------ */

function isBoundedText(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length <= maxLength;
}

/**
 * Validate an action's payload bounds before the state check so garbage
 * never mutates the machine. Reason messages are fixed and non-sensitive.
 */
export function validateVoiceAction(action: unknown): PayloadValidation<VoiceAction> {
  if (
    action === null ||
    typeof action !== "object" ||
    Array.isArray(action) ||
    typeof (action as { type?: unknown }).type !== "string"
  ) {
    return { ok: false, reason: "Voice action must be an object with a type." };
  }
  const type = (action as { type: string }).type;
  const keys = Object.keys(action).sort();
  switch (type) {
    case "arm":
    case "begin_capture":
    case "end_capture":
    case "speak_begin":
    case "speak_end":
    case "cancel":
    case "reset":
      if (keys.length !== 1) {
        return { ok: false, reason: `Voice action '${type}' takes no extra fields.` };
      }
      return { ok: true, value: { type } as VoiceAction };
    case "partial": {
      if (keys.length !== 2 || keys[0] !== "transcript") {
        return { ok: false, reason: "Voice action 'partial' accepts exactly transcript." };
      }
      const transcript = (action as { transcript: unknown }).transcript;
      if (!isBoundedText(transcript, VOICE_TEXT_MAX_LENGTH)) {
        return {
          ok: false,
          reason: `Voice partial must be a string of at most ${VOICE_TEXT_MAX_LENGTH} characters.`,
        };
      }
      return { ok: true, value: { type: "partial", transcript } };
    }
    case "finalize": {
      if (keys.length !== 2 || keys[0] !== "transcript") {
        return { ok: false, reason: "Voice action 'finalize' accepts exactly transcript." };
      }
      const transcript = (action as { transcript: unknown }).transcript;
      if (
        typeof transcript !== "string" ||
        transcript.trim().length === 0 ||
        transcript.length > VOICE_TEXT_MAX_LENGTH
      ) {
        return {
          ok: false,
          reason:
            `Voice final transcript must be non-empty and at most ${VOICE_TEXT_MAX_LENGTH} characters.`,
        };
      }
      return { ok: true, value: { type: "finalize", transcript } };
    }
    case "fail": {
      // Sorted keys include 'type' itself: [code, message, type].
      if (keys.length !== 3 || keys[0] !== "code" || keys[1] !== "message" || keys[2] !== "type") {
        return { ok: false, reason: "Voice action 'fail' accepts exactly code and message." };
      }
      const { code, message } = action as { code: unknown; message: unknown };
      if (!isVoiceErrorCode(code)) {
        return { ok: false, reason: "Voice failure code must be from the documented set." };
      }
      if (typeof message !== "string") {
        return { ok: false, reason: "Voice failure message must be a string." };
      }
      const bounded = message.trim();
      if (bounded.length === 0 || message.length > VOICE_FAILURE_MESSAGE_MAX_LENGTH) {
        return {
          ok: false,
          reason: `Voice failure message must be non-empty and at most ${VOICE_FAILURE_MESSAGE_MAX_LENGTH} characters.`,
        };
      }
      return { ok: true, value: { type: "fail", code, message } };
    }
    default:
      return { ok: false, reason: `Voice action '${type.slice(0, 32)}' is not documented.` };
  }
}

/* ------------------------------------------------------------------ */
/* The machine                                                         */
/* ------------------------------------------------------------------ */

export type VoiceMachine = {
  getState: () => VoiceState;
  /** Latest accepted final transcript or null (cleared on re-arm). */
  getTranscript: () => string | null;
  /** Latest accepted interim transcript or null (cleared to null on finalize). */
  getPartial: () => string | null;
  /** Failure recorded on `fail` (carried until `reset`). */
  getFailure: () => VoiceFailure | null;
  dispatch: (action: unknown) => VoiceTransition;
};

/**
 * Deterministic, side-effect-free reducer. Dispatch order matters:
 * exact action-shape validation first (refuse garbage), then the
 * state-specfic legality check (refuse illegal transitions), then the
 * state/data mutation.
 */
export function createVoiceMachine(): VoiceMachine {
  let state: VoiceState = "idle";
  let transcript: string | null = null;
  let partial: string | null = null;
  let failure: VoiceFailure | null = null;

  const refuse = (reason: string): VoiceTransition => ({ ok: false, state, reason });

  const dispatch = (rawAction: unknown): VoiceTransition => {
    const validated = validateVoiceAction(rawAction);
    if (!validated.ok) return refuse(validated.reason);
    const action = validated.value;

    const legal = VOICE_LEGAL_ACTIONS[state];
    if (!legal.includes(action.type)) {
      return refuse(
        `Voice action '${action.type}' is not legal in state '${state}'.`,
      );
    }

    switch (action.type) {
      case "arm":
        // Re-arming after a transcript discards it: a new utterance starts.
        state = "armed";
        transcript = null;
        partial = null;
        break;
      case "begin_capture":
        state = "listening";
        break;
      case "end_capture":
        state = "transcribing";
        break;
      case "partial":
        partial = action.transcript;
        break; // stays listening
      case "finalize":
        transcript = action.transcript;
        partial = null;
        state = "transcript_ready";
        break;
      case "speak_begin":
        state = "speaking";
        break; // synthesis source text lives outside the machine
      case "speak_end":
        state = "idle";
        break;
      case "cancel":
        state = "cancelled";
        partial = null;
        failure = {
          code: "RECORDING_CANCELLED",
          message: VOICE_ERROR_COPY["RECORDING_CANCELLED"],
        };
        break;
      case "fail":
        // Terminal until reset; whatever failed is recorded honestly.
        state = "failed";
        failure = { code: action.code, message: action.message };
        break;
      case "reset":
        state = "idle";
        failure = null;
        partial = null;
        transcript = null;
        break;
    }
    return { ok: true, state };
  };

  return {
    getState: () => state,
    getTranscript: () => transcript,
    getPartial: () => partial,
    getFailure: () => failure,
    dispatch,
  };
}

/* ------------------------------------------------------------------ */
/* Main → renderer voice events (jarvis:voice-events)                  */
/* ------------------------------------------------------------------ */

/**
 * The single event channel carries ONE discriminated union. Events only
 * ever travel main → renderer (webContents.send in later phases), which
 * is why `kind` does not live in IPC_CHANNELS/PRELOAD_METHODS — but the
 * payload contract is still shared and strict so both sides stay honest.
 * Like the actions, events include their discriminator key in the sorted
 * key arithmetic ("kind", versus the action's "type").
 */
export type VoiceEventPayload =
  | { kind: "state"; state: VoiceState }
  /** Interim recognition text (may be empty; bounded). */
  | { kind: "partial"; transcript: string }
  /** Authoritative final transcript (non-empty; bounded). */
  | { kind: "final"; transcript: string }
  /** One streamed synthesis chunk: raw mono PCM16 + sequence + rate. */
  | { kind: "tts-chunk"; pcm: ArrayBuffer; sequence: number; sampleRate: number }
  | { kind: "tts-ended" }
  /** Engine failure (closed code set; bounded message). */
  | { kind: "failure"; code: VoiceErrorCode; message: string };

export const VOICE_EVENT_KINDS: ReadonlyArray<VoiceEventPayload["kind"]> = [
  "state",
  "partial",
  "final",
  "tts-chunk",
  "tts-ended",
  "failure",
];

export function isVoiceState(value: unknown): value is VoiceState {
  return (
    typeof value === "string" &&
    (VOICE_STATES as ReadonlyArray<string>).includes(value)
  );
}

/**
 * Strict per-kind validation for a main→renderer voice event. Extra
 * keys, unknown kinds, oversized text, empty/oversized PCM, and
 * non-integer sequences all refuse — same discipline as the commands.
 */
export function validateVoiceEventPayload(
  value: unknown,
): PayloadValidation<VoiceEventPayload> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    typeof (value as { kind?: unknown }).kind !== "string"
  ) {
    return { ok: false, reason: "Voice event must be an object with a kind." };
  }
  const kind = (value as { kind: string }).kind;
  const keys = Object.keys(value).sort();
  switch (kind) {
    case "state": {
      if (keys.length !== 2 || keys[0] !== "kind" || keys[1] !== "state") {
        return { ok: false, reason: "Voice event 'state' accepts exactly state." };
      }
      const state = (value as { state: unknown }).state;
      if (!isVoiceState(state)) {
        return { ok: false, reason: "Voice event state must be from the documented set." };
      }
      return { ok: true, value: { kind: "state", state } };
    }
    case "partial":
    case "final": {
      if (keys.length !== 2 || keys[0] !== "kind" || keys[1] !== "transcript") {
        return { ok: false, reason: `Voice event '${kind}' accepts exactly transcript.` };
      }
      const transcript = (value as { transcript: unknown }).transcript;
      if (
        typeof transcript !== "string" ||
        transcript.length > VOICE_TEXT_MAX_LENGTH
      ) {
        return {
          ok: false,
          reason:
            `Voice event transcript must be a string of at most ${VOICE_TEXT_MAX_LENGTH} characters.`,
        };
      }
      if (kind === "final" && transcript.trim().length === 0) {
        return { ok: false, reason: "Voice event 'final' transcript must not be empty." };
      }
      return kind === "partial"
        ? { ok: true, value: { kind: "partial", transcript } }
        : { ok: true, value: { kind: "final", transcript } };
    }
    case "tts-chunk": {
      // Sorted keys: [kind, pcm, sampleRate, sequence].
      if (
        keys.length !== 4 ||
        keys[0] !== "kind" ||
        keys[1] !== "pcm" ||
        keys[2] !== "sampleRate" ||
        keys[3] !== "sequence"
      ) {
        return {
          ok: false,
          reason: "Voice event 'tts-chunk' accepts exactly pcm, sequence, and sampleRate.",
        };
      }
      const { pcm, sequence, sampleRate } = value as {
        pcm: unknown;
        sequence: unknown;
        sampleRate: unknown;
      };
      if (!(pcm instanceof ArrayBuffer)) {
        return { ok: false, reason: "Voice event PCM chunk must be an ArrayBuffer." };
      }
      if (pcm.byteLength === 0 || pcm.byteLength > VOICE_PCM_CHUNK_MAX_BYTES) {
        return {
          ok: false,
          reason:
            `Voice event PCM chunk must carry 1..${VOICE_PCM_CHUNK_MAX_BYTES} bytes.`,
        };
      }
      if (
        typeof sampleRate !== "number" ||
        !Number.isInteger(sampleRate) ||
        sampleRate < 8000 ||
        sampleRate > 96_000
      ) {
        return { ok: false, reason: "Voice event sample rate must be an integer in 8000..96000 Hz." };
      }
      if (
        typeof sequence !== "number" ||
        !Number.isInteger(sequence) ||
        sequence < 0 ||
        sequence > VOICE_SEQUENCE_MAX
      ) {
        return {
          ok: false,
          reason: `Voice event sequence must be an integer in 0..${VOICE_SEQUENCE_MAX}.`,
        };
      }
      return { ok: true, value: { kind: "tts-chunk", pcm, sequence, sampleRate } };
    }
    case "tts-ended": {
      if (keys.length !== 1 || keys[0] !== "kind") {
        return { ok: false, reason: "Voice event 'tts-ended' takes no extra fields." };
      }
      return { ok: true, value: { kind: "tts-ended" } };
    }
    case "failure": {
      // Sorted keys: [code, kind, message].
      if (keys.length !== 3 || keys[0] !== "code" || keys[2] !== "message") {
        return { ok: false, reason: "Voice event 'failure' accepts exactly code and message." };
      }
      const { code, message } = value as { code: unknown; message: unknown };
      if (!isVoiceErrorCode(code)) {
        return { ok: false, reason: "Voice failure code must be from the documented set." };
      }
      if (typeof message !== "string") {
        return { ok: false, reason: "Voice failure message must be a string." };
      }
      const bounded = message.trim();
      if (bounded.length === 0 || message.length > VOICE_FAILURE_MESSAGE_MAX_LENGTH) {
        return {
          ok: false,
          reason:
            `Voice failure message must be non-empty and at most ${VOICE_FAILURE_MESSAGE_MAX_LENGTH} characters.`,
        };
      }
      return { ok: true, value: { kind: "failure", code, message } };
    }
    default:
      return {
        ok: false,
        reason: `Voice event kind '${kind.slice(0, 32)}' is not documented.`,
      };
  }
}
