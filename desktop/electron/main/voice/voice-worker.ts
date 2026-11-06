/**
 * Voice worker core (Phase 6.4-b).
 *
 * Loads the sherpa-onnx native runtime LAZILY and owns all native
 * inference: streaming Zipformer STT (16 kHz mono float32 frames in,
 * partial/final text out) and int8 Kokoro-82M TTS (bounded text in,
 * chunked PCM out — with the sample rate ALWAYS reported by the model,
 * never assumed).
 *
 * This module is deliberately Electron-free: it runs as the entry point
 * of an Electron `utilityProcess` (wired via `voice-transport.ts`) and
 * can equally run in a plain Node process for the smoke benchmark.
 *
 * Failure honesty: every reply is structured, key-exact, and bounded
 * (validates through shared/voice-protocol.ts); a native constructor
 * throw becomes an `init-fail` with a clamped message — never a crash of
 * the whole worker and never synthetic audio or placeholder text.
 */

import type { VoiceErrorCode } from "../../../shared/voice-engine";
import { VOICE_TTS_MAX_SAMPLES } from "../../../shared/ipc-contract";
import {
  validateWorkerRequest,
  validateWorkerResponse,
  VOICE_PROTOCOL_STT_SAMPLE_RATE,
  VOICE_PROTOCOL_TTS_CHUNK_SAMPLES,
  type SttEnginePaths,
  type TtsEnginePaths,
  type WorkerRequest,
} from "../../../shared/voice-protocol";

const FAILURE_TEXT_MAX = 512;
const clampMessage = (message: string): string =>
  message.length <= FAILURE_TEXT_MAX ? message : message.slice(0, FAILURE_TEXT_MAX);

/**
 * Digital silence fed at stt-end so the encoder emits the utterance's
 * trailing tokens (≥300 ms measured minimum; 500 ms for margin).
 */
const STT_FLUSH_SILENCE_SECONDS = 0.5;

export type WorkerHost = {
  /** Send one structured reply to the parent (validated before send). */
  post: (message: unknown) => void;
  /** Begin consuming requests; the host owns the raw transport. */
  listen: (onRequest: (request: WorkerRequest) => void) => void;
};

export type StartedWorker = {
  /** Ask the host to stop consuming (native handles are released). */
  shutdown: () => Promise<void>;
};

/**
 * NATIVE availability only: the module reports whether the addon can be
 * required at all — used by the engine to distinguish "runtime absent"
 * (ENGINE_UNAVAILABLE) from "model files absent" (MODEL_MISSING).
 */
export function isSherpaRuntimePresent(): boolean {
  try {
    require("sherpa-onnx-node");
    return true;
  } catch {
    return false;
  }
}

