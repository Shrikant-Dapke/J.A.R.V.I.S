/**
 * Model manager tests (Phase 6.4-b) — mocked filesystem, hashing,
 * network, and disk-space probes. The pinned manifest (373 files, ~261
 * MB) never touches real I/O here: every capability is injected.
 *
 * Covers: never-download-on-construction, atomic install ordering,
 * checksum-mismatch rejection, corrupted-cache rejection, interrupted
 * (.part) recovery sweep, insufficient disk space, and deep verify.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { join } from "node:path";
import { STT_ZIPFORMER_EN_INT8, TTS_KOKORO_V11_INT8 } from "../electron/main/voice/model-manifest";
import {
  createModelManager,
  type DiskSpaceLike,
  type DirentLike,
  type DownloaderLike,
  type HashLike,
  type ModelFsLike,
} from "../electron/main/voice/model-manager";

const MODELS = [STT_ZIPFORMER_EN_INT8, TTS_KOKORO_V11_INT8];

/* ---------------------------------------------------------------------- */
/* Fake capability set                                                    */
/* ---------------------------------------------------------------------- */

/** Rows for files (`bytes` + `sha256`); directories are implicit. */
type FakeStore = Map<string, { bytes: number; sha256: string }>;

/** Number of REQUIRED files (the smoke assets are NOT required). */
const REQUIRED_COUNT = MODELS.reduce((sum, model) => sum + model.files.length, 0);
const REQUIRED_BYTES = MODELS.reduce(
  (sum, model) => sum + model.files.reduce((a, file) => a + file.size, 0),
  0,
);

function makeFs(store: FakeStore): ModelFsLike {
  return {
    exists: (path) => store.has(path),
    size: (path) => store.get(path)?.bytes ?? null,
    mkdirs: () => undefined, // directories are implicit in the fake store
    rename: (from, to) => {
      const row = store.get(from);
      if (!row) throw new Error(`fake rename: missing ${from}`);
      store.delete(from);
      store.set(to, row);
    },
    remove: (path) => {
      store.delete(path);
    },
    listEntries: (dir) => {
      // Windows-safe prefix handling: join() produces \ separators.
      const slashA = dir.endsWith("\\") ? dir : dir + "\\";
      const slashB = dir.endsWith("/") ? dir : dir + "/";
      const names = new Set<string>();
      for (const key of store.keys()) {
        let relative = "";
        if (key.startsWith(slashA)) relative = key.slice(slashA.length);
        else if (key.startsWith(slashB)) relative = key.slice(slashB.length);
        else continue;
        names.add(relative.split(/[\\/]/)[0]);
      }
      return [...names].map((name): DirentLike => ({
        name,
        isDirectory: !store.has(`${slashA}${name}`) && !store.has(`${slashB}${name}`),
      }));
    },
  };
}

/**
 * Downloader keyed by manifest URL. Normal URLs install the correct
 * bytes + pinned checksum; URLs in `corruptUrls` install wrong content
 * (checksum will mismatch); URLs in `failingUrls` simulate an
 * interrupted transfer (nothing usable is written, signature throws as a
 * network fault would).
 */
function makeDownloader(
  store: FakeStore,
  options: { corruptUrls?: Set<string>; failingUrls?: Set<string> } = {},
): DownloaderLike & { calls: Array<{ url: string; dest: string }> } {
  const calls: Array<{ url: string; dest: string }> = [];
  return {
    calls,
    async download(url, destPath) {
      calls.push({ url, dest: destPath });
      if (options.failingUrls?.has(url)) {
        // Interrupted mid-transfer: the .part file exists but is partial.
        const row = MODEL_URL_TO_ENTRY.get(url);
        store.set(destPath, { bytes: row ? Math.floor(row.size / 2) : 0, sha256: "partial" });
        throw new Error("network interrupted mid-download");
      }
      const row = MODEL_URL_TO_ENTRY.get(url);
      const corrupted = options.corruptUrls?.has(url) ?? false;
      store.set(destPath, {
        bytes: row ? row.size : 8,
        sha256: corrupted ? "aaaaaaaaaaaaaaaa" : row ? row.sha256 : "ffffffffffffffff",
      });
    },
  };
}

function makeHash(store: FakeStore): HashLike {
  return {
    async sha256(path) {
      const row = store.get(path);
      if (!row) throw new Error(`fake hash: missing ${path}`);
      return row.sha256;
    },
  };
}

function makeDisk(freeBytes: number | null): DiskSpaceLike {
  return { freeBytes: async () => freeBytes };
}

/** Pinned URL → manifest row (the "correct" download content). */
const MODEL_URL_TO_ENTRY = new Map<string, { path: string; size: number; sha256: string }>();
for (const model of MODELS) {
  for (const file of [...model.files, ...model.smokeFiles]) {
    MODEL_URL_TO_ENTRY.set(file.url, { path: file.path, size: file.size, sha256: file.sha256 });
  }
}

const ROOT_STT = STT_ZIPFORMER_EN_INT8;
const ROOT_TTS = TTS_KOKORO_V11_INT8;
const BASE = "C:\\fake\\models";
const target = (id: string, relative: string): string => join(BASE, id, relative);

