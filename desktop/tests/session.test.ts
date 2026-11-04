/** Session identifier contract tests (mirror of backend/core/security.py). */

import test from "node:test";
import assert from "node:assert/strict";
import {
  SESSION_ID_PATTERN,
  generateSessionId,
  getSessionId,
  maskSessionId,
} from "../electron/main/session";

test("generated session ids satisfy the backend pattern", () => {
  for (const id of [generateSessionId(), generateSessionId()]) {
    assert.match(id, SESSION_ID_PATTERN);
    assert.equal(id.length, 64);
  }
});

test("pattern rejects malformed identifiers", () => {
  assert.equal(SESSION_ID_PATTERN.test("a".repeat(15)), false, "below 16 chars denied");
  assert.equal(SESSION_ID_PATTERN.test("a".repeat(16)), true);
  assert.equal(SESSION_ID_PATTERN.test("a".repeat(128)), true);
  assert.equal(SESSION_ID_PATTERN.test("a".repeat(129)), false, "above 128 chars denied");
  assert.equal(SESSION_ID_PATTERN.test("bad id!!!"), false, "illegal characters denied");
  assert.equal(SESSION_ID_PATTERN.test(""), false);
});

test("the per-process session id is stable and unique across processes", () => {
  // Stability within one "process" (this module instance).
  assert.equal(getSessionId(), getSessionId());
  // A fresh generation never collides with a cached id.
  assert.notEqual(getSessionId(), generateSessionId());
});

test("masking never exposes the raw identifier", () => {
  const id = "ab12cd34ef56gh78ij90kl12mn34op56";
  const masked = maskSessionId(id);
  assert.match(masked, /^ab12cd\u2026op56$/);
  assert.equal(masked.includes(id.slice(7, -5)), false, "the middle stays hidden");
  assert.equal(maskSessionId("too-short"), "…");
});
