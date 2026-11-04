/**
 * Global augmentation for the narrowly exposed preload bridge.
 * `window.jarvis` exists ONLY because electron/preload/preload.ts
 * exposes exactly the methods in shared PRELOAD_METHODS.
 */

import type { JarvisApi } from "../../../shared/ipc-contract";

declare global {
  interface Window {
    readonly jarvis: Readonly<JarvisApi>;
  }
}

export {};
