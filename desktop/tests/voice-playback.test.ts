import test from "node:test";
import assert from "node:assert/strict";
import { float32ToPcm16Bytes } from "../shared/voice-audio";
import { BoundedTtsAudioAccumulator } from "../shared/voice-playback";

function chunk(
  requestId: number,
  sequence: number,
  samples: Float32Array,
  sampleRate = 24_000,
) {
  return {
    kind: "tts-chunk" as const,
    pcm: float32ToPcm16Bytes(samples),
    requestId,
    sequence,
    sampleRate,
    channels: 1 as const,
    encoding: "pcm16le" as const,
  };
}

function ended(requestId: number, totalSamples: number, sampleRate = 24_000) {
  return {
    kind: "tts-ended" as const,
    requestId,
    totalSamples,
    sampleRate,
    channels: 1 as const,
    encoding: "pcm16le" as const,
  };
}

test("bounded TTS assembly preserves 24 kHz metadata and PCM order", () => {
  const accumulator = new BoundedTtsAudioAccumulator(11, 8);
  assert.equal(accumulator.acceptChunk(chunk(11, 0, new Float32Array([0.25, -0.5]))).ok, true);
  assert.equal(accumulator.acceptChunk(chunk(11, 1, new Float32Array([0.75]))).ok, true);

  const result = accumulator.finish(ended(11, 3));
  assert.equal(result.ok, true);
  if (result.ok) {
    assert.equal(result.value.sampleRate, 24_000);
    assert.equal(result.value.samples.length, 3);
    assert.ok(Math.abs(result.value.samples[0] - 0.25) < 0.001);
    assert.ok(Math.abs(result.value.samples[1] + 0.5) < 0.001);
    assert.ok(Math.abs(result.value.samples[2] - 0.75) < 0.001);
  }
});

test("stale and duplicate TTS chunks cannot affect a newer request", () => {
  const accumulator = new BoundedTtsAudioAccumulator(20, 8);
  const stale = accumulator.acceptChunk(chunk(19, 0, new Float32Array([0.1])));
  assert.equal(stale.ok, false);
  if (!stale.ok) assert.equal(stale.stale, true);

  assert.equal(accumulator.acceptChunk(chunk(20, 0, new Float32Array([0.1]))).ok, true);
  const duplicate = accumulator.acceptChunk(chunk(20, 0, new Float32Array([0.9])));
  assert.equal(duplicate.ok, false);
  if (!duplicate.ok) assert.equal(duplicate.stale, true);

  const staleEnd = accumulator.finish(ended(19, 1));
  assert.equal(staleEnd.ok, false);
  if (!staleEnd.ok) assert.equal(staleEnd.stale, true);
});

test("TTS assembly rejects gaps, format changes, and sample-count mismatches", () => {
  const gap = new BoundedTtsAudioAccumulator(1, 8);
  const outOfOrder = gap.acceptChunk(chunk(1, 2, new Float32Array([0.1])));
  assert.equal(outOfOrder.ok, false);
  if (!outOfOrder.ok) assert.equal(outOfOrder.stale, false);

  const rate = new BoundedTtsAudioAccumulator(2, 8);
  assert.equal(rate.acceptChunk(chunk(2, 0, new Float32Array([0.1]), 24_000)).ok, true);
  const changedRate = rate.acceptChunk(chunk(2, 1, new Float32Array([0.1]), 22_050));
  assert.equal(changedRate.ok, false);

  const mismatch = new BoundedTtsAudioAccumulator(3, 8);
  assert.equal(mismatch.acceptChunk(chunk(3, 0, new Float32Array([0.1]))).ok, true);
  const wrongTotal = mismatch.finish(ended(3, 2));
  assert.equal(wrongTotal.ok, false);

  const bounded = new BoundedTtsAudioAccumulator(4, 1);
  const tooLarge = bounded.acceptChunk(chunk(4, 0, new Float32Array([0.1, 0.2])));
  assert.equal(tooLarge.ok, false);
});
