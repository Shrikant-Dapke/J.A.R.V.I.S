/**
 * Voice model manager (Phase 6.4-b).
 *
 * Downloads PINNED model files (see `model-manifest.ts`) into the app's
 * profile directory (`app.getPath("userData")/models`) and verifies the
 * SHA-256 of EVERY file BEFORE it is accepted:
 *
 *   1. preflight — how much disk space is required; refuse politely if
 *      the free-space probe is available and too low,
 *   2. download — to `<baseDir>/.tmp/<random>.part` (a leftover .part
 *      from an interrupted run is swept on the next attempt; a partial
 *      file is NEVER accepted),
 *   3. verify — hash the .part and compare against the pinned checksum,
 *   4. atomic install — `rename` the verified temp file over the target
 *      path (same volume ⇒ atomic replace),
 *   5. presence/status — a model is "present" iff all required files
 *      exist with the pinned sizes; deep re-verification is available
 *      on demand.
 *
 * Invariants:
 *   - NOTHING is downloaded on startup, ever — only on an explicit
 *     `prepareModels` request (Phase 6.4-c wires that to a user action),
 *   - downloaded files are data only: this module performs NO process
 *     execution and never invokes anything it downloaded,
 *   - all real filesystem/network/hashing capabilities are injected, so
 *     every rule here is pure-Node testable.
 */

import { createHash } from "node:crypto";
import { createReadStream, createWriteStream, existsSync, mkdirSync, readdirSync, renameSync, statSync, unlinkSync } from "node:fs";
import { dirname, join } from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import {
  STT_ZIPFORMER_EN_INT8,
  TTS_KOKORO_V11_INT8,
  type PinnedModel,
  type PinnedModelFile,
} from "./model-manifest";

/* ------------------------------------------------------------------ */
/* Injectable capabilities                                             */
/* ------------------------------------------------------------------ */

export type DirentLike = { name: string; isDirectory: boolean };

export type ModelFsLike = {
  exists: (path: string) => boolean;
  size: (path: string) => number | null;
  mkdirs: (dir: string) => void;
  rename: (from: string, to: string) => void;
  remove: (path: string) => void;
  /** Recursive name+type listing (empty on any error — probe contract). */
  listEntries: (dir: string) => DirentLike[];
};

export type DownloaderLike = {
  /**
   * Stream `url` into `destPath`. Throws on HTTP errors, truncation, or
   * any interruption — callers treat every throw as "retry from zero".
   */
  download: (url: string, destPath: string) => Promise<void>;
};

export type HashLike = {
  sha256: (path: string) => Promise<string>;
};

export type DiskSpaceLike = {
  /** Free bytes on the volume holding `path`, or null when unknown. */
  freeBytes: (path: string) => Promise<number | null>;
};

export type ModelManagerOptions = {
  baseDir: string;
  fs?: ModelFsLike;
  downloader?: DownloaderLike;
  hash?: HashLike;
  disk?: DiskSpaceLike;
};

/** Extra byte margin on top of the exact download sizes. */
const DISK_SPACE_MARGIN_BYTES = 64 * 1024 * 1024;
/** Failure message ceiling (mirrors VOICE_FAILURE_MESSAGE_MAX_LENGTH). */
const FAILURE_TEXT_MAX = 512;

const clampMessage = (message: string): string =>
  message.length <= FAILURE_TEXT_MAX ? message : message.slice(0, FAILURE_TEXT_MAX);

/* ------------------------------------------------------------------ */
/* Default (real) implementations — plain Node, no Electron imports    */
/* ------------------------------------------------------------------ */

export function createNodeModelFs(): ModelFsLike {
  const size = (path: string): number | null => {
    try {
      const stat = statSync(path);
      return stat.isFile() ? stat.size : null;
    } catch {
      return null;
    }
  };
  return {
    exists: (path) => existsSync(path),
    size,
    mkdirs: (dir) => mkdirSync(dir, { recursive: true }),
    rename: (from, to) => renameSync(from, to),
    remove: (path) => {
      try {
        unlinkSync(path);
      } catch {
        // Removing a temp file is best-effort only.
      }
    },
    listEntries: (dir) => {
      try {
        return readdirSync(dir, { withFileTypes: true }).map((entry) => ({
          name: entry.name,
          isDirectory: entry.isDirectory(),
        }));
      } catch {
        return [];
      }
    },
  };
}

export function createNodeSha256(): HashLike {
  return {
    sha256: (path) =>
      new Promise((resolve, reject) => {
        const hash = createHash("sha256");
        const stream = createReadStream(path);
        stream.on("data", (chunk) => hash.update(chunk));
        stream.on("error", reject);
        stream.on("end", () => resolve(hash.digest("hex")));
      }),
  };
}

