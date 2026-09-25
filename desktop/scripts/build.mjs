// Minimal build: bundle main/preload/renderer with esbuild into dist/.
import { build } from "esbuild";
import { cpSync, mkdirSync } from "node:fs";

const shared = {
  bundle: true,
  format: "esm",
  platform: "node",
  target: "node22",
  external: ["electron"],
  sourcemap: false,
};

mkdirSync("dist/main", { recursive: true });
mkdirSync("dist/renderer", { recursive: true });

await build({ ...shared, entryPoints: ["src/main/main.ts"], outfile: "dist/main/main.js" });
await build({ ...shared, entryPoints: ["src/preload.ts"], outfile: "dist/main/preload.js" });
await build({
  ...shared,
  platform: "browser",
  target: "chrome120",
  entryPoints: ["src/renderer/app.ts"],
  outfile: "dist/renderer/app.js",
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
