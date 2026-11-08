import assert from "node:assert/strict";
import { test } from "node:test";

import { createMountedGuard } from "../shared/mounted-guard";

test("mounted guard starts live, matching useRef(true) semantics", () => {
  const guard = createMountedGuard();
  assert.equal(guard.isMounted(), true);
});

test("setup → cleanup → setup (StrictMode double mount) re-arms the guard", () => {
  // React 18 StrictMode dev cycle: the same component instance keeps its
  // refs while the effect runs setup, then cleanup, then setup again.
  // The remount's arm() call is the line that keeps the hook alive.
  const guard = createMountedGuard();
  guard.arm();
  guard.disarm();
  assert.equal(guard.isMounted(), false);
  guard.arm();
  assert.equal(guard.isMounted(), true);
});

test("cleanup without a re-arm leaves the guard disarmed", () => {
  // The original bug shape: cleanup never followed by arm() must read as
  // dismounted, so guarded state updates stay no-ops by design.
  const guard = createMountedGuard();
  guard.arm();
  guard.disarm();
  guard.disarm();
  assert.equal(guard.isMounted(), false);
});

test("arm and disarm are idempotent and independent per instance", () => {
  const guard = createMountedGuard();
  guard.disarm();
  assert.equal(guard.isMounted(), false);
  guard.arm();
  guard.arm();
  assert.equal(guard.isMounted(), true);

  const second = createMountedGuard();
  guard.disarm();
  assert.equal(second.isMounted(), true, "instances own their state");
});
