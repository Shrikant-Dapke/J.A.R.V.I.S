/**
 * Microphone audio pipeline primitives (Phase 6.4-c).
 *
 * Pure, testable DSP used between the renderer AudioWorklet tap and the
 * `jarvis:voice-feed` IPC channel:
 *
 *   AudioWorklet tap (device/context rate, N channels)
 *     → mixToMono (worklet side)
 *     → StreamingPcmResampler (explicit device → 16 kHz resample)
 *     → float32ToPcm16Bytes (mono PCM16 little-endian)
 *     → Pcm16Chunker (fixed VOICE_MIC_CHUNK_SAMPLES frames)
 *
 * The STT engine requires exactly `VOICE_PCM_SAMPLE_RATE` (16 kHz) mono;
 * nothing here invents a rate — the source rate is always read from the
 * live AudioContext and carried explicitly through the resampler.
 */

import {
  VOICE_PCM_CHUNK_MAX_BYTES,
  VOICE_PCM_SAMPLE_RATE,
} from "./ipc-contract";

/** Mix N channel buffers of equal length to a single mono buffer. */
export function mixToMono(channels: Float32Array[]): Float32Array {
  if (channels.length === 0) {
    throw new Error("mixToMono requires at least one channel.");
  }
  const length = channels[0].length;
  for (const channel of channels) {
    if (channel.length !== length) {
      throw new Error("mixToMono channels must share one length.");
    }
  }
  if (channels.length === 1) return channels[0];
  const mono = new Float32Array(length);
  const gain = 1 / channels.length;
  for (let i = 0; i < length; i += 1) {
    let sum = 0;
    for (const channel of channels) sum += channel[i];
    mono[i] = sum * gain;
  }
  return mono;
}

/* ------------------------------------------------------------------ */
/* Streaming windowed-sinc resampler                                   */
/* ------------------------------------------------------------------ */

/**
 * Half-width of the sinc FIR in source samples (±16 → 33-tap kernel).
 * Long enough to strongly attenuate everything above the 16 kHz target's
 * Nyquist (8 kHz) while keeping per-chunk CPU cost trivial.
 */
const SINC_HALF_TAPS = 16;

/**
 * Windowed-sinc low-pass kernel evaluated at source-sample offset `x`,
 * normalized so its DC gain over the interpolation grid is ~1 (outputs
 * are additionally divided by the exact tap sum, see emitAvailable).
 *
 * `gain` = min(1, targetRate/sourceRate): for downsampling the cutoff
 * sits at the target Nyquist (antialiasing); for upsampling the source
 * is already bandlimited so the kernel degenerates to interpolation.
 */
function windowedSinc(x: number, gain: number): number {
  const px = Math.PI * gain * x;
  const sinc = Math.abs(px) < 1e-9 ? 1 : Math.sin(px) / px;
  // 33-tap Blackman window over x ∈ [-K, K]: symmetric form, exactly 0
  // at ±K and 1 at 0.
  const ax = Math.abs(x);
  const window =
    ax >= SINC_HALF_TAPS
      ? 0
      : 0.42 -
        0.5 * Math.cos((Math.PI * x) / SINC_HALF_TAPS) +
        0.08 * Math.cos((2 * Math.PI * x) / SINC_HALF_TAPS);
  return gain * sinc * window;
}

/**
 * Streaming windowed-sinc resampler with explicit, persistent state:
 * push input blocks at `sourceRate` (the LIVE AudioContext rate — never
 * an assumed constant), receive output blocks at `targetRate` (16 kHz).
 *
 * Output positions advance by `sourceRate / targetRate` source samples
 * with fractional grid alignment; the fractional phase is carried across
 * push() calls so block boundaries never click. Each output is the
 * windowed-sinc weighted sum of its ±16 source-sample neighborhood,
 * divided by the kernel's tap sum (exact unity DC gain).
 *
 * The FIR's group delay (~K source samples) means trailing outputs are
 * produced once by `flush()` at end-of-capture (zero-padded) — never
 * mid-stream. `flush()` is idempotent; `push()` after `flush()` throws.
 */
export class StreamingPcmResampler {
  /** Pending source samples, indexed from `base`. */
  private readonly buffer: number[] = [];
  /** Absolute source index of buffer[0]. */
  private base = 0;
  /** Absolute (fractional) source index of the next output center. */
  private nextPos = SINC_HALF_TAPS;
  /** Set by flush(); flush is idempotent, push afterwards is refused. */
  private drained = false;

  constructor(
    readonly sourceRate: number,
    readonly targetRate: number = VOICE_PCM_SAMPLE_RATE,
  ) {
    if (
      !Number.isFinite(sourceRate) ||
      !Number.isInteger(sourceRate) ||
      sourceRate < 8_000 ||
      sourceRate > 192_000
    ) {
      throw new Error(`Unsupported source sample rate: ${sourceRate}`);
    }
    if (sourceRate === targetRate) {
      // Identity is a pass-through: no kernel state is maintained.
      return;
    }
    if (
      !Number.isFinite(targetRate) ||
      targetRate !== VOICE_PCM_SAMPLE_RATE
    ) {
      throw new Error(`Only the ${VOICE_PCM_SAMPLE_RATE} Hz target is supported, got ${targetRate}.`);
    }
  }

  /** Source samples consumed per output sample (>1 means downsampling). */
  private get ratio(): number {
    return this.sourceRate / this.targetRate;
  }

