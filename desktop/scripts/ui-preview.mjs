// Bundle the UI's browser preview (ui/dev) into dist/ui-preview/. Serve that
// folder over http to look at it — IndexedDB, which keeps the device
// identity, is not available to a page opened from file:// in a browser.
import { build } from "esbuild";
import { cpSync, mkdirSync, rmSync } from "node:fs";

rmSync("dist/ui-preview", { recursive: true, force: true });
mkdirSync("dist/ui-preview", { recursive: true });

await build({
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "chrome120",
  entryPoints: ["ui/dev/main.tsx"],
  outfile: "dist/ui-preview/preview.js",
  jsx: "automatic",
  define: { "process.env.NODE_ENV": '"production"' },
  loader: { ".woff2": "file" },
  assetNames: "assets/[name]-[hash]",
  sourcemap: true,
});
cpSync("ui/dev/index.html", "dist/ui-preview/index.html");
console.log("built dist/ui-preview/ — serve it, e.g. npx serve dist/ui-preview");