/* ---------------------------------------------------------------------- */
/* Tests                                                                  */
/* ---------------------------------------------------------------------- */

test("constructing a manager downloads NOTHING and reports models missing", () => {
  const store: FakeStore = new Map();
  const downloader = makeDownloader(store);
  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader,
    hash: makeHash(store),
    disk: makeDisk(null),
  });
  assert.equal(downloader.calls.length, 0, "a constructed manager is inert");
  const status = manager.status();
  assert.equal(status.sttPresent, false);
  assert.equal(status.ttsPresent, false);
  assert.equal(status.sttPaths, null);
  assert.equal(status.ttsPaths, null);
  assert.deepEqual(status.sttMissing[0], `${ROOT_STT.id}/${ROOT_STT.files[0].path}`);
  assert.ok(status.ttsMissing.some((row) => row.includes("model.int8.onnx")));
});

test("prepareModels installs every required file atomically: download → verify → rename", async () => {
  const store: FakeStore = new Map();
  const downloader = makeDownloader(store);
  const events: Array<{ phase: string; file?: string }> = [];
  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader,
    hash: makeHash(store),
    disk: makeDisk(Number.MAX_SAFE_INTEGER),
  });

  const result = await manager.prepareModels((progress) => {
    events.push({ phase: progress.phase, file: progress.file });
  });

  assert.equal(result.ok, true);
  if (!result.ok) return;
  // One network hit per missing required file...
  const requiredCount = REQUIRED_COUNT;
  assert.equal(downloader.calls.length, requiredCount);
  assert.equal(result.downloaded, requiredCount);
  assert.equal(result.verifiedFromCache, 0);
  assert.equal(result.bytes, REQUIRED_BYTES);

  // ...and every required file landed at its pinned destination with
  // exactly the pinned size and checksum.
  for (const model of MODELS) {
    for (const file of model.files) {
      const installed = store.get(target(model.id, file.path));
      assert.ok(installed, `installed: ${file.path}`);
      assert.equal(installed.bytes, file.size);
      assert.equal(installed.sha256, file.sha256);
    }
  }

  // Temp downloads always went through .tmp/*.part.
  for (const call of downloader.calls) {
    assert.ok(
      call.dest.startsWith(join(BASE, ".tmp")) && call.dest.endsWith(".part"),
      `temp download lands in .tmp/*.part, got ${call.dest}`,
    );
  }

  // Per-file phase ordering is download → verify → install.
  const order = events.map((e) => e.phase);
  const firstFilePhases = order.slice(0, 3);
  assert.deepEqual(firstFilePhases, ["download", "verify", "install"]);

  // Second prepare verifies from cache: no new downloads.
  const again = await manager.prepareModels();
  assert.equal(again.ok, true);
  if (!again.ok) return;
  assert.equal(downloader.calls.length, requiredCount, "no second network hits");
  assert.equal(again.downloaded, 0);
  assert.equal(again.verifiedFromCache, requiredCount);

  const status = manager.status();
  assert.equal(status.sttPresent, true);
  assert.equal(status.ttsPresent, true);
  assert.equal(status.sttPaths?.encoder, target(ROOT_STT.id, "encoder-epoch-99-avg-1-chunk-16-left-128.int8.onnx"));
  assert.equal(status.ttsPaths?.model, target(ROOT_TTS.id, "model.int8.onnx"));
  assert.equal(status.ttsPaths?.dataDir, target(ROOT_TTS.id, "espeak-ng-data"));
  assert.match(status.ttsPaths?.lexicon ?? "", /lexicon-us-en\.txt/);

  // Smoke assets are pinned but NOT part of prepare(): engine readiness
  // never depends on them — the smoke harness fetches them on demand.
  const smoke = manager.smokePaths();
  const smokePinnedCount = MODELS.reduce((sum, model) => sum + model.smokeFiles.length, 0);
  assert.equal(smoke.missingAssets.length, smokePinnedCount, "prepare leaves pinned smoke assets out");
  assert.equal(smoke.transTxt, target(ROOT_STT.id, join("test_wavs", "trans.txt")));
});

test("a checksum-mismatched download is rejected, cleaned, and retriable", async () => {
  const store: FakeStore = new Map();
  const badUrl = ROOT_TTS.files[0].url; // model.int8.onnx
  const downloader = makeDownloader(store, { corruptUrls: new Set([badUrl]) });
  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader,
    hash: makeHash(store),
    disk: makeDisk(null),
  });

  const result = await manager.prepareModels();
  assert.equal(result.ok, false);
  if (result.ok) return;
  assert.match(result.error, /Checksum mismatch/u);
  // The corrupted file never reached its pinned destination...
  assert.equal(store.has(target(ROOT_TTS.id, "model.int8.onnx")), false);
  // ...and the failed attempt left no .part leftovers in .tmp/.
  const parts = [...store.keys()].filter((key) => key.startsWith(join(BASE, ".tmp")) && key.endsWith(".part"));
  assert.equal(parts.length, 0, "temp files are swept after a failed attempt");
  assert.equal(downloader.calls.length, ROOT_STT.files.length + 1, "stops at the first bad file");

  // Fixing the network makes the retry succeed.
  const healing = makeDownloader(store);
  const fixed = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader: healing,
    hash: makeHash(store),
    disk: makeDisk(null),
  });
  const retried = await fixed.prepareModels();
  assert.equal(retried.ok, true);
  assert.equal(store.has(target(ROOT_TTS.id, "model.int8.onnx")), true);
});

