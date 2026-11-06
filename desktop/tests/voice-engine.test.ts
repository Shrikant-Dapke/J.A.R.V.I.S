/**
 * Pure voice state machine tests (Phase 6.4-a).
 *
 * Covers: legal/illegal transitions, the cancellation path, failure
 * recovery, transcript/partial storage semantics, action payload
 * bounds, and strict main→renderer event validation.
 */

import test from "node:test";
import assert from "node:assert/strict";
import {
  VOICE_ERROR_CODES,
  VOICE_ERROR_COPY,
  VOICE_EVENT_KINDS,
  VOICE_LEGAL_ACTIONS,
  VOICE_STATES,
  VOICE_TRANSITIONS,
  createVoiceMachine,
  isVoiceErrorCode,
  isVoiceState,
  validateVoiceAction,
  validateVoiceEventPayload,
  type VoiceActionType,
  type VoiceState,
} from "../shared/voice-engine";
import {
  VOICE_FAILURE_MESSAGE_MAX_LENGTH,
  VOICE_PCM_CHUNK_MAX_BYTES,
  VOICE_SEQUENCE_MAX,
  VOICE_TEXT_MAX_LENGTH,
} from "../shared/ipc-contract";

/* -------------------------------------------------------------------- */
/* Static document tables                                                */
/* -------------------------------------------------------------------- */

test("states, actions, and failure vocabulary are the documented closed sets", () => {
  assert.deepEqual(VOICE_STATES.length, 8);
  for (const state of VOICE_STATES) {
    assert.ok(VOICE_LEGAL_ACTIONS[state] !== undefined);
    assert.ok(isVoiceState(state));
  }
  assert.equal(isVoiceState("astral"), false);
  assert.equal(isVoiceState(42), false);

  assert.deepEqual(VOICE_EVENT_KINDS, ["state", "partial", "final", "tts-chunk", "tts-ended", "failure"]);

  assert.equal(VOICE_ERROR_CODES.length, 6);
  for (const code of VOICE_ERROR_CODES) {
    assert.ok(isVoiceErrorCode(code));
    assert.match(VOICE_ERROR_COPY[code], /\S/u);
  }
  assert.equal(isVoiceErrorCode("SJD_CODE"), false);
  assert.equal(isVoiceErrorCode(null), false);
});

test("VOICE_TRANSITIONS mirrors VOICE_LEGAL_ACTIONS exactly", () => {
  for (const state of VOICE_STATES) {
    for (const action of Object.keys(VOICE_TRANSITIONS) as VoiceActionType[]) {
      const legal = VOICE_LEGAL_ACTIONS[state].includes(action);
      const mirrored = VOICE_TRANSITIONS[action].includes(state);
      assert.equal(legal, mirrored, `${action} in ${state}`);
    }
  }
});

/* -------------------------------------------------------------------- */
/* Happy-path flows                                                      */
/* -------------------------------------------------------------------- */

test("the full capture flow: arm → begin → partial → end → finalize", () => {
  const machine = createVoiceMachine();
  assert.equal(machine.getState(), "idle");
  assert.equal(machine.getTranscript(), null);
  assert.equal(machine.getPartial(), null);

  assert.deepEqual(machine.dispatch({ type: "arm" }), { ok: true, state: "armed" });
  assert.deepEqual(machine.dispatch({ type: "begin_capture" }), { ok: true, state: "listening" });

  const partial = machine.dispatch({ type: "partial", transcript: "status" });
  assert.deepEqual(partial, { ok: true, state: "listening" });
  assert.equal(machine.getPartial(), "status");
  assert.equal(machine.getState(), "listening", "partial never changes the state");

  assert.deepEqual(machine.dispatch({ type: "end_capture" }), { ok: true, state: "transcribing" });
  const final = machine.dispatch({
    type: "finalize",
    transcript: "status report, please",
  });
  assert.deepEqual(final, { ok: true, state: "transcript_ready" });
  assert.equal(machine.getTranscript(), "status report, please");
  assert.equal(machine.getPartial(), null, "finalize clears the interim text");
});

