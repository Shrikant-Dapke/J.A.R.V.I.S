/**
 * Bundle the sandboxed preload into one self-contained CommonJS file.
 *
 * Electron sandbox preloads may require Electron's built-in module, but they
 * cannot resolve arbitrary relative application modules at runtime. The
 * TypeScript main build still typechecks the source; esbuild only removes the
 * sandbox-incompatible relative imports from the emitted preload artifact.
 */

import { build } from "esbuild";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));

await build({
  absWorkingDir: root,
  entryPoints: ["electron/preload/preload.ts"],
  outfile: "dist/electron/preload/preload.js",
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "es2022",
  external: ["electron"],
  sourcemap: false,
  logLevel: "info",
});
