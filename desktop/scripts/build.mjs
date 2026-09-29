// Minimal build: bundle main/preload/renderer with esbuild into dist/.
// The preload must be emitted as .mjs — Electron parses .js preloads as
// CommonJS regardless of package.json "type": "module".
import { build } from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";

const shared = {
  bundle: true,
  format: "esm",
  platform: "node",
  target: "node22",
  external: ["electron"],
  sourcemap: false,
};

rmSync("dist", { recursive: true, force: true });
mkdirSync("dist/main", { recursive: true });
mkdirSync("dist/renderer", { recursive: true });

await build({
  ...shared,
  // `ws` is CommonJS internally and still requires Node built-ins such as
  // `events`. The Electron main bundle is ESM, so give esbuild's CommonJS
  // compatibility shim a real Node require instead of the browser-style
  // dynamic-require fallback that crashes at app startup.
  banner: {
    js: 'import { createRequire as __gnsisCreateRequire } from "node:module"; const require = __gnsisCreateRequire(import.meta.url);',
  },
  entryPoints: ["src/main/main.ts"],
  outfile: "dist/main/main.js",
});
await build({ ...shared, entryPoints: ["src/preload.ts"], outfile: "dist/main/preload.mjs" });
// The renderer is the React UI (@gnsis/ui) over the Electron host. Its
// stylesheet and the fonts it ships come out beside the script, so the
// page's strict content policy ('self' only) is enough to load them.
await build({
  ...shared,
  platform: "browser",
  target: "chrome120",
  entryPoints: ["src/renderer/app.tsx"],
  outfile: "dist/renderer/app.js",
  jsx: "automatic",
  define: { "process.env.NODE_ENV": '"production"' },
  loader: { ".woff2": "file" },
  assetNames: "assets/[name]-[hash]",
  minify: true,
});
await build({
  ...shared,
  platform: "browser",
  target: "chrome120",
  entryPoints: ["src/renderer/pcm-worklet.ts"],
  outfile: "dist/renderer/pcm-worklet.js",
});
cpSync("src/renderer/index.html", "dist/renderer/index.html");
console.log("built dist/");
