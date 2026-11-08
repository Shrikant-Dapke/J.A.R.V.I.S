import assert from "node:assert/strict";
import { test } from "node:test";

import { MIC_AUDIO_CONSTRAINTS, MIC_CAPTURE_VIDEO } from "../shared/mic-capture-constraints";

test("mic capture constraints pin the controlled AGC experiment profile", () => {
  // The experiment changes exactly ONE processing flag from the prior
  // baseline (autoGainControl: false → true); EC and NS must stay off
  // so any STT accuracy delta is attributable to AGC alone.
  assert.deepEqual(MIC_AUDIO_CONSTRAINTS, {
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: true,
  });
});

test("processing flags are strictly boolean (no browser coercion surprises)", () => {
  assert.equal(typeof MIC_AUDIO_CONSTRAINTS.echoCancellation, "boolean");
  assert.equal(typeof MIC_AUDIO_CONSTRAINTS.noiseSuppression, "boolean");
  assert.equal(typeof MIC_AUDIO_CONSTRAINTS.autoGainControl, "boolean");
});

test("constraint object is frozen so capture callsites cannot drift", () => {
  assert.ok(Object.isFrozen(MIC_AUDIO_CONSTRAINTS));
  assert.throws(() => {
    "use strict";
    (MIC_AUDIO_CONSTRAINTS as { autoGainControl: boolean }).autoGainControl = false;
  });
});

test("video stays disabled for the voice-only capture stream", () => {
  assert.equal(MIC_CAPTURE_VIDEO, false);
});
