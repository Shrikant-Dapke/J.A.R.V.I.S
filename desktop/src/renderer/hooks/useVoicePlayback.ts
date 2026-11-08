import { useCallback, useEffect, useRef, useState } from "react";
import {
  VOICE_PLAYBACK_REQUEST_ID_MAX,
  type IpcErrorCode,
} from "../../../shared/ipc-contract";
import type { VoiceEventPayload } from "../../../shared/voice-engine";
import { BoundedTtsAudioAccumulator } from "../../../shared/voice-playback";
import { errorCopy, prepareVoiceModels, subscribeVoiceEvents, ttsSpeak, ttsStop } from "../services/bridge";

export type VoicePlaybackState =
  | "idle"
  | "preparing"
  | "synthesizing"
  | "playing"
  | "completed"
  | "cancelled"
  | "failed";

export type VoicePlaybackFailure = {
  code: IpcErrorCode;
  message: string;
};

export type VoicePlaybackView = {
  state: VoicePlaybackState;
  activeMessageId: string | null;
  failure: VoicePlaybackFailure | null;
  preparing: boolean;
  play: (messageId: string, text: string) => void;
  stop: () => void;
  prepareModels: () => void;
};

type PlaybackSession = {
  token: number;
  requestId: number;
  messageId: string;
};

function userFailure(code: IpcErrorCode, fallback: string): VoicePlaybackFailure {
  const detailedCodes: ReadonlySet<IpcErrorCode> = new Set([
    "VOICE_STATE",
    "ENGINE_UNAVAILABLE",
    "MODEL_MISSING",
    "TTS_FAILURE",
    "AUDIO_OUTPUT_FAILURE",
  ]);
  return {
    code,
    message: detailedCodes.has(code) && fallback.trim().length > 0 ? fallback : errorCopy(code, fallback),
  };
}

function playbackTrace(message: string, details?: Record<string, unknown>): void {
  // Keep diagnostics bounded and metadata-only; synthesized PCM and reply text
  // never enter the console.
  if (details) console.info(`[voice][tts] ${message}`, details);
  else console.info(`[voice][tts] ${message}`);
}

