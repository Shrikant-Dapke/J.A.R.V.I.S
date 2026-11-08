/**
 * Serializes user-driven voice lifecycle commands without allowing one
 * rejected command to poison the queue. Audio chunks use their own bounded
 * feed chain; this queue is only for arm/begin/end/cancel/tts commands whose
 * state transitions must not race one another.
 */

export type VoiceCommandQueue = {
  run: <T>(command: () => Promise<T>) => Promise<T>;
  idle: () => Promise<void>;
};

export function createVoiceCommandQueue(): VoiceCommandQueue {
  let tail: Promise<void> = Promise.resolve();

  return {
    run: <T>(command: () => Promise<T>): Promise<T> => {
      const result = tail.then(command, command);
      tail = result.then(
        () => undefined,
        () => undefined,
      );
      return result;
    },
    idle: () => tail,
  };
}
