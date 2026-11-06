import test from "node:test";
import assert from "node:assert/strict";
import {
  float32ToPcm16Bytes,
  mixToMono,
  pcm16BytesToFloat32,
  Pcm16Chunker,
  StreamingPcmResampler,
} from "../shared/voice-audio";
import { VOICE_MIC_CHUNK_SAMPLES, VOICE_PCM_SAMPLE_RATE } from "../shared/ipc-contract";

function collectResampled(
  sourceRate: number,
  samples: Float32Array,
  blockSize = samples.length,
): Float32Array {
  const resampler = new StreamingPcmResampler(sourceRate);
  const output: number[] = [];
  for (let offset = 0; offset < samples.length; offset += blockSize) {
    const block = samples.slice(offset, Math.min(samples.length, offset + blockSize));
    output.push(...resampler.push(block));
  }
  output.push(...resampler.flush());
  return Float32Array.from(output);
}

function sine(rate: number, frequency: number, seconds: number, amplitude = 0.8): Float32Array {
  const samples = new Float32Array(Math.round(rate * seconds));
  for (let i = 0; i < samples.length; i += 1) {
    samples[i] = amplitude * Math.sin((2 * Math.PI * frequency * i) / rate);
  }
  return samples;
}

function rms(samples: Float32Array, start = 0, end = samples.length): number {
  let sum = 0;
  for (let i = start; i < end; i += 1) sum += samples[i] * samples[i];
  return Math.sqrt(sum / Math.max(1, end - start));
}

test("mixToMono averages channels and rejects inconsistent blocks", () => {
  const mono = mixToMono([new Float32Array([0.25, -0.5])]);
  assert.deepEqual([...mono], [0.25, -0.5]);
  assert.deepEqual(
    [...mixToMono([new Float32Array([1, -1]), new Float32Array([-0.5, 0.5])])],
    [0.25, -0.25],
  );
  assert.throws(() => mixToMono([]), /at least one channel/u);
  assert.throws(
    () => mixToMono([new Float32Array(1), new Float32Array(2)]),
    /share one length/u,
  );
});

test("identity resampling preserves 16 kHz samples and flush is empty", () => {
  const input = sine(VOICE_PCM_SAMPLE_RATE, 440, 0.1);
  const resampler = new StreamingPcmResampler(VOICE_PCM_SAMPLE_RATE);
  const output = resampler.push(input);
  assert.strictEqual(output, input, "identity conversion is a bounded pass-through");
  assert.deepEqual([...resampler.flush()], []);
});

test("44.1 kHz and 48 kHz streams are explicitly resampled to 16 kHz", () => {
  for (const sourceRate of [44_100, 48_000]) {
    const output = collectResampled(sourceRate, sine(sourceRate, 440, 1), 257);
    assert.ok(
      Math.abs(output.length - VOICE_PCM_SAMPLE_RATE) <= 1,
      `${sourceRate} produced ${output.length} samples`,
    );
    assert.ok(output.every(Number.isFinite), `${sourceRate} output is finite`);
    assert.ok(rms(output, 100, output.length - 100) > 0.2, `${sourceRate} tone survived`);
  }
});

test("streaming resampling is continuous across input block boundaries", () => {
  const input = sine(48_000, 1_000, 0.5);
  const oneBlock = collectResampled(48_000, input);
  const manyBlocks = collectResampled(48_000, input, 113);
  assert.equal(manyBlocks.length, oneBlock.length);
  let maxDifference = 0;
  for (let i = 0; i < oneBlock.length; i += 1) {
    maxDifference = Math.max(maxDifference, Math.abs(oneBlock[i] - manyBlocks[i]));
  }
  assert.ok(maxDifference < 1e-6, `block boundary changed output by ${maxDifference}`);
});

test("downsampling attenuates an out-of-band 11 kHz input tone", () => {
  const wanted = collectResampled(48_000, sine(48_000, 1_000, 1));
  const aliasSource = collectResampled(48_000, sine(48_000, 11_000, 1));
  const start = 500;
  const end = wanted.length - 500;
  assert.ok(rms(aliasSource, start, end) < rms(wanted, start, end) * 0.25);
});

test("PCM16 encoding is clamped and explicitly little-endian", () => {
  const bytes = float32ToPcm16Bytes(new Float32Array([-2, -1, -0.5, 0, 0.5, 1, 2]));
  const view = new DataView(bytes);
  assert.deepEqual(
    Array.from({ length: 7 }, (_, i) => view.getInt16(i * 2, true)),
    [-32768, -32768, -16384, 0, 16384, 32767, 32767],
  );
  const decoded = pcm16BytesToFloat32(bytes);
  assert.ok(Math.abs(decoded[2] + 0.5) < 0.001);
  assert.ok(Math.abs(decoded[4] - 0.5) < 0.001);
  assert.throws(() => pcm16BytesToFloat32(new ArrayBuffer(1)), /even number/u);
});

test("PCM16 chunking emits bounded frames and flushes one remainder", () => {
  const chunker = new Pcm16Chunker(VOICE_MIC_CHUNK_SAMPLES);
  const first = chunker.push(new Int16Array(VOICE_MIC_CHUNK_SAMPLES + 17));
  assert.equal(first.length, 1);
  assert.equal(first[0].byteLength, VOICE_MIC_CHUNK_SAMPLES * 2);
  assert.equal(new DataView(first[0]).getInt16(0, true), 0);
  const remainder = chunker.flush();
  assert.equal(remainder.length, 1);
  assert.equal(remainder[0].byteLength, 17 * 2);
  assert.deepEqual(chunker.flush(), []);
  assert.throws(() => chunker.push(new Int16Array(1)), /flushed/u);
});