export function useVoicePlayback(): VoicePlaybackView {
  const [state, setStateValue] = useState<VoicePlaybackState>("idle");
  const [activeMessageId, setActiveMessageId] = useState<string | null>(null);
  const [failure, setFailure] = useState<VoicePlaybackFailure | null>(null);

  const mountedRef = useRef(true);
  const stateRef = useRef<VoicePlaybackState>("idle");
  const tokenRef = useRef(0);
  const requestIdRef = useRef(0);
  const activeRef = useRef<PlaybackSession | null>(null);
  const accumulatorRef = useRef<BoundedTtsAudioAccumulator | null>(null);
  const mainSynthesisActiveRef = useRef(false);
  const synthesisPromiseRef = useRef<Promise<unknown> | null>(null);
  const contextRef = useRef<AudioContext | null>(null);
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);

  const setState = useCallback((next: VoicePlaybackState): void => {
    stateRef.current = next;
    if (mountedRef.current) setStateValue(next);
  }, []);

  const closeAudio = useCallback((): void => {
    const source = sourceRef.current;
    sourceRef.current = null;
    if (source) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // A source that already ended is safe to discard.
      }
      try {
        source.disconnect();
      } catch {
        // Disconnect is idempotent during cleanup.
      }
    }
    const context = contextRef.current;
    contextRef.current = null;
    if (context) void context.close().catch(() => undefined);
  }, []);

  const cancelMainSynthesis = useCallback((): Promise<void> => {
    const pending = synthesisPromiseRef.current;
    if (!mainSynthesisActiveRef.current && !pending) return Promise.resolve();
    mainSynthesisActiveRef.current = false;
    const stop = ttsStop().then(() => undefined).catch(() => undefined);
    return Promise.allSettled([stop, pending ?? Promise.resolve()]).then(() => undefined);
  }, []);

  const clearSession = useCallback((): void => {
    tokenRef.current += 1;
    activeRef.current = null;
    accumulatorRef.current = null;
    closeAudio();
  }, [closeAudio]);

  const failCurrent = useCallback(
    (token: number, code: IpcErrorCode, fallback: string): void => {
      if (tokenRef.current !== token || activeRef.current?.token !== token) return;
      activeRef.current = null;
      accumulatorRef.current = null;
      mainSynthesisActiveRef.current = false;
      closeAudio();
      setFailure(userFailure(code, fallback));
      setState("failed");
      void ttsStop().catch(() => undefined);
    },
    [closeAudio, setState],
  );

  const playAudio = useCallback(
    async (session: PlaybackSession, samples: Float32Array, sampleRate: number): Promise<void> => {
      if (tokenRef.current !== session.token || activeRef.current?.requestId !== session.requestId) return;
      try {
        const context = contextRef.current ?? new AudioContext();
        contextRef.current = context;
        await context.resume();
        if (tokenRef.current !== session.token || activeRef.current?.requestId !== session.requestId) return;
        if (context.state !== "running") {
          throw new Error(`AudioContext remained ${context.state} after resume.`);
        }
        if (!Number.isFinite(sampleRate) || sampleRate < 8_000 || sampleRate > 192_000) {
          throw new Error(`TTS sample rate ${sampleRate} Hz is not supported by the audio output.`);
        }
        const buffer = context.createBuffer(1, samples.length, sampleRate);
        buffer.getChannelData(0).set(samples);
        const source = context.createBufferSource();
        source.buffer = buffer;
        source.connect(context.destination);
        source.onended = () => {
          if (tokenRef.current !== session.token || activeRef.current?.requestId !== session.requestId) return;
          sourceRef.current = null;
          activeRef.current = null;
          accumulatorRef.current = null;
          closeAudio();
          setState("completed");
        };
        sourceRef.current = source;
        source.start();
        playbackTrace("audio source started", {
          requestId: session.requestId,
          samples: samples.length,
          sampleRate,
          contextState: context.state,
        });
        setState("playing");
      } catch (error) {
        playbackTrace("audio output failed", {
          requestId: session.requestId,
          message: error instanceof Error ? error.message : String(error),
        });
        failCurrent(
          session.token,
          "AUDIO_OUTPUT_FAILURE",
          `Synthesized audio could not be played: ${error instanceof Error ? error.message : String(error)}`,
        );
      }
    },
    [closeAudio, failCurrent, setState],
  );

  const play = useCallback(
    (messageId: string, text: string): void => {
      const boundedText = text.trim();
      if (boundedText.length === 0) return;

      const previousActive =
        activeRef.current !== null ||
        mainSynthesisActiveRef.current ||
        synthesisPromiseRef.current !== null ||
        sourceRef.current !== null;
      clearSession();
      const previousStop = previousActive ? cancelMainSynthesis() : Promise.resolve();

      const token = tokenRef.current;
      requestIdRef.current =
        requestIdRef.current >= VOICE_PLAYBACK_REQUEST_ID_MAX
          ? 0
          : requestIdRef.current + 1;
      const requestId = requestIdRef.current;
      const session: PlaybackSession = { token, requestId, messageId };
      activeRef.current = session;
      accumulatorRef.current = new BoundedTtsAudioAccumulator(requestId);
      setActiveMessageId(messageId);
      setFailure(null);
      setState("preparing");
      playbackTrace("playback requested", { requestId, characters: boundedText.length });

      // Construct/resume the context from the explicit speaker click. The
      // audio is not connected until synthesis has produced a complete,
      // bounded buffer.
      try {
        contextRef.current = new AudioContext();
        void contextRef.current.resume().catch(() => undefined);
        playbackTrace("output AudioContext created", {
          requestId,
          sampleRate: contextRef.current.sampleRate,
          state: contextRef.current.state,
        });
      } catch {
        failCurrent(token, "AUDIO_OUTPUT_FAILURE", "Audio playback is unavailable in this window.");
        return;
      }

      const synthesisPromise = (async () => {
        await previousStop;
        if (tokenRef.current !== token || activeRef.current?.requestId !== requestId) return;
        setState("synthesizing");
        mainSynthesisActiveRef.current = true;
        try {
          const result = await ttsSpeak({ text: boundedText, requestId });
          if (tokenRef.current !== token || activeRef.current?.requestId !== requestId) return;
          mainSynthesisActiveRef.current = false;
          if (!result.ok) {
            playbackTrace("synthesis failed", { requestId, code: result.code });
            failCurrent(token, result.code, result.message);
          }
        } catch {
          mainSynthesisActiveRef.current = false;
          playbackTrace("synthesis IPC failed", { requestId });
          failCurrent(token, "TTS_FAILURE", "Speech synthesis could not be reached.");
        }
      })();
      synthesisPromiseRef.current = synthesisPromise;
      void synthesisPromise.then(
        () => {
          if (synthesisPromiseRef.current === synthesisPromise) synthesisPromiseRef.current = null;
        },
        () => {
          if (synthesisPromiseRef.current === synthesisPromise) synthesisPromiseRef.current = null;
        },
      );
    },
    [cancelMainSynthesis, clearSession, failCurrent, setState],
  );

  const stop = useCallback((): void => {
    const hadSession = activeRef.current !== null || mainSynthesisActiveRef.current || sourceRef.current !== null;
    if (!hadSession && stateRef.current !== "failed" && stateRef.current !== "completed") return;
    clearSession();
    void cancelMainSynthesis();
    setState("cancelled");
  }, [cancelMainSynthesis, clearSession, setState]);

  const prepareModels = useCallback((): void => {
    if (stateRef.current !== "failed" || failure?.code !== "MODEL_MISSING") return;
    const token = ++tokenRef.current;
    activeRef.current = null;
    accumulatorRef.current = null;
    closeAudio();
    setState("preparing");
    void (async () => {
      try {
        const result = await prepareVoiceModels();
        if (!mountedRef.current || tokenRef.current !== token) return;
        if (result.ok) {
          setFailure(null);
          setActiveMessageId(null);
          setState("idle");
        } else {
          setFailure(userFailure(result.code, result.message));
          setState("failed");
        }
      } catch {
        if (mountedRef.current && tokenRef.current === token) {
          setFailure(userFailure("TTS_FAILURE", "Voice models could not be prepared."));
          setState("failed");
        }
      }
    })();
  }, [closeAudio, failure, setState]);

  useEffect(() => {
    mountedRef.current = true;
    let unsubscribe: (() => void) | null = null;
    try {
      unsubscribe = subscribeVoiceEvents((event: VoiceEventPayload) => {
        const session = activeRef.current;
        if (!session || event.kind === "state" || event.kind === "partial" || event.kind === "final" || event.kind === "failure") {
          return;
        }
        if (event.kind === "tts-chunk") {
          const accumulator = accumulatorRef.current;
          if (!accumulator || event.requestId !== session.requestId) return;
          const result = accumulator.acceptChunk(event);
          if (!result.ok) {
            if (result.stale) return;
            failCurrent(session.token, "TTS_FAILURE", result.reason);
          }
          return;
        }
        if (event.kind === "tts-ended") {
          if (event.requestId !== session.requestId) return;
          const accumulator = accumulatorRef.current;
          if (!accumulator) return;
          const result = accumulator.finish(event);
          if (!result.ok) {
            if (result.stale) return;
            failCurrent(session.token, "TTS_FAILURE", result.reason);
            return;
          }
          playbackTrace("synthesis complete", {
            requestId: event.requestId,
            samples: result.value.samples.length,
            sampleRate: result.value.sampleRate,
          });
          void playAudio(session, result.value.samples, result.value.sampleRate);
        }
      });
    } catch {
      // Outside Electron, the explicit speaker action reports the bridge error.
    }
    return () => {
      mountedRef.current = false;
      clearSession();
      void cancelMainSynthesis();
      unsubscribe?.();
    };
  }, [cancelMainSynthesis, clearSession, failCurrent, playAudio]);

  return {
    state,
    activeMessageId,
    failure,
    preparing: state === "preparing",
    play,
    stop,
    prepareModels,
  };
}