  /** Kernel cutoff gain: 1 when upsampling, target/source when downsampling. */
  private get gain(): number {
    return Math.min(1, this.targetRate / this.sourceRate);
  }

  /** Resamples pushed input; returns all newly-produced 16 kHz samples. */
  push(block: Float32Array): Float32Array {
    if (this.drained) {
      throw new Error("StreamingPcmResampler was flushed; start a new one.");
    }
    if (block.length === 0) return new Float32Array(0);
    if (this.ratio === 1) {
      this.base += block.length;
      this.nextPos += block.length;
      return block;
    }
    for (let i = 0; i < block.length; i += 1) this.buffer.push(block[i]);
    return Float32Array.from(this.emitAvailable());
  }

  /** End-of-capture tail flush (zero-padded); idempotent afterwards. */
  flush(): Float32Array {
    if (this.drained) return new Float32Array(0);
    this.drained = true;
    if (this.ratio === 1) return new Float32Array(0);
    // Pad long enough for every remaining output center (≤ last real
    // source index) plus its +K tap look-ahead to become available.
    for (let i = 0; i < 2 * SINC_HALF_TAPS; i += 1) this.buffer.push(0);
    const outputs = this.emitAvailable();
    this.buffer.length = 0;
    return Float32Array.from(outputs);
  }

  /** Emit every output whose full ±16-tap neighborhood is available. */
  private emitAvailable(): number[] {
    const outputs: number[] = [];
    const newest = this.base + this.buffer.length - 1;
    for (;;) {
      const center = Math.floor(this.nextPos);
      if (center + SINC_HALF_TAPS > newest) break;
      const frac = this.nextPos - center;
      let sum = 0;
      let norm = 0;
      for (let j = -SINC_HALF_TAPS; j <= SINC_HALF_TAPS; j += 1) {
        const m = center + j;
        const h = windowedSinc(frac - j, this.gain);
        norm += h;
        const idx = m - this.base;
        if (idx >= 0 && idx < this.buffer.length) sum += h * this.buffer[idx];
      }
      // Dividing by the full kernel tap sum keeps DC gain exactly 1 for
      // every fractional phase; out-of-stream taps (edge windows, flush
      // padding) contribute 0 data and fade naturally.
      outputs.push(sum / norm);
      this.nextPos += this.ratio;
    }
    // Trim consumed history, keeping exactly the taps the next output
    // (and its -K look-behind) still needs.
    const keepFrom =
      Math.floor(this.nextPos) - SINC_HALF_TAPS - this.base;
    if (keepFrom > 0) {
      this.buffer.splice(0, keepFrom);
      this.base += keepFrom;
    }
    return outputs;
  }
}

/* ------------------------------------------------------------------ */
/* PCM16 codec + chunk framing                                         */
/* ------------------------------------------------------------------ */

/**
 * Encode mono float32 (-1…+1) as little-endian PCM16 bytes; out-of-range
 * values are clamped, never wrapped.
 */
export function float32ToPcm16Bytes(samples: Float32Array): ArrayBuffer {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    out[i] = Math.round(clamped * 32_767);
  }
  return out.buffer as ArrayBuffer;
}

/** Decode bounded little-endian PCM16 bytes back to float32 (-1…+1). */
export function pcm16BytesToFloat32(buffer: ArrayBuffer): Float32Array {
  const view = new Int16Array(buffer);
  const out = new Float32Array(view.length);
  for (let i = 0; i < view.length; i += 1) out[i] = view[i] / 32_768;
  return out;
}

/**
 * Frames a running 16 kHz PCM16 sample stream into fixed-size chunks;
 * `flush()` emits any remainder as one final shorter chunk (the channel
 * validator accepts ≥ 1 sample). Each flush is idempotent afterwards.
 */
export class Pcm16Chunker {
  private carry: Int16Array = new Int16Array(0);
  private drained = false;

  constructor(readonly chunkSamples: number) {
    if (
      !Number.isInteger(chunkSamples) ||
      chunkSamples < 1 ||
      chunkSamples * 2 > VOICE_PCM_CHUNK_MAX_BYTES
    ) {
      throw new Error(`Unsupported chunk sample count: ${chunkSamples}`);
    }
  }

  /** Returns zero or more complete fixed-size chunks (ArrayBuffers). */
  push(samples: Int16Array): ArrayBuffer[] {
    if (this.drained) {
      throw new Error("Pcm16Chunker was flushed; start a new one.");
    }
    const combined = concatInt16(this.carry, samples);
    const chunks: ArrayBuffer[] = [];
    let offset = 0;
    while (offset + this.chunkSamples <= combined.length) {
      chunks.push(
        new Int16Array(
          combined.slice(offset, offset + this.chunkSamples),
        ).buffer as ArrayBuffer,
      );
      offset += this.chunkSamples;
    }
    this.carry = combined.slice(offset);
    return chunks;
  }

  /** Emit the remaining samples (may be empty); idempotent afterwards. */
  flush(): ArrayBuffer[] {
    if (this.drained || this.carry.length === 0) {
      this.drained = true;
      return [];
    }
    this.drained = true;
    const out = [new Int16Array(this.carry).buffer as ArrayBuffer];
    this.carry = new Int16Array(0);
    return out;
  }
}

function concatInt16(a: Int16Array, b: Int16Array): Int16Array {
  if (a.length === 0) return b;
  if (b.length === 0) return a;
  const out = new Int16Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}
