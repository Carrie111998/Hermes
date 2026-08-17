#!/usr/bin/env node
// Patch spectrum-ts' iMessage OUTBOUND path until upstream stops enabling
// server-side data detection on styled sends.
//
// Background: the iMessage provider renders markdown content to styled text
// (a plain string + UTF-16 formatting ranges) and calls
// `remote.messages.sendText(chat, text, { formatting })` WITHOUT passing
// `enableDataDetection`. The server then defaults data detection ON for the
// styled path, and a message whose text contains a raw URL 500s (the SDK's
// `sendText` accepts `options.enableDataDetection`; the provider just never
// sets it — see the sendText/sendMultipart options in
// `@photon-ai/advanced-imessage`). Plain-text sends are unaffected because
// iMessage auto-links bare URLs without data detection.
//
// We rewrite the two styled outbound call sites so data detection is
// explicitly OFF:
//   1. `sendContent`'s `markdown` case  -> `sendText(..., { enableDataDetection: false })`
//      (single-content sends — the path the Hermes sidecar uses for /send)
//   2. `send$1`'s group branch          -> `sendMultipart(..., { enableDataDetection: false })`
//      (group/multipart sends — same 500 class on styled parts with URLs)
//
// Result: markdown messages containing URLs send as ONE styled message, the
// URL embedded in the text, no 500, no message-splitting workaround needed.
//
// Since spectrum-ts 5.x split the SDK into scoped packages, the iMessage
// provider lives in `@spectrum-ts/imessage/dist/index.js`. The published
// output is tab-indented; the anchors below match that exactly and fail
// loudly if a future spectrum-ts reshapes the provider.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const MARKER = "Hermes patch: disable iMessage data detection on styled sends";

function scriptDir() {
  return path.dirname(fileURLToPath(import.meta.url));
}

function replaceOnce(source, from, to, label) {
  const count = source.split(from).length - 1;
  if (count !== 1) {
    throw new Error(`expected exactly one ${label} match, found ${count}`);
  }
  return source.replace(from, to);
}

// 1) sendContent markdown case: the styled sendText options object gains an
// explicit enableDataDetection: false. The anchor is unique to the markdown
// branch (text sends use `withReply(effectOption(effect), replyTo)` without a
// formatting line; richlink uses `{ enableLinkPreview: true }`).
function patchMarkdownSendText(source) {
  return replaceOnce(
    source,
    `\t\t\t\t...effectOption(effect),\n\t\t\t\t...formattingOption(rendered.formatting)\n\t\t\t}, replyTo)), content);`,
    `\t\t\t\t...effectOption(effect),\n\t\t\t\tenableDataDetection: false,\n\t\t\t\t...formattingOption(rendered.formatting)\n\t\t\t}, replyTo)), content);`,
    "sendContent markdown sendText options"
  );
}

// 2) group branch: sendMultipart currently receives no options object at all,
// so styled parts (markdown rendered to text + formatting) hit the same
// server-side data detection default. Pass an explicit false.
function patchGroupSendMultipart(source) {
  return replaceOnce(
    source,
    `\t\t\tbubbleIndex: idx\n\t\t})));`,
    `\t\t\tbubbleIndex: idx\n\t\t})), { enableDataDetection: false });`,
    "group sendMultipart options"
  );
}

export function patchSpectrumMarkdown(root = scriptDir()) {
  const dist = path.join(
    root,
    "node_modules",
    "@spectrum-ts",
    "imessage",
    "dist"
  );
  if (!fs.existsSync(dist)) {
    throw new Error(`@spectrum-ts/imessage dist not found: ${dist}`);
  }
  const files = fs.readdirSync(dist)
    .filter((name) => name.endsWith(".js"))
    .map((name) => path.join(dist, name));

  for (const file of files) {
    const raw = fs.readFileSync(file, "utf8");
    if (raw.includes(MARKER)) {
      return { patched: false, file, reason: "already patched" };
    }
    // Normalize to LF for matching so the patch works regardless of the
    // checkout's line-ending style (Windows git autocrlf produces CRLF,
    // which would otherwise defeat the \n-based search strings). The
    // original EOL style is restored on write. Indentation in the published
    // tarball is tabs; the anchors match that directly.
    const CR = String.fromCharCode(13);
    const CRLF = CR + "\n";
    const usedCRLF = raw.includes(CRLF);
    const original = usedCRLF ? raw.split(CRLF).join("\n") : raw;
    if (
      !original.includes("...formattingOption(rendered.formatting)") ||
      !original.includes("bubbleIndex: idx")
    ) {
      continue;
    }
    let patched = original;
    patched = patchMarkdownSendText(patched);
    patched = patchGroupSendMultipart(patched);
    patched = `// ${MARKER}\n${patched}`;
    if (usedCRLF) {
      patched = patched.split("\n").join(CRLF);
    }
    fs.writeFileSync(file, patched, "utf8");
    return { patched: true, file };
  }
  throw new Error("could not find @spectrum-ts/imessage styled send chunk to patch");
}

const _invokedDirectly =
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href;
if (_invokedDirectly) {
  try {
    const root = process.argv[2] ? path.resolve(process.argv[2]) : scriptDir();
    const result = patchSpectrumMarkdown(root);
    const action = result.patched ? "patched" : "ok";
    console.error(
      `photon-sidecar: spectrum styled-send data-detection patch ${action}: ${result.file}`
    );
  } catch (err) {
    console.error(
      "photon-sidecar: spectrum styled-send data-detection patch failed: " +
        (err && err.stack ? err.stack : String(err))
    );
    process.exit(1);
  }
}