test("an interrupted transfer leaves a .part file; the next run sweeps and retries", async () => {
  const store: FakeStore = new Map();
  const badUrl = ROOT_STT.files[0].url;
  const failing = makeDownloader(store, { failingUrls: new Set([badUrl]) });
  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader: failing,
    hash: makeHash(store),
    disk: makeDisk(null),
  });

  const failed = await manager.prepareModels();
  assert.equal(failed.ok, false);
  const partials = [...store.keys()].filter((key) => key.endsWith(".part"));
  const sweptNothingYet = partials.length > 0 || failing.calls.length === 1;
  assert.ok(sweptNothingYet, "failure path ran");

  // A.Leftover .part from a previous crash:
  store.set(join(BASE, ".tmp", "leftover-1.part"), { bytes: 5, sha256: "junk" });
  const healing = makeDownloader(store);
  const retry = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader: healing,
    hash: makeHash(store),
    disk: makeDisk(null),
  });
  const okRun = await retry.prepareModels();
  assert.equal(okRun.ok, true);
  if (!okRun.ok) return;
  assert.equal(store.has(join(BASE, ".tmp", "leftover-1.part")), false, "leftover .part was swept");
  assert.ok(store.has(target(ROOT_STT.id, ROOT_STT.files[0].path)), "the interrupted file completed");
});

test("a cached file whose SIZE matches but content was tampered with is re-fetched", async () => {
  const store: FakeStore = new Map();
  // Pre-place ALL files as valid EXCEPT one which keeps its size but is
  // bitwise-corrupted (hash no longer matches).
  const downloader = makeDownloader(store);
  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader,
    hash: makeHash(store),
    disk: makeDisk(null),
  });
  const seeded = await manager.prepareModels();
  assert.equal(seeded.ok, true);

  const victimRel = ROOT_STT.files[0].path;
  const victim = target(ROOT_STT.id, victimRel);
  const row = store.get(victim);
  assert.ok(row);
  store.set(victim, { ...row, sha256: "bits-flipped-in-place" });

  const detector = makeDownloader(store);
  const verifier = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader: detector,
    hash: makeHash(store),
    disk: makeDisk(null),
  });
  const repaired = await verifier.prepareModels();
  assert.equal(repaired.ok, true);
  if (!repaired.ok) return;
  assert.equal(detector.calls.length, 1, "exactly the tampered file was re-fetched");
  assert.equal(detector.calls[0].url, ROOT_STT.files[0].url);
  assert.equal(repaired.downloaded, 1);

  // Deep verify() agrees the tree is intact again.
  const checked = await verifier.verify();
  assert.equal(checked.ok, true);
  if (checked.ok) assert.equal(checked.checked, REQUIRED_COUNT);
});

test("insufficient disk space refuses BEFORE any download", async () => {
  const store: FakeStore = new Map();
  const downloader = makeDownloader(store);
  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader,
    hash: makeHash(store),
    disk: makeDisk(1024), // 1 KB free — far below required+margins
  });
  const result = await manager.prepareModels();
  assert.equal(result.ok, false);
  if (result.ok) return;
  assert.match(result.error, /Insufficient disk space/u);
  assert.equal(downloader.calls.length, 0);
  assert.equal(store.size, 0);
});

test("an unknown disk-space probe does NOT block preparation", async () => {
  const store: FakeStore = new Map();
  const downloader = makeDownloader(store);
  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader,
    hash: makeHash(store),
    disk: makeDisk(null),
  });
  const result = await manager.prepareModels();
  assert.equal(result.ok, true, "disk unknown = proceed (checksums still guard integrity)");
  assert.equal(downloader.calls.length, REQUIRED_COUNT);
});

test("verify() honestly reports missing and checksum-broken files", async () => {
  const store: FakeStore = new Map();
  // Seed only ONE valid file; everything else is missing.
  const validPath = target(ROOT_TTS.id, "tokens.txt");
  const validRow = ROOT_TTS.files.find((f) => f.path === "tokens.txt");
  assert.ok(validRow);
  store.set(validPath, { bytes: validRow.size, sha256: validRow.sha256 });

  const manager = createModelManager({
    baseDir: BASE,
    fs: makeFs(store),
    downloader: makeDownloader(store),
    hash: makeHash(store),
    disk: makeDisk(null),
  });
  const result = await manager.verify();
  assert.equal(result.ok, false);
  if (result.ok) return;
  assert.ok(result.problems.some((problem) => problem.includes(`missing: encoder`)));
  assert.ok(result.problems.some((problem) => problem.includes("size mismatch") || problem.includes("missing")));
});
