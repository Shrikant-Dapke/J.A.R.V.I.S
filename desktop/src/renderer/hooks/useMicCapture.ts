import { useCallback, useEffect, useRef, useState } from "react";
import {
  VOICE_MIC_CHUNK_SAMPLES,
  type IpcErrorCode,
  type VoiceFeedPayload,
} from "../../../shared/ipc-contract";
import type { VoiceEventPayload } from "../../../shared/voice-engine";
import {
  float32ToPcm16Bytes,
  mixToMono,
  Pcm16Chunker,
  StreamingPcmResampler,
} from "../../../shared/voice-audio";
import { createMountedGuard } from "../../../shared/mounted-guard";
import { createVoiceCommandQueue } from "../../../shared/voice-command-queue";
import { MIC_AUDIO_CONSTRAINTS, MIC_CAPTURE_VIDEO } from "../../../shared/mic-capture-constraints";
import {
  subscribeVoiceEvents,
  voiceArm,
  voiceBegin,
  voiceCancel,
  voiceEnd,
  voiceFeed,
  prepareVoiceModels,
} from "../services/bridge";

export type MicCaptureState =
  | "idle"
  | "requesting_permission"
  | "listening"
  | "transcribing"
  | "transcript_ready"
  | "cancelled"
  | "failed";

export type MicFailureKind =
  | "MICROPHONE_DENIED"
  | "DEVICE_UNAVAILABLE"
  | "ENGINE_UNAVAILABLE"
  | "MODEL_MISSING"
  | "STT_FAILURE"
  | "SILENT_INPUT"
  | "RECORDING_CANCELLED"
  | "CAPTURE_FAILURE";

export type MicFailure = {
  kind: MicFailureKind;
  message: string;
};

export type MicCaptureView = {
  state: MicCaptureState;
  partial: string;
  transcript: string;
  failure: MicFailure | null;
  preparing: boolean;
  start: () => void;
  prepareModels: () => void;
  stop: () => void;
  cancel: () => void;
};

type WorkletMessage = {
  channels: unknown;
};

type CaptureResources = {
  stream: MediaStream;
  track: MediaStreamTrack;
  context: AudioContext;
  source: MediaStreamAudioSourceNode;
  worklet: AudioWorkletNode;
  mute: GainNode;
  resampler: StreamingPcmResampler;
  chunker: Pcm16Chunker;
  onTrackEnded: () => void;
  inputBlocks: number;
  inputSamples: number;
  peak: number;
};

type CaptureMode = "idle" | "active" | "ending" | "cancelled" | "failed";

const MAX_PENDING_FEEDS = 3;
const SILENT_INPUT_PEAK = 0.0001;

function micTrace(message: string, details?: Record<string, unknown>): void {
  // Diagnostics contain only bounded metadata and never PCM or transcript text.
  if (details) console.info(`[voice][mic] ${message}`, details);
  else console.info(`[voice][mic] ${message}`);
}

function disposeCaptureResources(resources: CaptureResources | null): void {
  if (!resources) return;
  resources.track.removeEventListener("ended", resources.onTrackEnded);
  resources.worklet.port.onmessage = null;
  resources.worklet.port.onmessageerror = null;
  try {
    resources.worklet.port.close();
  } catch {
    // The port may already be closed during browser teardown.
  }
  try {
    resources.source.disconnect();
  } catch {
    // Disconnect is idempotent for the capture cleanup path.
  }
  try {
    resources.worklet.disconnect();
  } catch {
    // Disconnect is idempotent for the capture cleanup path.
  }
  try {
    resources.mute.disconnect();
  } catch {
    // Disconnect is idempotent for the capture cleanup path.
  }
  for (const track of resources.stream.getTracks()) track.stop();
  void resources.context.close().catch(() => undefined);
}

