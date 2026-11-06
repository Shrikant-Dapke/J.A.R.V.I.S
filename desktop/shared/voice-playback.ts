/**
 * Pure, bounded renderer-side TTS assembly policy.
 *
 * The native worker emits incremental PCM chunks, but playback waits for the
 * authoritative `tts-ended` event before creating one AudioBuffer. Keeping
 * this policy separate makes request identity, ordering, metadata, and the
 * total-memory ceiling deterministic without requiring a browser runtime.
 */

import {
  VOICE_TTS_CHANNELS,
  VOICE_TTS_ENCODING,
  VOICE_TTS_MAX_SAMPLES,
} from "./ipc-contract";
import { pcm16BytesToFloat32 } from "./voice-audio";
import type { VoiceEventPayload } from "./voice-engine";

type TtsChunk = Extract<VoiceEventPayload, { kind: "tts-chunk" }>;
type TtsEnded = Extract<VoiceEventPayload, { kind: "tts-ended" }>;

export type PlaybackAssemblyResult<T> =
  | { ok: true; value: T }
  | { ok: false; stale: boolean; reason: string };

export type AssembledTtsAudio = {
  samples: Float32Array;
  sampleRate: number;
};

export class BoundedTtsAudioAccumulator {
  private readonly chunks: Float32Array[] = [];
  private nextSequence = 0;
  private totalSamples = 0;
  private sampleRate: number | null = null;

  constructor(
    readonly requestId: number,
    private readonly maxSamples: number = VOICE_TTS_MAX_SAMPLES,
  ) {}

  acceptChunk(chunk: TtsChunk): PlaybackAssemblyResult<null> {
    if (chunk.requestId !== this.requestId) {
      return { ok: false, stale: true, reason: "TTS chunk belongs to another playback request." };
    }
    if (chunk.sequence < this.nextSequence) {
      return { ok: false, stale: true, reason: "Duplicate or stale TTS chunk." };
    }
    if (chunk.sequence !== this.nextSequence) {
      return { ok: false, stale: false, reason: "TTS chunks arrived out of order." };
    }
    if (chunk.channels !== VOICE_TTS_CHANNELS || chunk.encoding !== VOICE_TTS_ENCODING) {
      return { ok: false, stale: false, reason: "TTS audio format is not mono PCM16 LE." };
    }
    if (this.sampleRate !== null && this.sampleRate !== chunk.sampleRate) {
      return { ok: false, stale: false, reason: "TTS chunks changed sample rate mid-playback." };
    }
    try {
      const samples = pcm16BytesToFloat32(chunk.pcm);
      if (this.totalSamples + samples.length > this.maxSamples) {
        return { ok: false, stale: false, reason: "TTS output exceeded the bounded playback limit." };
      }
      this.chunks.push(samples);
      this.totalSamples += samples.length;
      this.nextSequence += 1;
      this.sampleRate = chunk.sampleRate;
      return { ok: true, value: null };
    } catch {
      return { ok: false, stale: false, reason: "TTS PCM output was malformed." };
    }
  }

  finish(ended: TtsEnded): PlaybackAssemblyResult<AssembledTtsAudio> {
    if (ended.requestId !== this.requestId) {
      return { ok: false, stale: true, reason: "TTS completion belongs to another playback request." };
    }
    if (ended.channels !== VOICE_TTS_CHANNELS || ended.encoding !== VOICE_TTS_ENCODING) {
      return { ok: false, stale: false, reason: "TTS completion format is not mono PCM16 LE." };
    }
    if (ended.totalSamples !== this.totalSamples) {
      return { ok: false, stale: false, reason: "TTS completion sample count did not match its chunks." };
    }
    if (this.sampleRate !== ended.sampleRate) {
      return { ok: false, stale: false, reason: "TTS completion sample rate did not match its chunks." };
    }
    const samples = new Float32Array(this.totalSamples);
    let offset = 0;
    for (const chunk of this.chunks) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    return { ok: true, value: { samples, sampleRate: ended.sampleRate } };
  }
}
