import test from "node:test";
import assert from "node:assert/strict";
import { mergeVoiceTranscript } from "../shared/voice-capture";

test("voice transcript insertion preserves an empty or typed draft predictably", () => {
  assert.equal(mergeVoiceTranscript("", "  hello there  "), "hello there");
  assert.equal(mergeVoiceTranscript("already typed", "spoken text"), "already typed\nspoken text");
  assert.equal(mergeVoiceTranscript("already typed\n", "spoken text"), "already typed\nspoken text");
  assert.equal(mergeVoiceTranscript("already typed", "   "), "already typed");
});