function errorName(error: unknown): string {
  return error instanceof DOMException
    ? error.name
    : error instanceof Error
      ? error.name
      : "";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function classifyCaptureError(error: unknown): MicFailure {
  const name = errorName(error);
  if (name === "NotAllowedError" || name === "SecurityError") {
    return {
      kind: "MICROPHONE_DENIED",
      message: "Microphone access was denied. Allow JARVIS to use the microphone and try again.",
    };
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return {
      kind: "DEVICE_UNAVAILABLE",
      message: "No usable microphone was found.",
    };
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return {
      kind: "DEVICE_UNAVAILABLE",
      message: "The microphone is busy or could not be read.",
    };
  }
  return {
    kind: "CAPTURE_FAILURE",
    message: errorMessage(error) || "Microphone capture could not start.",
  };
}

function classifyIpcFailure(code: IpcErrorCode, message: string): MicFailure {
  switch (code) {
    case "MICROPHONE_DENIED":
      return { kind: "MICROPHONE_DENIED", message };
    case "ENGINE_UNAVAILABLE":
      return { kind: "ENGINE_UNAVAILABLE", message };
    case "MODEL_MISSING":
      return { kind: "MODEL_MISSING", message };
    case "RECORDING_CANCELLED":
      return { kind: "RECORDING_CANCELLED", message };
    default:
      return { kind: "STT_FAILURE", message };
  }
}

function extractChannels(value: unknown): Float32Array[] | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const channels = (value as WorkletMessage).channels;
  if (!Array.isArray(channels) || channels.length < 1 || channels.length > 8) return null;

  const decoded: Float32Array[] = [];
  let length: number | null = null;
  for (const channel of channels) {
    let samples: Float32Array;
    if (channel instanceof ArrayBuffer) {
      if (channel.byteLength === 0 || channel.byteLength % Float32Array.BYTES_PER_ELEMENT !== 0) {
        return null;
      }
      samples = new Float32Array(channel);
    } else if (channel instanceof Float32Array) {
      samples = channel;
    } else {
      return null;
    }
    if (samples.length === 0 || samples.length > 8192) return null;
    if (length === null) length = samples.length;
    if (samples.length !== length) return null;
    decoded.push(samples);
  }
  return decoded;
}

function workletUrl(): string {
  return new URL("jarvis-mic-worklet.js", document.baseURI).toString();
}