test("TTS flow from idle and from transcript_ready", () => {
  const fromIdle = createVoiceMachine();
  assert.deepEqual(fromIdle.dispatch({ type: "speak_begin" }), { ok: true, state: "speaking" });
  assert.deepEqual(fromIdle.dispatch({ type: "speak_end" }), { ok: true, state: "idle" });

  const fromReady = createVoiceMachine();
  fromReady.dispatch({ type: "arm" });
  fromReady.dispatch({ type: "begin_capture" });
  fromReady.dispatch({ type: "end_capture" });
  fromReady.dispatch({ type: "finalize", transcript: "hello" });
  assert.equal(fromReady.getState(), "transcript_ready");
  assert.deepEqual(fromReady.dispatch({ type: "speak_begin" }), { ok: true, state: "speaking" });
  assert.deepEqual(fromReady.dispatch({ type: "speak_end" }), { ok: true, state: "idle" });
});

test("re-arming after a transcript discards it (new utterance)", () => {
  const machine = createVoiceMachine();
  machine.dispatch({ type: "arm" });
  machine.dispatch({ type: "begin_capture" });
  machine.dispatch({ type: "partial", transcript: "draft" });
  machine.dispatch({ type: "end_capture" });
  machine.dispatch({ type: "finalize", transcript: "final text" });
  assert.equal(machine.getTranscript(), "final text");

  assert.deepEqual(machine.dispatch({ type: "arm" }), { ok: true, state: "armed" });
  assert.equal(machine.getTranscript(), null);
  assert.equal(machine.getPartial(), null);
});

/* -------------------------------------------------------------------- */
/* Illegal transitions                                                   */
/* -------------------------------------------------------------------- */

test("every action is refused in every state outside its legality table", () => {
  for (const state of VOICE_STATES) {
    for (const action of Object.keys(VOICE_TRANSITIONS) as VoiceActionType[]) {
      if (VOICE_LEGAL_ACTIONS[state].includes(action)) continue;
      const machine = createVoiceMachine();
      drive(machine, state);
      const before = {
        transcript: machine.getTranscript(),
        partial: machine.getPartial(),
      };
      const result = machine.dispatch(sampleAction(action));
      assert.equal(result.ok, false, `${action} must be illegal in ${state}`);
      assert.equal(machine.getState(), state, "refused dispatch leaves the state unchanged");
      assert.deepEqual(
        {
          transcript: machine.getTranscript(),
          partial: machine.getPartial(),
        },
        before,
        "refused dispatch leaves transcript/partial untouched",
      );
    }
  }
});

/** Drive the machine from idle into the requested state via legal actions. */
function drive(
  machine: ReturnType<typeof createVoiceMachine>,
  target: VoiceState | "idle",
): void {
  const routes: Record<VoiceState | "idle", VoiceActionType[]> = {
    idle: [],
    armed: ["arm"],
    listening: ["arm", "begin_capture"],
    transcribing: ["arm", "begin_capture", "end_capture"],
    transcript_ready: ["arm", "begin_capture", "end_capture", "finalize"],
    speaking: ["arm", "begin_capture", "end_capture", "finalize", "speak_begin"],
    cancelled: ["arm", "begin_capture", "cancel"],
    failed: ["arm", "begin_capture", "fail"],
  };
  for (const step of routes[target]) machine.dispatch(sampleAction(step));
}

function sampleAction(type: VoiceActionType): unknown {
  switch (type) {
    case "partial":
      return { type: "partial", transcript: "demo" };
    case "finalize":
      return { type: "finalize", transcript: "demo" };
    case "fail":
      return { type: "fail", code: "STT_FAILURE", message: "demo" };
    default:
      return { type };
  }
}

/* -------------------------------------------------------------------- */
/* Cancellation, failure, recovery                                       */
/* -------------------------------------------------------------------- */

