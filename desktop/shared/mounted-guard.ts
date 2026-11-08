/**
 * Live-state guard for renderer hook subscription effects.
 *
 * React 18 StrictMode's development double-mount runs every effect as
 * setup → cleanup → setup on the SAME component instance, with the hook's
 * refs preserved. A hook that disarms a mounted flag in its cleanup and
 * never re-arms it in the effect body stays dead for the app's lifetime
 * (all guarded state updates become silent no-ops). The guard encodes
 * that contract: every effect setup calls arm(), every cleanup disarm(),
 * and consumers check isMounted() before touching React state.
 *
 * Electron-free and instance-free so the lifecycle is verifiable in Node.
 */

export type MountedGuard = {
  /** Effect setup: mark live (idempotent). */
  arm: () => void;
  /** Effect cleanup: mark dead (idempotent). */
  disarm: () => void;
  isMounted: () => boolean;
};

export function createMountedGuard(): MountedGuard {
  // Begins armed, mirroring the useRef(true) initializer it replaces:
  // the hook is live from the moment its first effect setup runs.
  let mounted = true;
  return {
    arm: (): void => {
      mounted = true;
    },
    disarm: (): void => {
      mounted = false;
    },
    isMounted: (): boolean => mounted,
  };
}
