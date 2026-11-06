/*
 * JARVIS microphone tap.
 *
 * The worklet performs only the real-time graph-side copy and forwards
 * bounded device/context-rate channel blocks. Mixdown, resampling, PCM16
 * encoding, and protocol framing stay in the shared renderer pipeline so
 * they remain deterministic and unit-testable.
 */
class JarvisMicTap extends AudioWorkletProcessor {
  process(inputs, outputs) {
    const input = inputs[0];
    const output = outputs[0];

    // Keep the graph pulled without monitoring the microphone.
    if (output) {
      for (const channel of output) channel.fill(0);
    }

    if (!input || input.length === 0 || !input[0] || input[0].length === 0) {
      return true;
    }

    // A normal input is one or two channels. Cap the forwarded channel
    // count so a malformed device configuration cannot create an unbounded
    // message; the renderer validates equal lengths before DSP.
    const channels = input.slice(0, 8).map((channel) => {
      const copy = new Float32Array(channel.length);
      copy.set(channel);
      return copy.buffer;
    });
    this.port.postMessage({ channels }, channels);
    return true;
  }
}

registerProcessor("jarvis-mic-tap", JarvisMicTap);