export function createNodeDownloader(): DownloaderLike {
  return {
    download: async (url, destPath) => {
      const response = await fetch(url, { redirect: "follow" });
      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status} while downloading a model file.`);
      }
      const target = createWriteStream(destPath);
      await pipeline(Readable.fromWeb(response.body as never), target);
      // Truncation guard: a shorter body must not be accepted.
      const written = statSync(destPath).size;
      const expected = Number(response.headers.get("content-length") ?? "0");
      if (expected > 0 && written !== expected) {
        throw new Error(`Truncated download (${written} of ${expected} bytes) — file rejected.`);
      }
    },
  };
}

export function createNodeDiskSpace(): DiskSpaceLike {
  return {
    freeBytes: async (path) => {
      // fs.statfs exists on Windows-capable Node builds in some forms;
      // if anything fails the probe is honestly "unknown".
      try {
        const fsModule = await import("node:fs");
        if (typeof fsModule.statfs !== "function") return null;
        return await new Promise<number | null>((resolve) => {
          fsModule.statfs(path, (error, stats) =>
            resolve(error ? null : stats.bsize * stats.bavail),
          );
        });
      } catch {
        return null;
      }
    },
  };
}

/* ------------------------------------------------------------------ */
/* The manager                                                         */
/* ------------------------------------------------------------------ */

export type SttPaths = { encoder: string; decoder: string; joiner: string; tokens: string };
export type TtsPaths = { model: string; voices: string; tokens: string; dataDir: string; lexicon: string };

export type ModelStatus = {
  sttPresent: boolean;
  ttsPresent: boolean;
  sttMissing: string[];
  ttsMissing: string[];
  sttPaths: SttPaths | null;
  ttsPaths: TtsPaths | null;
};

export type PrepareProgress = {
  file: string;
  doneFiles: number;
  totalFiles: number;
  phase: "download" | "verify" | "install";
};

export type PrepareResult =
  | { ok: true; downloaded: number; verifiedFromCache: number; bytes: number }
  | { ok: false; error: string };

export type VerifyResult =
  | { ok: true; checked: number }
  | { ok: false; problems: string[] };

export type VoiceModelManager = {
  readonly baseDir: string;
  /** Inert smoke-test assets, also pinned; null when absent. */
  smokePaths: () => { wavsDir: string | null; transTxt: string | null; missingAssets: string[] };
  status: () => ModelStatus;
  prepareModels: (onProgress?: (progress: PrepareProgress) => void) => Promise<PrepareResult>;
  /** Deep verification of every required file against the pinned hashes. */
  verify: (onProgress?: (progress: PrepareProgress) => void) => Promise<VerifyResult>;
};

const STT_MODEL = STT_ZIPFORMER_EN_INT8;
const TTS_MODEL = TTS_KOKORO_V11_INT8;

/**
 * Build the manager. Nothing touches the network at construction; the
 * only sink is `prepareModels`, which must be deliberately invoked.
 */
export function createModelManager(options: ModelManagerOptions): VoiceModelManager {
  const fs = options.fs ?? createNodeModelFs();
  const downloader = options.downloader ?? createNodeDownloader();
  const hash = options.hash ?? createNodeSha256();
  const disk = options.disk ?? createNodeDiskSpace();
  const baseDir = options.baseDir;

  const tmpDir = join(baseDir, ".tmp");
  const modelDir = (model: PinnedModel): string => join(baseDir, model.id);
  const absFile = (model: PinnedModel, entry: PinnedModelFile): string =>
    join(modelDir(model), entry.path);

  const sweepTempDir = (): void => {
    for (const entry of fs.listEntries(tmpDir)) {
      if (!entry.isDirectory && entry.name.endsWith(".part")) {
        fs.remove(join(tmpDir, entry.name));
      }
    }
  };

  const requiredRows = (): Array<{ model: PinnedModel; entry: PinnedModelFile; absolute: string }> => [
    ...STT_MODEL.files.map((entry) => ({ model: STT_MODEL, entry, absolute: absFile(STT_MODEL, entry) })),
    ...TTS_MODEL.files.map((entry) => ({ model: TTS_MODEL, entry, absolute: absFile(TTS_MODEL, entry) })),
  ];

  const isPresent = (path: string, entry: PinnedModelFile): boolean =>
    fs.exists(path) && fs.size(path) === entry.size;

  const smokePaths = (): { wavsDir: string | null; transTxt: string | null; missingAssets: string[] } => {
    const missingAssets: string[] = [];
    const wavEntries = STT_MODEL.smokeFiles.filter((e) => e.path.startsWith("test_wavs/") && e.path.endsWith(".wav"));
    const transEntry = STT_MODEL.smokeFiles.find((e) => e.path === "test_wavs/trans.txt");
    for (const entry of [...wavEntries, ...(transEntry ? [transEntry] : [])]) {
      if (!isPresent(absFile(STT_MODEL, entry), entry)) missingAssets.push(entry.path);
    }
    return {
      wavsDir: wavEntries.length > 0 ? join(modelDir(STT_MODEL), "test_wavs") : null,
      transTxt: transEntry ? absFile(STT_MODEL, transEntry) : null,
      missingAssets,
    };
  };

  const status = (): ModelStatus => {
    const sttMissing: string[] = [];
    const ttsMissing: string[] = [];
    for (const { model, entry } of requiredRows()) {
      if (!isPresent(absFile(model, entry), entry)) {
        (model.id === TTS_MODEL.id ? ttsMissing : sttMissing).push(`${model.id}/${entry.path}`);
      }
    }
    const dir = (model: PinnedModel) => modelDir(model);
    return {
      sttPresent: sttMissing.length === 0,
      ttsPresent: ttsMissing.length === 0,
      sttMissing,
      ttsMissing,
      sttPaths: sttMissing.length === 0
        ? {
            // Exact-keyed by the protocol validators — insertion IS key
            // order in-process, so these literals are built pre-sorted.
            // NOTE: filenames must match the STT model's manifest files
            // (stt-zipformer-en-2023-06-21-int8 → no chunk-size suffix).
            decoder: join(dir(STT_MODEL), "decoder-epoch-99-avg-1.int8.onnx"),
            encoder: join(dir(STT_MODEL), "encoder-epoch-99-avg-1.int8.onnx"),
            joiner: join(dir(STT_MODEL), "joiner-epoch-99-avg-1.int8.onnx"),
            tokens: join(dir(STT_MODEL), "tokens.txt"),
          }
        : null,
      ttsPaths: ttsMissing.length === 0
        ? {
            dataDir: join(dir(TTS_MODEL), "espeak-ng-data"),
            lexicon: [
              join(dir(TTS_MODEL), "lexicon-us-en.txt"),
              join(dir(TTS_MODEL), "lexicon-gb-en.txt"),
              join(dir(TTS_MODEL), "lexicon-zh.txt"),
            ].join(","),
            model: join(dir(TTS_MODEL), "model.int8.onnx"),
            tokens: join(dir(TTS_MODEL), "tokens.txt"),
            voices: join(dir(TTS_MODEL), "voices.bin"),
          }
        : null,
    };
  };

  const prepareModels = async (
    onProgress?: (progress: PrepareProgress) => void,
  ): Promise<PrepareResult> => {
    sweepTempDir();
    const rows = requiredRows();
    const missing = rows.filter(({ entry, absolute }) => !isPresent(absolute, entry));
    if (missing.length > 0) {
      const requiredBytes = missing.reduce((sum, row) => sum + row.entry.size, 0);
      const free = await disk.freeBytes(baseDir).catch(() => null);
      if (free !== null && free < requiredBytes + DISK_SPACE_MARGIN_BYTES) {
        return {
          ok: false,
          error: clampMessage(
            `Insufficient disk space: need ${(requiredBytes / 1048576).toFixed(1)} MB including margin; only ${free === null ? "unknown" : (free / 1048576).toFixed(1)} MB free.`,
          ),
        };
      }
    }

    let downloaded = 0;
    let verifiedFromCache = 0;
    let bytes = 0;
    const totalFiles = rows.length;
    let doneIndex = 0;

    for (const { entry, absolute } of rows) {
      doneIndex += 1;
      const progress = (phase: PrepareProgress["phase"]): void =>
        onProgress?.({ file: entry.path, doneFiles: doneIndex, totalFiles, phase });

      if (isPresent(absolute, entry)) {
        // Present files are STILL hash-verified: a corrupted file that
        // kept its size is rejected and re-fetched.
        progress("verify");
        const actual = await hash.sha256(absolute);
        if (actual === entry.sha256) {
          verifiedFromCache += 1;
          continue;
        }
        fs.remove(absolute);
      }

      fs.mkdirs(dirname(absolute));
      fs.mkdirs(tmpDir);
      const tempPath = join(
        tmpDir,
        `${entry.sha256.slice(0, 12)}-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}.part`,
      );
      try {
        progress("download");
        await downloader.download(entry.url, tempPath);
        progress("verify");
        const actual = await hash.sha256(tempPath);
        if (actual !== entry.sha256) {
          throw new Error(
            `Checksum mismatch for ${entry.path}: pinned ${entry.sha256.slice(0, 12)}…, received ${actual.slice(0, 12)}… — download rejected.`,
          );
        }
        progress("install");
        fs.rename(tempPath, absolute);
      } catch (error) {
        fs.remove(tempPath);
        return {
          ok: false,
          error: clampMessage(error instanceof Error ? error.message : String(error)),
        };
      }
      downloaded += 1;
      bytes += entry.size;
    }

    return { ok: true, downloaded, verifiedFromCache, bytes };
  };

  const verify = async (
    onProgress?: (progress: PrepareProgress) => void,
  ): Promise<VerifyResult> => {
    const problems: string[] = [];
    const rows = requiredRows();
    let checked = 0;
    let index = 0;
    for (const { entry, absolute } of rows) {
      index += 1;
      onProgress?.({ file: entry.path, doneFiles: index, totalFiles: rows.length, phase: "verify" });
      if (!fs.exists(absolute)) {
        problems.push(`missing: ${entry.path}`);
        continue;
      }
      if (fs.size(absolute) !== entry.size) problems.push(`size mismatch: ${entry.path}`);
      checked += 1;
      const actual = await hash.sha256(absolute);
      if (actual !== entry.sha256) problems.push(`checksum mismatch: ${entry.path}`);
    }
    return problems.length === 0 ? { ok: true, checked } : { ok: false, problems };
  };

  return { baseDir, smokePaths, status, prepareModels, verify };
}
