/**
 * Main-process voice engine (Phase 6.4-b).
 *
 * Owns the shared voice state machine (`createVoiceMachine`, the exact
 * implementation 6.4-a tests pin) plus the ONLY worker transport, so:
 *   - every user-visible state truth flows through the pinned machine,
 *   - native inference isolates in an Electron `utilityProcess` via an
 *     injected transport factory (tests use fake transports, the app
 *     uses voice-transport.ts),
 *   - every failure maps to the closed, copy-pinned VoiceErrorCode set.
 *
 * Engine rules:
 *   - models initialize LAZILY: nothing loads until the first capture
 *     or synthesis command needs it, and nothing is ever DOWNLOADED
 *     here (only `prepareModels`, always user-driven),
 *   - worker replies/events are validated through shared/voice-protocol
 *     and renderer events through `validateVoiceEventPayload` before
 *     emission — nothing unvetted crosses toward the renderer,
 *   - crash/exit of the worker marks the session failed honestly; the
 *     next `arm` acknowledges (reset) and respawns — no silent retries.
 */

import { createVoiceMachine, isVoiceErrorCode, validateVoiceEventPayload, type VoiceErrorCode, type VoiceEventPayload, type VoiceFailure, type VoiceState } from "../../../shared/voice-engine";
import {
  ipcError,
  ipcOk,
  VOICE_PCM_CHUNK_MAX_BYTES,
  VOICE_PLAYBACK_REQUEST_ID_MAX,
  VOICE_TTS_CHANNELS,
  VOICE_TTS_ENCODING,
  VOICE_TTS_MAX_SAMPLES,
  type IpcErrorCode,
  type IpcResult,
} from "../../../shared/ipc-contract";
import { validateWorkerRequest, validateWorkerResponse, type WorkerRequest, type WorkerResponse } from "../../../shared/voice-protocol";
import type { VoiceModelManager } from "./model-manager";

/* ------------------------------------------------------------------ */
/* Injectable transport                                                */
/* ------------------------------------------------------------------ */

export type VoiceWorkerTransport = {
  send: (request: WorkerRequest) => void;
  onResponse: (callback: (response: WorkerResponse) => void) => void;
  onExit: (callback: (info: { exitCode: number | null }) => void) => void;
  kill: () => void;
  running: () => boolean;
};

export type TransportFactory = () => Promise<VoiceWorkerTransport>;

export type VoiceEngineDeps = {
  models: VoiceModelManager;
  createTransport: TransportFactory;
  /** Bounded waits; generous defaults (model load is slow and honest). */
  spawnTimeoutMs?: number;
  initTimeoutMs?: number;
  streamTimeoutMs?: number;
  synthTimeoutMs?: number;
  trace?: (message: string) => void;
};

/* ------------------------------------------------------------------ */
/* Results and views                                                   */
/* ------------------------------------------------------------------ */

export type VoiceEngineOutcome =
  | { ok: true; state: VoiceState }
  | { ok: false; code: IpcErrorCode; message: string };

export type VoiceEngineStatus = {
  engineAvailable: boolean;
  sttModelPresent: boolean;
  ttsModelPresent: boolean;
  vadModelPresent: false;
  message: string;
};

export type VoiceEngineEventSubscriber = (event: VoiceEventPayload) => void;

const FAILURE_TEXT_MAX = 512;
const STATUS_MESSAGE_MAX = 200;
const TRANSCRIPT_MAX = 2000;

const clamp = (message: string, max: number): string =>
  message.length <= max ? message : message.slice(0, max);

/** Native error messages must never carry absolute paths toward the UI. */
const SCRUB_PATH_RE = /(?:[A-Za-z]:)?[\\/](?:[\w\-. ~]+[\\/]){1,8}[\w\-. ~]+/g;
export const scrubPathsForUi = (message: string): string =>
  clamp(message.replace(SCRUB_PATH_RE, "…/"), FAILURE_TEXT_MAX);

/* ------------------------------------------------------------------ */
/* The engine                                                          */
/* ------------------------------------------------------------------ */

