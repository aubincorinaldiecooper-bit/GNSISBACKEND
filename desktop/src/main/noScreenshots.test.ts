/**
 * GNSIS takes no screenshots (desktop/AGENTS.md). It sees the screen only
 * through the live share the person starts, and saves no pictures. This
 * fails the build if screenshot code appears: a page capture, the macOS
 * screenshot tool, a screen listing that makes thumbnails, or a screenshot
 * action offered to the model.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    if (statSync(full).isDirectory()) out.push(...sources(full));
    else if (/\.(ts|tsx|mts|js|mjs)$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(full);
  }
  return out;
}

const code = [...sources(path.join(desktop, "src")), ...sources(path.join(desktop, "ui/src"))].map((file) => ({
  file: path.relative(desktop, file),
  text: readFileSync(file, "utf8"),
}));

/** The argument text of every call to `name(`. */
function calls(text: string, name: string): string[] {
  const out: string[] = [];
  let at = text.indexOf(`${name}(`);
  while (at !== -1) {
    let depth = 0;
    let i = at + name.length;
    for (; i < text.length; i += 1) {
      if (text[i] === "(") depth += 1;
      else if (text[i] === ")" && --depth === 0) break;
    }
    out.push(text.slice(at + name.length + 1, i));
    at = text.indexOf(`${name}(`, i);
  }
  return out;
}

test("the code this checks is really there", () => {
  assert.ok(code.some(({ file }) => file === path.join("src", "main", "main.ts")));
});

test("no page captures and no macOS screenshot tool", () => {
  for (const { file, text } of code) {
    assert.ok(!/\bcapturePage\s*\(/.test(text), `${file} captures a page`);
    assert.ok(!/["'`][^"'`\n]*\bscreencapture\b/.test(text), `${file} runs the screenshot tool`);
  }
});

test("listing screens to share makes no thumbnails", () => {
  let listings = 0;
  for (const { file, text } of code) {
    for (const args of calls(text, "getSources")) {
      listings += 1;
      assert.match(args, /thumbnailSize:\s*\{\s*width:\s*0,\s*height:\s*0\s*\}/, `${file}: getSources must ask for no thumbnails`);
    }
  }
  assert.ok(listings > 0, "the screen listing moved; point this check at it");
});

test("the model is offered no screenshot action", () => {
  const catalog = readFileSync(path.join(desktop, "../runtime/configs/gnsis-host-tools.json"), "utf8");
  const names = [...catalog.matchAll(/"(?:name|enum)"\s*:\s*("[^"]*"|\[[^\]]*\])/g)].map((m) => m[1]).join(" ");
  assert.ok(!/screen\s*shot|screenshot|capture_screen|screen_capture/i.test(names), "a screenshot action is offered");
});


test("Cua is never used as a screenshot polling loop", () => {
  for (const { file, text } of code) {
    assert.ok(
      !/\.call\(\s*["']get_desktop_state["']/.test(text),
      `${file} asks Cua for a desktop screenshot instead of using the persistent Panoptic stream`,
    );
    assert.ok(
      !/include_screenshot\s*:\s*true/.test(text),
      `${file} asks Cua for a window screenshot; use Panoptic for pixels`,
    );
  }
});
