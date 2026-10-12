import { execFileSync } from "node:child_process";
import { chmodSync, mkdirSync, rmSync } from "node:fs";
import path from "node:path";

if (process.platform !== "darwin") {
  console.log("tap listener: macOS build skipped");
  process.exit(0);
}

const source = path.resolve("native/tap-listener/main.swift");
const outputDir = path.resolve("native/bin");
const arm64 = path.join(outputDir, "gnsis-tap-listener-arm64");
const x64 = path.join(outputDir, "gnsis-tap-listener-x64");
const universal = path.join(outputDir, "gnsis-tap-listener");

rmSync(outputDir, { recursive: true, force: true });
mkdirSync(outputDir, { recursive: true });

const compile = (arch, target) => {
  execFileSync("xcrun", [
    "swiftc",
    "-O",
    "-target",
    `${arch}-apple-macos14`,
    "-framework",
    "IOKit",
    source,
    "-o",
    target,
  ], { stdio: "inherit" });
};

compile("arm64", arm64);
compile("x86_64", x64);
execFileSync("lipo", ["-create", arm64, x64, "-output", universal], {
  stdio: "inherit",
});
execFileSync("codesign", [
  "--force",
  "--sign",
  "-",
  "--identifier",
  "com.gnsis.desktop.tap-listener",
  universal,
], { stdio: "inherit" });
chmodSync(universal, 0o755);
execFileSync(universal, ["--self-test"], { stdio: "inherit" });
rmSync(arm64);
rmSync(x64);
console.log(`tap listener: built ${universal}`);