export function useMicCapture({
  onTranscriptReady,
}: {
  onTranscriptReady: (transcript: string) => void;
}): MicCaptureView {
  const [state, setStateValue] = useState<MicCaptureState>("idle");
  const [partial, setPartial] = useState("");
  const [transcript, setTranscript] = useState("");
  const [failure, setFailure] = useState<MicFailure | null>(null);
  const [preparing, setPreparing] = useState(false);

  const mountedRef = useRef(createMountedGuard());
  const modeRef = useRef<CaptureMode>("idle");
  const sessionRef = useRef(0);
  const resourcesRef = useRef<CaptureResources | null>(null);
  const feedChainRef = useRef<Promise<void>>(Promise.resolve());
  const pendingFeedsRef = useRef(0);
  const sequenceRef = useRef(0);
  const finalHandledRef = useRef(false);
  const engineActiveRef = useRef(false);
  const pendingContextRef = useRef<AudioContext | null>(null);
  const commandQueueRef = useRef(createVoiceCommandQueue());
  const onTranscriptReadyRef = useRef(onTranscriptReady);

  useEffect(() => {
    onTranscriptReadyRef.current = onTranscriptReady;
  }, [onTranscriptReady]);

  const setState = useCallback((next: MicCaptureState): void => {
    if (mountedRef.current.isMounted()) setStateValue(next);
  }, []);

  const disposeCurrentResources = useCallback((): CaptureResources | null => {
    const resources = resourcesRef.current;
    resourcesRef.current = null;
    disposeCaptureResources(resources);
    const pendingContext = pendingContextRef.current;
    pendingContextRef.current = null;
    if (pendingContext && pendingContext !== resources?.context) {
      void pendingContext.close().catch(() => undefined);
    }
    return resources;
  }, []);

  const cancelEngine = useCallback(async (): Promise<void> => {
    if (!engineActiveRef.current) return;
    engineActiveRef.current = false;
    try {
      await commandQueueRef.current.run(async () => {
        const result = await voiceCancel();
        if (!result.ok && result.code !== "VOICE_STATE") {
          micTrace("cancel failed", { code: result.code });
        }
      });
    } catch {
      // Local cleanup and the visible failure remain authoritative.
    }
  }, []);

  const failSession = useCallback(
    async (sessionId: number, nextFailure: MicFailure): Promise<void> => {
      if (
        sessionRef.current !== sessionId ||
        modeRef.current === "failed" ||
        modeRef.current === "cancelled"
      ) {
        return;
      }
      modeRef.current = "failed";
      setState("failed");
      setFailure(nextFailure);
      disposeCurrentResources();
      await cancelEngine();
    },
    [cancelEngine, disposeCurrentResources, setState],
  );

  const enqueueChunk = useCallback(
    (sessionId: number, pcm: ArrayBuffer): void => {
      if (sessionRef.current !== sessionId || modeRef.current === "cancelled" || modeRef.current === "failed") {
        return;
      }
      if (pendingFeedsRef.current >= MAX_PENDING_FEEDS) {
        void failSession(sessionId, {
          kind: "CAPTURE_FAILURE",
          message: "Microphone capture could not keep up with the audio stream.",
        });
        return;
      }

      const payload: VoiceFeedPayload = {
        sequence: sequenceRef.current,
        pcm,
      };
      sequenceRef.current += 1;
      pendingFeedsRef.current += 1;
      feedChainRef.current = feedChainRef.current
        .then(async () => {
          if (sessionRef.current !== sessionId || modeRef.current === "cancelled" || modeRef.current === "failed") {
            return;
          }
          const result = await voiceFeed(payload);
          if (!result.ok) throw classifyIpcFailure(result.code, result.message);
        })
        .catch((error: unknown) => {
          micTrace("feed failed", {
            kind: error && typeof error === "object" && "kind" in error ? (error as MicFailure).kind : "unknown",
          });
          if (sessionRef.current === sessionId && modeRef.current !== "cancelled" && modeRef.current !== "failed") {
            void failSession(
              sessionId,
              error && typeof error === "object" && "kind" in error
                ? (error as MicFailure)
                : classifyCaptureError(error),
            );
          }
        })
        .finally(() => {
          if (sessionRef.current === sessionId) pendingFeedsRef.current -= 1;
        });
    },
    [failSession],
  );

  const pushSamples = useCallback(
    (sessionId: number, resources: CaptureResources, samples: Float32Array): void => {
      if (samples.length === 0) return;
      const pcm = float32ToPcm16Bytes(samples);
      for (const chunk of resources.chunker.push(new Int16Array(pcm))) {
        enqueueChunk(sessionId, chunk);
      }
    },
    [enqueueChunk],
  );

  const handleWorkletMessage = useCallback(
    (sessionId: number, resources: CaptureResources, value: unknown): void => {
      if (sessionRef.current !== sessionId || modeRef.current !== "active") return;
      const channels = extractChannels(value);
      if (!channels) {
        void failSession(sessionId, {
          kind: "CAPTURE_FAILURE",
          message: "The microphone audio stream was malformed.",
        });
        return;
      }
      try {
        resources.inputBlocks += 1;
        resources.inputSamples += channels[0]?.length ?? 0;
        for (const channel of channels) {
          for (const sample of channel) resources.peak = Math.max(resources.peak, Math.abs(sample));
        }
        if (resources.inputBlocks === 1) {
          micTrace("first audio block", {
            samples: channels[0]?.length ?? 0,
            peak: resources.peak,
          });
        }
        pushSamples(sessionId, resources, resources.resampler.push(mixToMono(channels)));
      } catch (error) {
        void failSession(sessionId, classifyCaptureError(error));
      }
    },
    [failSession, pushSamples],
  );

  const start = useCallback((): void => {
    if (modeRef.current !== "idle" && modeRef.current !== "failed" && modeRef.current !== "cancelled") return;

    const sessionId = sessionRef.current + 1;
    sessionRef.current = sessionId;
    modeRef.current = "active";
    sequenceRef.current = 0;
    pendingFeedsRef.current = 0;
    feedChainRef.current = Promise.resolve();
    finalHandledRef.current = false;
    setFailure(null);
    setPartial("");
    setTranscript("");
    setState("requesting_permission");

    let context: AudioContext;
    try {
      // Construct and resume synchronously from the microphone button's user
      // activation. Waiting for getUserMedia()/addModule() first can leave a
      // Chromium AudioContext suspended, which means the worklet never runs.
      context = new AudioContext({ latencyHint: "interactive" });
      pendingContextRef.current = context;
      void context.resume().catch((error: unknown) => {
        micTrace("initial AudioContext.resume failed", { message: errorMessage(error) });
      });
      micTrace("AudioContext created", { sampleRate: context.sampleRate, state: context.state });
    } catch (error) {
      void failSession(sessionId, {
        kind: "CAPTURE_FAILURE",
        message: `Audio input could not be initialized: ${errorMessage(error)}`,
      });
      return;
    }

    void (async () => {
      let stream: MediaStream | null = null;
      try {
        micTrace("requesting microphone");
        stream = await navigator.mediaDevices.getUserMedia({
          audio: { ...MIC_AUDIO_CONSTRAINTS },
          video: MIC_CAPTURE_VIDEO,
        });
        if (sessionRef.current !== sessionId || modeRef.current !== "active") {
          for (const track of stream.getTracks()) track.stop();
          return;
        }

        const track = stream.getAudioTracks()[0];
        if (!track) throw new Error("The microphone stream did not contain an audio track.");
        const settings = track.getSettings();
        micTrace("microphone granted", {
          sampleRate: settings.sampleRate ?? null,
          channelCount: settings.channelCount ?? null,
          contextRate: context.sampleRate,
        });
        await context.audioWorklet.addModule(workletUrl());
        micTrace("AudioWorklet module loaded", { contextState: context.state });

        const source = context.createMediaStreamSource(stream);
        const worklet = new AudioWorkletNode(context, "jarvis-mic-tap", {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          outputChannelCount: [1],
        });
        const mute = context.createGain();
        mute.gain.value = 0;
        const resources: CaptureResources = {
          stream,
          track,
          context,
          source,
          worklet,
          mute,
          resampler: new StreamingPcmResampler(context.sampleRate),
          chunker: new Pcm16Chunker(VOICE_MIC_CHUNK_SAMPLES),
          onTrackEnded: () => undefined,
          inputBlocks: 0,
          inputSamples: 0,
          peak: 0,
        };
        resources.onTrackEnded = () => {
          if (sessionRef.current === sessionId && modeRef.current === "active") {
            void failSession(sessionId, {
              kind: "DEVICE_UNAVAILABLE",
              message: "The microphone stopped unexpectedly.",
            });
          }
        };
        track.addEventListener("ended", resources.onTrackEnded);
        worklet.port.onmessage = (event: MessageEvent<unknown>) =>
          handleWorkletMessage(sessionId, resources, event.data);
        worklet.port.onmessageerror = () => {
          void failSession(sessionId, {
            kind: "CAPTURE_FAILURE",
            message: "The microphone audio worklet stopped responding.",
          });
        };
        resourcesRef.current = resources;
        pendingContextRef.current = null;
        // Resume while still inside the explicit user-activation flow;
        // the graph remains disconnected until the engine is ready.
        await context.resume();
        if (context.state !== "running") {
          throw new Error(`Audio input context is ${context.state}; microphone processing cannot start.`);
        }

        await commandQueueRef.current.run(async () => {
          const armed = await voiceArm();
          if (!armed.ok) throw classifyIpcFailure(armed.code, armed.message);
          engineActiveRef.current = true;
          if (sessionRef.current !== sessionId || modeRef.current !== "active") {
            engineActiveRef.current = false;
            await voiceCancel();
            return;
          }
          const begun = await voiceBegin();
          if (!begun.ok) {
            engineActiveRef.current = false;
            throw classifyIpcFailure(begun.code, begun.message);
          }
        });
        if (sessionRef.current !== sessionId || modeRef.current !== "active") {
          disposeCurrentResources();
          await cancelEngine();
          return;
        }

        source.connect(worklet);
        worklet.connect(mute);
        mute.connect(context.destination);
        micTrace("capture graph connected", { contextRate: context.sampleRate });
        setState("listening");
      } catch (error) {
        if (stream && resourcesRef.current === null) {
          for (const track of stream.getTracks()) track.stop();
        }
        const classified =
          error && typeof error === "object" && "kind" in error
            ? (error as MicFailure)
            : classifyCaptureError(error);
        await failSession(sessionId, classified);
      }
    })();
  }, [cancelEngine, disposeCurrentResources, failSession, handleWorkletMessage, setState]);

  const stop = useCallback((): void => {
    if (modeRef.current !== "active") return;
    const sessionId = sessionRef.current;
    const resources = resourcesRef.current;
    modeRef.current = "ending";
    setState("transcribing");
    disposeCurrentResources();

    void (async () => {
      try {
        await feedChainRef.current;
        if (!resources || sessionRef.current !== sessionId || modeRef.current !== "ending") return;

        micTrace("stopping capture", {
          inputBlocks: resources.inputBlocks,
          inputSamples: resources.inputSamples,
          peak: resources.peak,
        });
        if (resources.inputBlocks === 0) {
          await failSession(sessionId, {
            kind: "CAPTURE_FAILURE",
            message: "The microphone worklet produced no audio blocks. Check the selected input device and try again.",
          });
          return;
        }
        if (resources.peak < SILENT_INPUT_PEAK) {
          await failSession(sessionId, {
            kind: "SILENT_INPUT",
            message: "No microphone signal was detected. Check the input device and speak closer to the microphone.",
          });
          return;
        }

        pushSamples(sessionId, resources, resources.resampler.flush());
        for (const chunk of resources.chunker.flush()) enqueueChunk(sessionId, chunk);
        await feedChainRef.current;
        if (sessionRef.current !== sessionId || modeRef.current !== "ending") return;

        const ended = await commandQueueRef.current.run(async () => {
          if (sessionRef.current !== sessionId || modeRef.current !== "ending") return null;
          return voiceEnd();
        });
        if (!ended) return;
        engineActiveRef.current = false;
        if (!ended.ok) {
          await failSession(sessionId, classifyIpcFailure(ended.code, ended.message));
        }
      } catch (error) {
        await failSession(sessionId, classifyCaptureError(error));
      }
    })();
  }, [disposeCurrentResources, enqueueChunk, failSession, pushSamples, setState]);

  const cancel = useCallback((): void => {
    if (modeRef.current === "idle") return;
    const sessionId = sessionRef.current;
    modeRef.current = "cancelled";
    setPartial("");
    setState("cancelled");
    disposeCurrentResources();
    void (async () => {
      await feedChainRef.current;
      if (sessionRef.current !== sessionId) return;
      await cancelEngine();
    })();
  }, [cancelEngine, disposeCurrentResources, setState]);

  const prepareModels = useCallback((): void => {
    if (preparing || failure?.kind !== "MODEL_MISSING" || modeRef.current !== "failed") return;
    setPreparing(true);
    void (async () => {
      try {
        const result = await prepareVoiceModels();
        if (!mountedRef.current.isMounted()) return;
        if (result.ok) {
          modeRef.current = "idle";
          setFailure(null);
          setState("idle");
        } else {
          setFailure(classifyIpcFailure(result.code, result.message));
        }
      } catch (error) {
        if (mountedRef.current.isMounted()) setFailure(classifyCaptureError(error));
      } finally {
        if (mountedRef.current.isMounted()) setPreparing(false);
      }
    })();
  }, [failure, preparing, setState]);

  useEffect(() => {
    // Re-arm on every effect run: React StrictMode's development double
    // mount runs setup → cleanup → setup on the same instance, and the
    // cleanup below unconditionally disarms. Without this line the hook
    // stays dead for the app's lifetime and every setState is a no-op.
    // Idiom: useVoicePlayback's subscription effect (mountedRef.current
    // = true at setup). Lifecycle verified by tests/mounted-guard.test.ts.
    mountedRef.current.arm();
    let unsubscribe: (() => void) | null = null;
    try {
      unsubscribe = subscribeVoiceEvents((event: VoiceEventPayload) => {
        if (!mountedRef.current.isMounted()) return;
        if (event.kind === "state") {
          if (event.state === "listening" && modeRef.current === "active") setState("listening");
          if (event.state === "transcribing" && modeRef.current !== "cancelled") setState("transcribing");
          if (event.state === "cancelled" && modeRef.current !== "failed") setState("cancelled");
          if (event.state === "cancelled") engineActiveRef.current = false;
          if (event.state === "failed" && modeRef.current !== "cancelled") {
            engineActiveRef.current = false;
            setState("failed");
          }
          return;
        }
        if (event.kind === "partial") {
          setPartial(event.transcript);
          return;
        }
        if (event.kind === "final") {
          if (finalHandledRef.current || (modeRef.current !== "ending" && modeRef.current !== "active")) {
            return;
          }
          finalHandledRef.current = true;
          setTranscript(event.transcript);
          setPartial("");
          modeRef.current = "idle";
          engineActiveRef.current = false;
          setState("transcript_ready");
          onTranscriptReadyRef.current(event.transcript);
          return;
        }
        if (event.kind === "failure") {
          if (modeRef.current === "failed") return;
          if (modeRef.current === "cancelled" && event.code !== "RECORDING_CANCELLED") return;
          const nextFailure = classifyIpcFailure(event.code, event.message);
          if (event.code === "RECORDING_CANCELLED") {
            modeRef.current = "cancelled";
            setState("cancelled");
          } else {
            engineActiveRef.current = false;
            modeRef.current = "failed";
            setState("failed");
          }
          setFailure(nextFailure);
          disposeCurrentResources();
        }
      });
    } catch {
      // The UI can still report capture errors when opened outside Electron;
      // the bridge absence is surfaced when the user explicitly starts.
    }
    return () => {
      mountedRef.current.disarm();
      modeRef.current = "cancelled";
      sessionRef.current += 1;
      disposeCurrentResources();
      void cancelEngine();
      unsubscribe?.();
    };
  }, [cancelEngine, disposeCurrentResources, setState]);

  return { state, partial, transcript, failure, preparing, start, prepareModels, stop, cancel };
}