test("cancel from every session state lands in cancelled with RECORDING_CANCELLED", () => {
  for (const state of ["armed", "listening", "transcribing", "transcript_ready", "speaking"] as const) {
    const machine = createVoiceMachine();
    drive(machine, state);
    const result = machine.dispatch({ type: "cancel" });
    assert.deepEqual(result, { ok: true, state: "cancelled" }, state);
    assert.equal(machine.getState(), "cancelled");
    assert.equal(machine.getPartial(), null);
    const failure = machine.getFailure();
    assert.ok(failure, state);
    assert.equal(failure.code, "RECORDING_CANCELLED");
    assert.equal(failure.message, VOICE_ERROR_COPY["RECORDING_CANCELLED"]);

    // Terminal until reset: no further capture from cancelled.
    assert.equal(machine.dispatch({ type: "arm" }).ok, false);
    assert.equal(machine.dispatch({ type: "speak_begin" }).ok, false);
    assert.deepEqual(machine.dispatch({ type: "reset" }), { ok: true, state: "idle" });
    assert.equal(machine.getFailure(), null);
    assert.equal(machine.getState(), "idle");
  }
});

test("fail records the engine failure; reset restores an empty idle", () => {
  const machine = createVoiceMachine();
  machine.dispatch({ type: "arm" });
  machine.dispatch({ type: "begin_capture" });
  const result = machine.dispatch({
    type: "fail",
    code: "STT_FAILURE",
    message: "recognizer threw",
  });
  assert.deepEqual(result, { ok: true, state: "failed" });
  assert.deepEqual(machine.getFailure(), { code: "STT_FAILURE", message: "recognizer threw" });

  assert.equal(machine.dispatch({ type: "arm" }).ok, false, "failed is terminal until reset");
  assert.deepEqual(machine.dispatch({ type: "reset" }), { ok: true, state: "idle" });
  assert.equal(machine.getFailure(), null);
  assert.equal(machine.getTranscript(), null);
  assert.equal(machine.getPartial(), null);

  // Recovery proven by reuse: full second capture succeeds after reset.
  assert.deepEqual(machine.dispatch({ type: "arm" }), { ok: true, state: "armed" });
  machine.dispatch({ type: "begin_capture" });
  machine.dispatch({ type: "end_capture" });
  assert.deepEqual(machine.dispatch({ type: "finalize", transcript: "again" }), {
    ok: true,
    state: "transcript_ready",
  });
  assert.equal(machine.getTranscript(), "again");
});

/* -------------------------------------------------------------------- */
/* Action payload bounds                                                 */
/* -------------------------------------------------------------------- */

test("partial accepts empty text but respects the string bound", () => {
  const machine = createVoiceMachine();
  machine.dispatch({ type: "arm" });
  machine.dispatch({ type: "begin_capture" });

  assert.equal(machine.dispatch({ type: "partial", transcript: "" }).ok, true);
  assert.equal(machine.getPartial(), "");

  assert.equal(
    machine.dispatch({ type: "partial", transcript: "x".repeat(VOICE_TEXT_MAX_LENGTH) }).ok,
    true,
    "the 2000-char ceiling passes",
  );
  assert.equal(
    machine.dispatch({ type: "partial", transcript: "x".repeat(VOICE_TEXT_MAX_LENGTH + 1) }).ok,
    false,
  );
  assert.equal(machine.dispatch({ type: "partial", transcript: 42 }).ok, false);
});

test("finalize requires non-empty, bounded text", () => {
  const machine = createVoiceMachine();
  machine.dispatch({ type: "arm" });
  machine.dispatch({ type: "begin_capture" });
  machine.dispatch({ type: "end_capture" });

  for (const bad of ["", "   ", "x".repeat(VOICE_TEXT_MAX_LENGTH + 1), null, 42]) {
    assert.equal(machine.dispatch({ type: "finalize", transcript: bad }).ok, false, String(bad));
  }
  assert.equal(
    machine.dispatch({ type: "finalize", transcript: "x".repeat(VOICE_TEXT_MAX_LENGTH) }).ok,
    true,
    "the 2000-char ceiling passes",
  );
});

