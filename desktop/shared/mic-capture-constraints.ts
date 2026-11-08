/**
 * Microphone capture constraints (browser-side device tap config).
 *
 * These flags configure the browser's built-in audio processing at
 * getUserMedia() time — upstream of everything in voice-audio.ts, which
 * only shapes the stream AFTER capture. They are pinned as a single
 * source of truth so the capture profile stays reviewable and testable
 * under the Node-only test architecture (the getUserMedia call itself
 * is renderer-only).
 */

/**
 * Experimental (controlled AGC trial): autoGainControl raised from false
 * to true to restore a natural input level on raw device taps. Echo
 * cancellation and noise suppression deliberately remain OFF so only ONE
 * processing variable changes at a time — any STT accuracy delta from
 * this experiment is attributable to AGC alone.
 */
export const MIC_AUDIO_CONSTRAINTS = Object.freeze({
  echoCancellation: false,
  noiseSuppression: false,
  autoGainControl: true,
});

/** Non-audio capture constraints for the microphone stream. */
export const MIC_CAPTURE_VIDEO = false as const;