export function startVoiceWorker(host: WorkerHost, nativeModule?: unknown): StartedWorker {
  type SherpaModule = typeof import("sherpa-onnx-node");
  let sherpa: SherpaModule | null = null;
  let recognizer: import("sherpa-onnx-node").OnlineRecognizer | null = null;
  let tts: import("sherpa-onnx-node").OfflineTts | null = null;
  let stream: import("sherpa-onnx-node").OnlineStream | null = null;
  let staleStream = false;
  let shutdownRequested = false;

  const emit = (response: unknown): void => {
    const validated = validateWorkerResponse(response);
    if (!validated.ok) {
      // Never emit a malformed message; say so honestly instead.
      host.post({ type: "worker-error", scope: "engine", message: "Internal reply validation failed." });
      return;
    }
    host.post(validated.value);
  };

  const ensureSherpa = (): SherpaModule => {
    if (nativeModule) return nativeModule as SherpaModule;
    if (!sherpa) {
      // eslint-disable-next-line @typescript-eslint/no-require-imports
      sherpa = require("sherpa-onnx-node") as SherpaModule;
    }
    return sherpa as SherpaModule;
  };

  /* ----------------------------- STT ------------------------------ */

  const lazyStt = (paths?: SttEnginePaths): void => {
    if (recognizer || !paths) return;
    const module = ensureSherpa();
    recognizer = new module.OnlineRecognizer({
      featConfig: { sampleRate: VOICE_PROTOCOL_STT_SAMPLE_RATE, featureDim: 80 },
      modelConfig: {
        transducer: { encoder: paths.encoder, decoder: paths.decoder, joiner: paths.joiner },
        tokens: paths.tokens,
        numThreads: 1,
        provider: "cpu",
        debug: 0,
      },
      decodingMethod: "greedy_search",
      enableEndpoint: 1,
      rule1MinTrailingSilence: 2.4,
      rule2MinTrailingSilence: 1.2,
      rule3MinUtteranceLength: 20,
      blankPenalty: 0,
    });
  };

  let lastPartial = "";

  const decodePending = (): string => {
    if (!recognizer || !stream) return "";
    while (recognizer.isReady(stream)) recognizer.decode(stream);
    const result = recognizer.getResult(stream);
    return result.text;
  };

  /* ----------------------------- TTS ------------------------------ */

  /** Resolves once a requested TTS leg has finished (success OR fail). */
  let ttsSettled: Promise<void> = Promise.resolve();
  let ttsLoadError: string | null = null;

  const lazyTts = (paths?: TtsEnginePaths): void => {
    if (tts || !paths) return;
    const module = ensureSherpa();
    ttsSettled = (async () => {
      try {
        tts = await module.OfflineTts.createAsync({
          model: {
            kokoro: {
              model: paths.model,
              voices: paths.voices,
              tokens: paths.tokens,
              dataDir: paths.dataDir,
              lexicon: paths.lexicon,
            },
          },
          numThreads: 1,
          provider: "cpu",
        });
        ttsLoadError = null;
      } catch (error) {
        ttsLoadError = clampMessage(error instanceof Error ? error.message : String(error));
      }
    })();
  };

  let synthesizeAbort = false;
  let synthesisInFlight = false;

  /* --------------------------- Dispatch --------------------------- */

  const pending: WorkerRequest[] = [];
  let draining = false;

  const drain = async (): Promise<void> => {
    if (draining) return;
    draining = true;
    while (pending.length > 0 && !shutdownRequested) {
      const request = pending.shift() as WorkerRequest;
      await handle(request);
    }
    draining = false;
  };

  const pushFailure = (
    code: VoiceErrorCode,
    scope: "stt" | "tts" | "engine",
    message: string,
    requestId?: number,
  ): void => {
    emit({
      type: "worker-error",
      scope,
      message: clampMessage(message),
      ...(code !== "ENGINE_UNAVAILABLE" ? { code } : {}),
      ...(requestId !== undefined ? { requestId } : {}),
    });
  };

  const handle = async (request: WorkerRequest): Promise<void> => {
    switch (request.type) {
      case "init": {
        try {
          lazyStt(request.stt);
          lazyTts(request.tts);
          // The TTS constructor is async; init reports the SETTLED state,
          // so `tts:true` always means "loadable and loaded" — never
          // "still trying".
          await ttsSettled;
          if (request.tts && tts === null && ttsLoadError !== null) {
            emit({ type: "init-fail", engine: "tts", message: ttsLoadError });
            return;
          }
          emit({
            type: "init-ok",
            stt: recognizer !== null,
            tts: tts !== null,
            ttsSampleRate: tts ? tts.sampleRate : 0,
            ttsNumSpeakers: tts ? tts.numSpeakers : 0,
          });
        } catch (error) {
          emit({
            type: "init-fail",
            engine: request.stt ? "stt" : "tts",
            message: clampMessage(error instanceof Error ? error.message : String(error)),
          });
        }
        return;
      }
      case "stt-start": {
        if (!recognizer) {
          pushFailure("MODEL_MISSING", "engine", "Speech recognition is not initialized.");
          return;
        }
        try {
          stream = recognizer.createStream();
          staleStream = false;
          lastPartial = "";
        } catch (error) {
          pushFailure("STT_FAILURE", "stt", error instanceof Error ? error.message : String(error));
        }
        return;
      }
      case "stt-feed": {
        if (!recognizer || !stream) {
          pushFailure("STT_FAILURE", "stt", "No recognition stream is open.");
          return;
        }
        try {
          // sherpa-onnx-node ≥ 2.0: waveform ingestion lives on the STREAM.
          stream.acceptWaveform({
            samples: request.samples,
            sampleRate: VOICE_PROTOCOL_STT_SAMPLE_RATE,
          });
          const text = decodePending();
          if (text !== lastPartial) {
            lastPartial = text;
            emit({ type: "stt-partial", transcript: text });
          }
        } catch (error) {
          pushFailure("STT_FAILURE", "stt", error instanceof Error ? error.message : String(error));
        }
        return;
      }
      case "stt-end": {
        if (!recognizer || staleStream || !stream) {
          pushFailure("STT_FAILURE", "stt", "No open recognition stream to end.");
          return;
        }
        try {
          // Terminating flush (MEASURED): with real int8 Zipformer models
          // the trailing word is dropped unless the encoder receives extra
          // frames after the speech ends — `inputFinished()` alone does not
          // recover it, but ≥300 ms of digital silence before the final
          // drain reliably does. The pad is never presented as captured
          // audio; it exists only to push the decoder's trailing chunk
          // through. 500 ms gives margin over the measured 300 ms minimum.
          stream.acceptWaveform({
            samples: new Float32Array(Math.round(STT_FLUSH_SILENCE_SECONDS * VOICE_PROTOCOL_STT_SAMPLE_RATE)),
            sampleRate: VOICE_PROTOCOL_STT_SAMPLE_RATE,
          });
          stream.inputFinished();
          const text = decodePending();
          recognizer.reset(stream);
          staleStream = true;
          if (text.trim().length === 0) {
            pushFailure("STT_FAILURE", "stt", "No speech was recognized in the recording.");
            return;
          }
          emit({ type: "stt-final", transcript: text.slice(0, 2000) });
        } catch (error) {
          pushFailure("STT_FAILURE", "stt", error instanceof Error ? error.message : String(error));
        }
        return;
      }
      case "tts-speak": {
        if (!tts) {
          await ttsSettled;
          if (tts === null) {
            pushFailure("MODEL_MISSING", "engine", "Speech synthesis is not initialized.", request.requestId);
            return;
          }
        }
        const engine = tts;
        if (!engine) {
          pushFailure("TTS_FAILURE", "tts", "Speech synthesis is not available.", request.requestId);
          return;
        }
        synthesizeAbort = false;
        synthesisInFlight = true;
        let sequence = 0;
        let totalSamples = 0;
        let outputLimitExceeded = false;
        try {
          // onProgress receives INCREMENTAL synthesis pieces; the final
          // `audio.samples` is the WHOLE utterance. The emitted stream is
          // therefore fed exclusively by onProgress — the audio object is
          // a fallback for runtimes that give no progress callbacks.
          const chunker = (samples: Float32Array, sampleRate: number): boolean => {
            if (totalSamples + samples.length > VOICE_TTS_MAX_SAMPLES) {
              outputLimitExceeded = true;
              return false;
            }
            for (let offset = 0; offset < samples.length; offset += VOICE_PROTOCOL_TTS_CHUNK_SAMPLES) {
              const chunk = samples.slice(offset, offset + VOICE_PROTOCOL_TTS_CHUNK_SAMPLES);
              emit({
                type: "tts-chunk",
                requestId: request.requestId,
                sequence,
                samples: chunk,
                sampleRate,
              });
              sequence += 1;
              totalSamples += chunk.length;
            }
            return true;
          };
          const audio = await engine.generateAsync({
            text: request.text,
            sid: request.sid,
            speed: request.speed,
            onProgress: (info) => {
              if (synthesizeAbort) return 0; // sherpa stops generation on 0
              return chunker(info.samples, engine.sampleRate) ? 1 : 0;
            },
          });
          if (totalSamples === 0 && audio.samples.length > 0) {
            chunker(audio.samples, audio.sampleRate);
          }
          if (outputLimitExceeded) {
            synthesisInFlight = false;
            pushFailure(
              "TTS_FAILURE",
              "tts",
              "Speech output exceeded the bounded playback limit.",
              request.requestId,
            );
            return;
          }
          synthesisInFlight = false;
          emit({
            type: "tts-ended",
            requestId: request.requestId,
            totalSamples,
            sampleRate: totalSamples > 0 ? engine.sampleRate : audio.sampleRate,
            aborted: synthesizeAbort,
          });
        } catch (error) {
          synthesisInFlight = false;
          pushFailure(
            "TTS_FAILURE",
            "tts",
            error instanceof Error ? error.message : String(error),
            request.requestId,
          );
        }
        return;
      }
      case "tts-abort": {
        if (synthesisInFlight) synthesizeAbort = true;
        return;
      }
      case "shutdown": {
        shutdownRequested = true;
        return;
      }
      default:
        return;
    }
  };

  host.listen((rawRequest) => {
    const validated = validateWorkerRequest(rawRequest);
    if (!validated.ok) {
      emit({ type: "worker-error", scope: "engine", message: "Malformed worker request refused." });
      return;
    }
    pending.push(validated.value);
    void drain();
  });

  // The beacon proves the transport works before any model is loaded.
  emit({ type: "worker-ready" });

  return {
    shutdown: async () => {
      shutdownRequested = true;
      // The addon exposes no explicit release for the online recognizer;
      // dropping the handles is the documented teardown path — the OS
      // reclaims the native memory with the worker process.
      recognizer = null;
      tts = null;
      stream = null;
    },
  };
}