test("malformed action shapes refuse without touching the machine", () => {
  const machine = createVoiceMachine();
  machine.dispatch({ type: "arm" });
  machine.dispatch({ type: "begin_capture" });
  machine.dispatch({ type: "partial", transcript: "kept" });

  const before = { state: machine.getState(), partial: machine.getPartial() };

  for (const raw of [
    null,
    undefined,
    42,
    "arm",
    [],
    {},
    { type: 42 },
    { type: "nonexistent" },
    { type: "cancel", extra: "field" },
    { type: "reset", force: true },
    { type: "partial", transcript: "hello", spam: 1 },
    { type: "fail", code: "STT_FAILURE" },
    { type: "fail", code: "NOT_A_CODE", message: "boom" },
    { type: "fail", code: "STT_FAILURE", message: "" },
    { type: "fail", code: "STT_FAILURE", message: "   " },
    {
      type: "fail",
      code: "STT_FAILURE",
      message: "x".repeat(VOICE_FAILURE_MESSAGE_MAX_LENGTH + 1),
    },
  ]) {
    const result = machine.dispatch(raw);
    assert.equal(result.ok, false, JSON.stringify(raw));
  }

  // validateVoiceAction independently agrees on the same closed outcomes.
  assert.equal(validateVoiceAction({ type: "arm", extra: 1 }).ok, false);
  assert.equal(validateVoiceAction({ type: "partial", transcript: "ok" }).ok, true);
  assert.equal(validateVoiceAction({ type: "fail", code: "STT_FAILURE", message: "m" }).ok, true);

  assert.deepEqual(
    { state: machine.getState(), partial: machine.getPartial() },
    before,
    "garbage never mutated the machine",
  );
});

/* -------------------------------------------------------------------- */
/* Event validation (main → renderer, jarvis:voice-events)               */
/* -------------------------------------------------------------------- */

test("every documented event kind validates and round-trips", () => {
  const samples: Array<{ input: unknown; expected: Parameters<typeof assert.deepEqual>[1] }> = [
    { input: { kind: "state", state: "listening" }, expected: { kind: "state", state: "listening" } },
    { input: { kind: "partial", transcript: "" }, expected: { kind: "partial", transcript: "" } },
    { input: { kind: "final", transcript: "hello" }, expected: { kind: "final", transcript: "hello" } },
    {
      input: { kind: "tts-chunk", pcm: new ArrayBuffer(4), sequence: 0, sampleRate: 24_000 },
      expected: { kind: "tts-chunk", pcm: new ArrayBuffer(4), sequence: 0, sampleRate: 24_000 },
    },
    { input: { kind: "tts-ended" }, expected: { kind: "tts-ended" } },
    {
      input: { kind: "failure", code: "MODEL_MISSING", message: "no model" },
      expected: { kind: "failure", code: "MODEL_MISSING", message: "no model" },
    },
  ];
  for (const { input, expected } of samples) {
    const validated = validateVoiceEventPayload(input);
    assert.equal(validated.ok, true, JSON.stringify(input));
    if (validated.ok) assert.deepEqual(validated.value, expected);
  }
});

test("event shapes reject unknown kinds and extra keys", () => {
  for (const bad of [
    null,
    undefined,
    42,
    "state",
    [],
    {},
    { kind: 42 },
    { kind: "ghost" },
    { kind: "tts-ended", ridealong: true },
    { kind: "state", state: "listening", spam: 1 },
    { kind: "partial", transcript: "ok", spam: 1 },
    { kind: "state" }, // missing state
    { kind: "partial" }, // missing transcript
    { kind: "tts-chunk", pcm: new ArrayBuffer(2) }, // missing sequence/rate
    { kind: "tts-chunk", pcm: new ArrayBuffer(2), sequence: 1 }, // missing sampleRate
    { kind: "failure", code: "STT_FAILURE" }, // missing message
  ]) {
    assert.equal(validateVoiceEventPayload(bad).ok, false, JSON.stringify(bad));
  }
});

