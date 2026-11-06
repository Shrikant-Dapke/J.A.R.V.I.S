/**
 * Per-capture sequence gate for renderer microphone chunks.
 *
 * IPC validation bounds the sequence number; this gate adds the session
 * ordering invariant before PCM reaches the native recognizer. Duplicate
 * and replayed chunks are harmless idempotent drops. A strictly increasing
 * sequence is accepted, including a gap: the transport cannot recover a
 * missing audio chunk, but it must never feed a later chunk twice or let a
 * replay corrupt recognizer ordering.
 */

export type MicSequenceDecision =
  | { accepted: true; sequence: number }
  | { accepted: false; sequence: number; reason: "duplicate-or-replay" };

export type MicSequenceGate = {
  accept: (sequence: number) => MicSequenceDecision;
  reset: () => void;
  last: () => number | null;
};

export function createMicSequenceGate(): MicSequenceGate {
  let lastSequence: number | null = null;

  return {
    accept: (sequence) => {
      if (lastSequence !== null && sequence <= lastSequence) {
        return { accepted: false, sequence, reason: "duplicate-or-replay" };
      }
      lastSequence = sequence;
      return { accepted: true, sequence };
    },
    reset: () => {
      lastSequence = null;
    },
    last: () => lastSequence,
  };
}
