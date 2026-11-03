// Renders every clip to out/<id>.mp4 (H.264, for the landing page) and out/<id>.webm.
// Set CHROME_PATH to use a specific browser; otherwise Remotion downloads its own.
import { execFileSync } from "node:child_process";

const clips = ["say-it", "asks-first", "interrupt"];
const only = process.argv.slice(2);
const browser = process.env.CHROME_PATH ? ["--browser-executable", process.env.CHROME_PATH] : [];

for (const id of only.length ? only : clips) {
  execFileSync(
    "npx",
    ["remotion", "render", "src/index.ts", id, `out/${id}.mp4`, "--codec", "h264", "--crf", "20", ...browser],
    { stdio: "inherit" },
  );
}
