/**
 * Electron `utilityProcess` transport for the voice worker
 * (Phase 6.4-b). This is the ONLY Electron-coupled file in the voice
 * stack: the engine and worker are plain Node and stay unit-testable.
 *
 * Isolation rationale: sherpa-onnx load/decode/synthesis calls are
 * synchronous native work; running them in the main process would stall
 * every other IPC task. A utility process gives a separate event loop,
 * crash isolation (an engine fault kills the worker, never the app) and
 * a bounded MessagePort-style channel (structured clone).
 *
 * Messages cross as validated structures only: outgoing requests are
 * validated here AND in the worker, responses are validated before the
 * engine consumes them. No filesystem paths beyond the workers' own
 * script location are exposed, and the renderer never sees this layer.
 */

import { utilityProcess, type UtilityProcess } from "electron";
import { join } from "node:path";
import { validateWorkerRequest, type WorkerRequest, type WorkerResponse } from "../../../shared/voice-protocol";
import type { TransportFactory, VoiceWorkerTransport } from "./voice-engine";

const SPAWN_TIMEOUT_MS = 10_000;

/**
 * Build a factory that forks the compiled voice worker
 * (`dist/electron/main/voice/voice-worker.js`). Keep it lazy: the fork
 * only happens when the engine first needs native inference.
 */
export function createUtilityProcessTransportFactory(
  workerScriptPath: string = join(__dirname, "voice-worker.js"),
): TransportFactory {
  return () =>
    new Promise<VoiceWorkerTransport>((resolve, reject) => {
      const responseCallbacks = new Set<(response: WorkerResponse) => void>();
      const exitCallbacks = new Set<(info: { exitCode: number | null }) => void>();
      let running = true;
      let child: UtilityProcess;

      try {
        child = utilityProcess.fork(workerScriptPath, [], {
          serviceName: "jarvis-voice",
        });
      } catch (error) {
        reject(error instanceof Error ? error : new Error(String(error)));
        return;
      }

      let settled = false;
      const onceFailed = (error: Error): void => {
        if (settled) return;
        settled = true;
        reject(error);
      };
      const onceSpawned = (value: VoiceWorkerTransport): void => {
        if (settled) return;
        settled = true;
        resolve(value);
      };

      const spawnGuard = setTimeout(() => {
        if (settled) return;
        try {
          child.kill();
        } catch {
          // Best-effort cleanup on a dead spawn.
        }
        onceFailed(new Error("The voice worker did not spawn in time."));
      }, SPAWN_TIMEOUT_MS);

      child.once("spawn", () => {
        clearTimeout(spawnGuard);
        onceSpawned({
          send: (request: WorkerRequest) => {
            const validated = validateWorkerRequest(request);
            if (!validated.ok) {
              throw new Error(`Refused malformed worker request: ${validated.reason}`);
            }
            if (!running) throw new Error("The voice worker is not running.");
            // structured-cloneable payload (validated shapes + TypedArrays).
            child.postMessage(validated.value);
          },
          onResponse: (callback) => {
            responseCallbacks.add(callback);
          },
          onExit: (callback) => {
            exitCallbacks.add(callback);
          },
          kill: () => {
            running = false;
            try {
              child.kill();
            } catch {
              // Already dead — nothing to do.
            }
          },
          running: () => running && child.pid !== undefined,
        });
      });
      child.on("message", (raw: unknown) => {
        if (!running) return;
        for (const callback of responseCallbacks) callback(raw as WorkerResponse);
      });

      child.on("exit", (exitCode: number | null) => {
        running = false;
        clearTimeout(spawnGuard);
        if (!settled) {
          onceFailed(new Error(`The voice worker exited before it spawned (code ${exitCode ?? "null"}).`));
          return;
        }
        for (const callback of exitCallbacks) callback({ exitCode });
      });
    });
}
