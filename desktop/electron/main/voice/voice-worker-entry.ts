/**
 * Electron utility-process bootstrap for the Electron-free voice worker core.
 *
 * `utilityProcess.fork()` executes this module as a CommonJS entry point; it
 * does not invoke exports from the loaded module. Keep the transport-specific
 * parent-port wiring here so `voice-worker.ts` remains usable by the Node
 * smoke test and in-memory engine tests without importing Electron.
 */

import { startVoiceWorker } from "./voice-worker";

type UtilityParentPort = {
  on: (event: "message", listener: (event: { data: unknown }) => void) => void;
  postMessage: (message: unknown) => void;
};

const parentPort = (process as NodeJS.Process & { parentPort?: UtilityParentPort | null }).parentPort;

if (!parentPort) {
  throw new Error("The voice worker must run inside an Electron utility process.");
}

startVoiceWorker({
  post: (message) => parentPort.postMessage(message),
  listen: (onRequest) => {
    parentPort.on("message", (event) => onRequest(event.data));
  },
});
