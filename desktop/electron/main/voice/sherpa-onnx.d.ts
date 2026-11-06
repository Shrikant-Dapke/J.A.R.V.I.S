/**
 * Minimal type surface for `sherpa-onnx-node` (Apache-2.0 native addon).
 * The package ships no TypeScript declarations; only the tiny subset the
 * voice worker uses is declared here, and everything else stays `unknown`
 * so accidental usage fails the build rather than slipping through `any`.
 */

declare module "sherpa-onnx-node" {
  /** A single streaming recognition result. */
  export type OnlineResult = { text: string; tokens: string[] };

  /**
   * The stream owns audio input: `acceptWaveform` lives on the stream in
   * sherpa-onnx-node ≥ 2.0 (NOT on the recognizer).
   */
  export type OnlineStream = {
    acceptWaveform(wave: { samples: Float32Array; sampleRate: number }): void;
    inputFinished(): void;
  };

  export class OnlineRecognizer {
    constructor(config: unknown);
    createStream(): OnlineStream;
    isReady(stream: OnlineStream): boolean;
    decode(stream: OnlineStream): void;
    isEndpoint(stream: OnlineStream): boolean;
    getResult(stream: OnlineStream): OnlineResult;
    reset(stream: OnlineStream): void;
  }

  export type GeneratedAudio = { samples: Float32Array; sampleRate: number };

  export class OfflineTts {
    static createAsync(config: unknown): Promise<OfflineTts>;
    readonly sampleRate: number;
    readonly numSpeakers: number;
    readonly lang: string;
    generateAsync(request: {
      text: string;
      sid: number;
      speed: number;
      onProgress?: (info: { samples: Float32Array; progress: number }) => number | boolean | void;
    }): Promise<GeneratedAudio>;
  }

  export function readWave(filename: string): { samples: Float32Array; sampleRate: number };
  export const version: string;
  export const onnxruntimeVersion: string;
}