test("event value bounds: transcript length, PCM bytes, sequence range", () => {
  // Transcript bounds (partial may be empty; final may not).
  assert.equal(
    validateVoiceEventPayload({ kind: "partial", transcript: "x".repeat(VOICE_TEXT_MAX_LENGTH) }).ok,
    true,
  );
  assert.equal(
    validateVoiceEventPayload({ kind: "partial", transcript: "x".repeat(VOICE_TEXT_MAX_LENGTH + 1) })
      .ok,
    false,
  );
  assert.equal(validateVoiceEventPayload({ kind: "partial", transcript: 42 }).ok, false);
  assert.equal(validateVoiceEventPayload({ kind: "final", transcript: "   " }).ok, false);
  assert.equal(
    validateVoiceEventPayload({ kind: "final", transcript: "x".repeat(VOICE_TEXT_MAX_LENGTH + 1) })
      .ok,
    false,
  );

  // PCM bounds (sample rate is carried on EVERY chunk now — model-reported).
  const chunk = (pcm: unknown, sequence: unknown, sampleRate: unknown = 24_000) =>
    validateVoiceEventPayload({ kind: "tts-chunk", pcm, sequence, sampleRate });
  assert.equal(chunk(new ArrayBuffer(1), 3).ok, true, "1 byte is a legal chunk");
  assert.equal(
    chunk(new ArrayBuffer(VOICE_PCM_CHUNK_MAX_BYTES), 3).ok,
    true,
    "the 65_536-byte ceiling passes",
  );
  assert.equal(chunk(new ArrayBuffer(VOICE_PCM_CHUNK_MAX_BYTES + 1), 3).ok, false);
  assert.equal(chunk(new ArrayBuffer(0), 3).ok, false, "empty chunks are refused");
  assert.equal(chunk(new Float32Array(4), 3).ok, false, "only ArrayBuffer is accepted");
  assert.equal(chunk("pcm", 3).ok, false);
  assert.equal(chunk(null, 3).ok, false);

  // Sample-rate bounds: an integer within 8000..96000 (never hardcoded).
  for (const badRate of [7999, 96_001, 24_000.5, "24000", null, NaN, Infinity]) {
    assert.equal(chunk(new ArrayBuffer(2), 0, badRate).ok, false, `rate ${String(badRate)}`);
  }
  assert.equal(chunk(new ArrayBuffer(2), 0, 8000).ok, true, "8 kHz lattice floor");
  assert.equal(chunk(new ArrayBuffer(2), 0, 96_000).ok, true, "96 kHz lattice ceiling");
  // A chunk missing the rate key entirely is refused.
  assert.equal(
    validateVoiceEventPayload({ kind: "tts-chunk", pcm: new ArrayBuffer(2), sequence: 0 }).ok,
    false,
    "sampleRate is mandatory",
  );

  // Sequence bounds.
  for (const bad of [-1, 1.5, "3", null, NaN, VOICE_SEQUENCE_MAX + 1]) {
    assert.equal(chunk(new ArrayBuffer(2), bad).ok, false, `sequence ${String(bad)}`);
  }
  assert.equal(chunk(new ArrayBuffer(2), VOICE_SEQUENCE_MAX).ok, true);

  // Failure bounds.
  const failure = (code: unknown, message: unknown) =>
    validateVoiceEventPayload({ kind: "failure", code, message });
  assert.equal(
    failure("TTS_FAILURE", "x".repeat(VOICE_FAILURE_MESSAGE_MAX_LENGTH)).ok,
    true,
    "the 512-char ceiling passes",
  );
  assert.equal(failure("TTS_FAILURE", "x".repeat(VOICE_FAILURE_MESSAGE_MAX_LENGTH + 1)).ok, false);
  assert.equal(failure("TTS_FAILURE", "").ok, false);
  assert.equal(failure("TTS_FAILURE", "   ").ok, false);
  assert.equal(failure("GHOST_CODE", "fine here").ok, false);
});