export type VoiceEngine = {
  status: () => VoiceEngineStatus;
  prepareModels: (
    onProgress?: (info: {
      file: string;
      doneFiles: number;
      totalFiles: number;
      phase: "download" | "verify" | "install";
    }) => void,
  ) => Promise<IpcResult<{ downloaded: number; bytes: number }>>;
  state: () => VoiceState;
  failure: () => VoiceFailure | null;
  /** User-facing command surface — each maps through the machine. */
  arm: () => Promise<VoiceEngineOutcome>;
  begin: () => Promise<VoiceEngineOutcome>;
  feed: (samples: Float32Array) => VoiceEngineOutcome;
  end: () => Promise<IpcResult<{ state: VoiceState; transcript: string }>>;
  cancel: () => VoiceEngineOutcome;
  speak: (text: string, sid?: number, speed?: number, requestId?: number) => Promise<VoiceEngineOutcome>;
  stop: () => VoiceEngineOutcome;
  /** Acknowledge terminal states (failed/cancelled → idle). */
  acknowledge: () => VoiceEngineOutcome;
  dispose: () => void;
  subscribe: (subscriber: VoiceEngineEventSubscriber) => () => void;
};

type SessionPhase = "speaking" | "stt";

export function createVoiceEngine(deps: VoiceEngineDeps): VoiceEngine {
  const machine = createVoiceMachine();
  const spawnTimeout = deps.spawnTimeoutMs ?? 10_000;
  const initTimeout = deps.initTimeoutMs ?? 240_000;
  const streamTimeout = deps.streamTimeoutMs ?? 15_000;
  const synthTimeout = deps.synthTimeoutMs ?? 300_000;
  const trace = deps.trace ?? (() => undefined);

  const subscribers = new Set<VoiceEngineEventSubscriber>();
  let transport: VoiceWorkerTransport | null = null;
  /** Which reply the engine currently waits for — strictly single flight. */
  type Awaiting = { kind: "worker-ready" | "init" | "session"; resolve: (value: WorkerResponse | null) => void };
  let awaiting: Awaiting | null = null;
  let awaitingTimer: ReturnType<typeof setTimeout> | null = null;
  /** The worker knows these legs; set as init-ok confirms them. */
  let session: SessionPhase | null = null;
  /** Identity of the active renderer-owned synthesis request. */
  let speakingRequestId: number | null = null;

  /* ------------------------- event plumbing ----------------------- */

  const emit = (event: VoiceEventPayload): void => {
    const validated = validateVoiceEventPayload(event);
    if (!validated.ok) {
      // Renderer-event validation failure is an engine bug — drop it.
      trace(`[voice] refused invalid event: ${validated.reason}`);
      return;
    }
    for (const subscriber of subscribers) subscriber(validated.value);
  };

  const emitState = (state: VoiceState): void => emit({ kind: "state", state });

  const failMachine = (code: VoiceErrorCode, message: string): void => {
    const bounded = clamp(message, FAILURE_TEXT_MAX);
    const before = machine.getState();
    const result = machine.dispatch({ type: "fail", code, message: bounded });
    if (result.ok) {
      emitState(result.state);
      emit({ kind: "failure", code, message: bounded });
      session = null;
    } else if (before !== "idle" && before !== "failed") {
      trace(`[voice] could not record failure in ${before}: ${result.reason}`);
    }
  };

  const applyAction = (action: unknown): VoiceEngineOutcome => {
    const result = machine.dispatch(action);
    if (!result.ok) {
      return { ok: false, code: "VOICE_STATE", message: clamp(result.reason, STATUS_MESSAGE_MAX) };
    }
    emitState(result.state);
    if (machine.getState() === "cancelled") {
      const failure = machine.getFailure();
      if (failure) emit({ kind: "failure", code: failure.code, message: failure.message });
    }
    return { ok: true, state: result.state };
  };

  /** State guard BEFORE any side effect (models, worker, transport). */
  const requireState = (
    ...states: VoiceState[]
  ): Extract<VoiceEngineOutcome, { ok: false }> | null =>
    states.includes(machine.getState())
      ? null
      : {
          ok: false,
          code: "VOICE_STATE",
          message: `Command requires state ${states.join(" or ")}; engine is '${machine.getState()}'.`,
        };

  /* ------------------------- transport lifecycle ------------------ */

  const settleAwaiting = (value: WorkerResponse | null): void => {
    if (awaitingTimer) {
      clearTimeout(awaitingTimer);
      awaitingTimer = null;
    }
    const current = awaiting;
    awaiting = null;
    current?.resolve(value);
  };

  const engineFailureText = (): string => machine.getFailure()?.message ?? "Voice engine failure.";

  const killTransport = (): void => {
    transport?.kill();
    transport = null;
  };

  const handleTransportExit = (_info: { exitCode: number | null }): void => {
    transport = null;
    settleAwaiting(null);
    const state = machine.getState();
    if (state === "failed" || state === "cancelled" || state === "idle") {
      if (state !== "idle") trace(`[voice] worker exited during ${state}`);
      return;
    }
    failMachine(
      state === "speaking" ? "TTS_FAILURE" : "STT_FAILURE",
      "The voice engine worker stopped unexpectedly.",
    );
  };

  const ensureTransport = async (): Promise<VoiceEngineOutcome | { kind: "transport"; transport: VoiceWorkerTransport }> => {
    if (transport) return { kind: "transport", transport };
    try {
      transport = await deps.createTransport();
    } catch (error) {
      return {
        ok: false,
        code: "ENGINE_UNAVAILABLE",
        message: clamp(error instanceof Error ? error.message : String(error), STATUS_MESSAGE_MAX),
      };
    }
    transport.onExit(handleTransportExit);
    transport.onResponse(routeResponse);
    // Await the beacon once per spawn, bounded.
    const ready = await new Promise<WorkerResponse | null>((resolve) => {
      awaiting = { kind: "worker-ready", resolve };
      awaitingTimer = setTimeout(() => resolve(null), spawnTimeout);
    });
    if (!ready || ready.type !== "worker-ready") {
      killTransport();
      return { ok: false, code: "ENGINE_UNAVAILABLE", message: "The voice worker did not become ready." };
    }
    return { kind: "transport", transport };
  };

  const isTransportOk = (
    value: Awaited<ReturnType<typeof ensureTransport>>,
  ): value is { kind: "transport"; transport: VoiceWorkerTransport } => (value as { kind?: string }).kind === "transport";

  /* ------------------------- response routing --------------------- */

  type InitOk = Extract<WorkerResponse, { type: "init-ok" }>;
  type TtsChunk = Extract<WorkerResponse, { type: "tts-chunk" }>;
  type WorkerError = Extract<WorkerResponse, { type: "worker-error" }>;

  const emitTtsChunk = (response: TtsChunk): void => {
    const sampleCount = response.samples.length;
    if (sampleCount === 0) return;
    if (sampleCount * 2 > VOICE_PCM_CHUNK_MAX_BYTES) {
      trace(`[voice] refused oversized tts chunk (${sampleCount} samples)`);
      return;
    }
    const pcm = new ArrayBuffer(sampleCount * 2);
    const view = new DataView(pcm);
    for (let i = 0; i < sampleCount; i++) {
      const clamped = Math.max(-1, Math.min(1, response.samples[i]));
      view.setInt16(i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    }
    emit({
      kind: "tts-chunk",
      pcm,
      requestId: response.requestId,
      sequence: response.sequence,
      sampleRate: response.sampleRate,
      channels: VOICE_TTS_CHANNELS,
      encoding: VOICE_TTS_ENCODING,
    });
  };

  const routeWorkerError = (response: WorkerError): void => {
    if (
      response.requestId !== undefined &&
      (session !== "speaking" || speakingRequestId !== response.requestId)
    ) {
      // A late error from a cancelled/superseded synthesis must not poison
      // the newer playback request.
      return;
    }
    const fallback: VoiceErrorCode =
      response.scope === "stt" ? "STT_FAILURE" : response.scope === "tts" ? "TTS_FAILURE" : "ENGINE_UNAVAILABLE";
    const code: VoiceErrorCode =
      response.code !== undefined && response.code !== null && isVoiceErrorCode(response.code)
        ? (response.code as VoiceErrorCode)
        : fallback;
    session = null;
    settleAwaiting(null);
    failMachine(code, scrubPathsForUi(response.message));
  };

  const routeResponse = (raw: unknown): void => {
    const validated = validateWorkerResponse(raw);
    if (!validated.ok) {
      trace(`[voice] refused malformed worker response: ${validated.reason}`);
      return;
    }
    const response = validated.value;
    switch (response.type) {
      case "worker-ready":
        if (awaiting?.kind === "worker-ready") settleAwaiting(response);
        return;
      case "init-ok":
      case "init-fail":
        if (awaiting?.kind === "init") settleAwaiting(response);
        return;
      case "stt-partial": {
        // Interim text is always a state event toward the renderer.
        if (response.transcript.length > 0) {
          emit({ kind: "partial", transcript: response.transcript.slice(0, TRANSCRIPT_MAX) });
        }
        return;
      }
      case "stt-final":
        if (awaiting?.kind === "session" && session === "stt") settleAwaiting(response);
        return;
      case "tts-chunk":
        if (session !== "speaking" || speakingRequestId !== response.requestId) return;
        emitTtsChunk(response);
        return;
      case "tts-ended":
        if (
          awaiting?.kind === "session" &&
          session === "speaking" &&
          speakingRequestId === response.requestId
        ) {
          settleAwaiting(response);
        }
        return;
      case "worker-error":
        routeWorkerError(response);
        return;
      default:
        return;
    }
  };

  /* ------------------------- init & commands ---------------------- */

  const dispatchAwaiting = async (
    kind: Awaiting["kind"],
    phase: SessionPhase | null,
    request: WorkerRequest,
    timeoutMs: number,
  ): Promise<WorkerResponse | null> => {
    const t = transport;
    if (!t) return null;
    return await new Promise<WorkerResponse | null>((resolve) => {
      session = phase;
      awaiting = { kind, resolve };
      awaitingTimer = setTimeout(() => resolve(null), timeoutMs);
      try {
        t.send(request);
      } catch (error) {
        resolve(null);
        trace(`[voice] send failed: ${error instanceof Error ? error.message : String(error)}`);
      }
    });
  };

  const loadStt = async (): Promise<InitOk | { error: string }> => {
    const started = await ensureTransport();
    if (!isTransportOk(started)) {
      return { error: started.ok ? "Voice worker failed to start." : started.message };
    }
    const models = deps.models.status();
    if (!models.sttPresent || !models.sttPaths) {
      return { error: "Speech-to-text model files are missing — prepare the models first." };
    }
    const response = await dispatchAwaiting("init", null, { type: "init", stt: models.sttPaths }, initTimeout);
    if (!response) return { error: "Speech recognition did not initialize in time." };
    if (response.type === "init-fail" && response.engine === "stt") {
      return { error: scrubPathsForUi(response.message) };
    }
    if (response.type === "init-ok" && response.stt) return response;
    return { error: "Speech recognition init failed." };
  };

  const loadTts = async (): Promise<InitOk | { error: string }> => {
    const started = await ensureTransport();
    if (!isTransportOk(started)) {
      return { error: started.ok ? "Voice worker failed to start." : started.message };
    }
    const models = deps.models.status();
    if (!models.ttsPresent || !models.ttsPaths) {
      return { error: "Text-to-speech model files are missing — prepare the models first." };
    }
    const response = await dispatchAwaiting("init", null, { type: "init", tts: models.ttsPaths }, initTimeout);
    if (!response) return { error: "Speech synthesis did not initialize in time." };
    if (response.type === "init-fail" && response.engine === "tts") {
      return { error: scrubPathsForUi(response.message) };
    }
    if (response.type === "init-ok" && response.tts) return response;
    return { error: "Speech synthesis init failed." };
  };

  const arm = async (): Promise<VoiceEngineOutcome> => {
    const state = machine.getState();
    if (state === "failed" || state === "cancelled") {
      const acknowledge = applyAction({ type: "reset" });
      if (!acknowledge.ok) return acknowledge;
    }
    return applyAction({ type: "arm" });
  };

  const begin = async (): Promise<VoiceEngineOutcome> => {
    const guard = requireState("armed");
    if (guard) return guard;
    const init = await loadStt();
    if ("error" in init) {
      failMachine("MODEL_MISSING", init.error);
      return { ok: false, code: "MODEL_MISSING", message: init.error };
    }
    // Opening the stream is fire-and-forget (the worker answers only in
    // failure): apply the legal transition first — a late worker-error
    // still lands the machine in 'failed' from 'listening', honestly.
    const streamRequest = validateWorkerRequest({ type: "stt-start" });
    if (!streamRequest.ok || !transport) {
      failMachine("STT_FAILURE", "The recognizer stream could not be requested.");
      return { ok: false, code: "STT_FAILURE", message: "The recognizer stream could not be requested." };
    }
    try {
      transport.send(streamRequest.value);
    } catch (error) {
      failMachine(
        "STT_FAILURE",
        error instanceof Error ? error.message : "The recognizer stream could not be requested.",
      );
      return { ok: false, code: "STT_FAILURE", message: engineFailureText() };
    }
    return applyAction({ type: "begin_capture" });
  };

  const feed = (samples: Float32Array): VoiceEngineOutcome => {
    const guard = requireState("listening");
    if (guard) return guard;
    const validated = validateWorkerRequest({ type: "stt-feed", samples });
    if (!validated.ok) {
      return { ok: false, code: "BAD_PAYLOAD", message: "Audio chunk refused (bounds)." };
    }
    if (!transport) {
      return { ok: false, code: "ENGINE_UNAVAILABLE", message: "No voice worker is running." };
    }
    try {
      transport.send(validated.value);
    } catch (error) {
      return {
        ok: false,
        code: "ENGINE_UNAVAILABLE",
        message: clamp(error instanceof Error ? error.message : "Audio chunk could not be sent.", STATUS_MESSAGE_MAX),
      };
    }
    return { ok: true, state: machine.getState() };
  };

  const end = async (): Promise<IpcResult<{ state: VoiceState; transcript: string }>> => {
    const guard = requireState("listening");
    if (guard) return ipcError(guard.code, guard.message);
    const response = await dispatchAwaiting("session", "stt", { type: "stt-end" }, streamTimeout);
    if (!response || response.type !== "stt-final") {
      const failure = machine.getFailure();
      const message = failure?.message ?? "Speech recognition failed.";
      if (machine.getState() === "listening") {
        failMachine("STT_FAILURE", message);
      }
      return ipcError("STT_FAILURE", message);
    }
    const transcript = response.transcript.slice(0, TRANSCRIPT_MAX);
    const finalize = machine.dispatch({ type: "end_capture" });
    if (!finalize.ok) {
      return ipcError("VOICE_STATE", clamp(finalize.reason, STATUS_MESSAGE_MAX));
    }
    emitState(finalize.state);
    const ready = machine.dispatch({ type: "finalize", transcript });
    if (!ready.ok) return ipcError("UNEXPECTED", "Transcript could not be recorded.");
    emitState(ready.state);
    emit({ kind: "final", transcript });
    return ipcOk({ state: ready.state, transcript });
  };

  const abortSynthesis = (): void => {
    if (!transport) return;
    const validated = validateWorkerRequest({ type: "tts-abort" });
    if (validated.ok) transport.send(validated.value);
  };

  const cancel = (): VoiceEngineOutcome => {
    const state = machine.getState();
    if (state === "speaking") abortSynthesis();
    if (state === "listening" || state === "armed") killTransport();
    settleAwaiting(null);
    speakingRequestId = null;
    return applyAction({ type: "cancel" });
  };

  const stop = (): VoiceEngineOutcome => {
    const state = machine.getState();
    if (state === "speaking") abortSynthesis();
    settleAwaiting(null);
    speakingRequestId = null;
    return applyAction({ type: "cancel" });
  };

  const speak = async (
    text: string,
    sid = 0,
    speed = 1,
    requestId = 0,
  ): Promise<VoiceEngineOutcome> => {
    if (
      !Number.isInteger(requestId) ||
      requestId < 0 ||
      requestId > VOICE_PLAYBACK_REQUEST_ID_MAX
    ) {
      return { ok: false, code: "BAD_PAYLOAD", message: "TTS request identity is out of bounds." };
    }
    const currentState = machine.getState();
    if (currentState === "failed" || currentState === "cancelled") {
      const reset = applyAction({ type: "reset" });
      if (!reset.ok) return reset;
    }
    const guard = requireState("idle", "transcript_ready");
    if (guard) return guard;
    const bounded = text.slice(0, TRANSCRIPT_MAX);
    const began = applyAction({ type: "speak_begin" });
    if (!began.ok) return began;
    speakingRequestId = requestId;

    const init = await loadTts();
    if ("error" in init) {
      if (speakingRequestId !== requestId || machine.getState() !== "speaking") {
        return {
          ok: false,
          code: "RECORDING_CANCELLED",
          message: "Speech playback was cancelled.",
        };
      }
      const missing = init.error.includes("missing");
      failMachine(missing ? "MODEL_MISSING" : "TTS_FAILURE", init.error);
      speakingRequestId = null;
      return { ok: false, code: missing ? "MODEL_MISSING" : "TTS_FAILURE", message: init.error };
    }
    const ended = await dispatchAwaiting(
      "session",
      "speaking",
      {
        type: "tts-speak",
        text: bounded,
        sid,
        speed: Math.max(0.5, Math.min(3, speed)),
        requestId,
      },
      synthTimeout,
    );
    if (speakingRequestId !== requestId) {
      return {
        ok: false,
        code: "RECORDING_CANCELLED",
        message: "Speech playback was cancelled or superseded.",
      };
    }
    if (machine.getState() === "failed") {
      const recorded = machine.getFailure();
      speakingRequestId = null;
      return {
        ok: false,
        code: recorded?.code ?? "TTS_FAILURE",
        message: recorded?.message ?? "Speech synthesis failed.",
      };
    }
    if (machine.getState() !== "speaking") {
      return {
        ok: false,
        code: "RECORDING_CANCELLED",
        message: "Speech playback was cancelled or superseded.",
      };
    }
    if (!ended || ended.type !== "tts-ended") {
      failMachine("TTS_FAILURE", "Synthesis did not finish in time.");
      speakingRequestId = null;
      return { ok: false, code: "TTS_FAILURE", message: "Synthesis did not finish in time." };
    }
    if (ended.aborted) {
      // The user aborted mid-synthesis while still 'speaking': cancel is
      // the legal transition and records the cancellation honestly.
      speakingRequestId = null;
      return applyAction({ type: "cancel" });
    }
    const finished = applyAction({ type: "speak_end" });
    speakingRequestId = null;
    if (!finished.ok) return finished;
    // The closed-set event vocabulary includes tts-ended for NATURAL
    // completion; cancellations are carried by the state event alone.
    if (
      ended.type === "tts-ended" &&
      ended.totalSamples > 0 &&
      ended.totalSamples <= VOICE_TTS_MAX_SAMPLES
    ) {
      emit({
        kind: "tts-ended",
        requestId,
        totalSamples: ended.totalSamples,
        sampleRate: ended.sampleRate,
        channels: VOICE_TTS_CHANNELS,
        encoding: VOICE_TTS_ENCODING,
      });
    }
    return { ok: true, state: machine.getState() };
  };

  return {
    status: (): VoiceEngineStatus => {
      const models = deps.models.status();
      const alive = transport?.running() ?? false;
      const message = alive
        ? "Voice engine initialized for this session."
        : models.sttPresent || models.ttsPresent
          ? "Models ready; the engine starts on first use."
          : "Voice models are not installed yet — prepare them first.";
      return {
        engineAvailable: alive,
        sttModelPresent: models.sttPresent,
        ttsModelPresent: models.ttsPresent,
        vadModelPresent: false,
        message: clamp(message, STATUS_MESSAGE_MAX),
      };
    },
    prepareModels: async (onProgress) => {
      const result = await deps.models.prepareModels(
        onProgress
          ? (progress) => {
              onProgress({
                file: progress.file,
                doneFiles: progress.doneFiles,
                totalFiles: progress.totalFiles,
                phase: progress.phase,
              });
            }
          : undefined,
      );
      return result.ok
        ? ipcOk({ downloaded: result.downloaded, bytes: result.bytes })
        : ipcError("MODEL_MISSING", clamp(result.error, FAILURE_TEXT_MAX));
    },
    state: () => machine.getState(),
    failure: () => machine.getFailure(),
    arm,
    begin,
    feed,
    end,
    cancel,
    speak,
    stop,
    acknowledge: () => {
      const state = machine.getState();
      if (state === "failed" || state === "cancelled") return applyAction({ type: "reset" });
      return { ok: true, state };
    },
    dispose: () => {
      killTransport();
      subscribers.clear();
    },
    subscribe: (subscriber) => {
      subscribers.add(subscriber);
      return () => {
        subscribers.delete(subscriber);
      };
    },
  };
}
