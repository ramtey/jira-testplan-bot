"""Render the label sample as a local HTML page.

Same job as the CLI's `label` phase, easier on the eyes: a 70-line diff is
hard to read in a terminal window. The page is written to the results
directory and opened from disk — never published. It embeds real source from
private repositories and real ticket text, which is exactly the material this
project keeps out of its public repo.

It shows what the CLI shows and nothing more. The judge's verdict is not in
the file, so it cannot leak into a label by accident or by View Source.
"""

import html
import json

HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grounding Labels</title>
<style>
  :root {
    --bg: #fbfbfa; --panel: #fff; --ink: #1b1b19; --muted: #6b6a66;
    --line: #e4e2dd; --code-bg: #f6f5f2; --cited: #fff2c4; --cited-edge: #e0b400;
    --supports: #1a7f47; --silent: #8a6d1f; --contradicts: #b3261e;
    --accent: #2a5db0;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #17171a; --panel: #1f1f23; --ink: #e9e8e4; --muted: #9b9a95;
      --line: #32323a; --code-bg: #141417; --cited: #3b3212; --cited-edge: #a98b17;
      --supports: #5fc98a; --silent: #d4b661; --contradicts: #f08a82;
      --accent: #8ab0f0;
    }
  }
  :root[data-theme="dark"] {
    --bg: #17171a; --panel: #1f1f23; --ink: #e9e8e4; --muted: #9b9a95;
    --line: #32323a; --code-bg: #141417; --cited: #3b3212; --cited-edge: #a98b17;
    --supports: #5fc98a; --silent: #d4b661; --contradicts: #f08a82; --accent: #8ab0f0;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 10; background: var(--panel);
    border-bottom: 1px solid var(--line); padding: 10px 16px;
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  }
  .bar { flex: 1 1 200px; height: 7px; background: var(--code-bg);
         border-radius: 4px; overflow: hidden; min-width: 120px; }
  .bar i { display: block; height: 100%; background: var(--accent); width: 0; }
  .counts { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 13px; }
  .counts b { color: var(--ink); }
  button {
    font: inherit; padding: 7px 13px; border-radius: 7px; cursor: pointer;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink);
  }
  button:hover { border-color: var(--accent); }
  main { max-width: 1000px; margin: 0 auto; padding: 16px; }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 12px; padding: 18px; margin-bottom: 16px; }
  .meta { color: var(--muted); font-size: 13px; display: flex;
          gap: 10px; flex-wrap: wrap; align-items: center; }
  .tag { border: 1px solid var(--line); border-radius: 999px; padding: 1px 9px; font-size: 12px; }
  h2 { font-size: 12px; letter-spacing: .09em; text-transform: uppercase;
       color: var(--muted); margin: 18px 0 7px; font-weight: 600; }
  .assertion { font-size: 16px; line-height: 1.6; }
  .cites { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
           font-size: 13.5px; word-break: break-all; color: var(--accent); }
  pre { background: var(--code-bg); border: 1px solid var(--line); border-radius: 9px;
        padding: 0; overflow-x: auto; margin: 0; max-height: 62vh; }
  code { display: block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size: 12.5px; line-height: 1.5; }
  .ln { display: flex; padding: 0 12px; white-space: pre; }
  .ln.hit { background: var(--cited); box-shadow: inset 3px 0 0 var(--cited-edge); }
  .num { color: var(--muted); user-select: none; text-align: right;
         min-width: 48px; padding-right: 14px; flex: none; }
  .choices { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
  .choices button { flex: 1 1 200px; text-align: left; padding: 12px 14px; line-height: 1.4; }
  .choices button kbd { font-family: ui-monospace, monospace; border: 1px solid var(--line);
       border-radius: 5px; padding: 1px 6px; margin-right: 8px; }
  .choices .s { border-left: 4px solid var(--supports); }
  .choices .i { border-left: 4px solid var(--silent); }
  .choices .c { border-left: 4px solid var(--contradicts); }
  .choices button small { display: block; color: var(--muted); font-size: 12.5px; margin-top: 3px; }
  .chosen { outline: 2px solid var(--accent); }
  .nav { display: flex; gap: 10px; justify-content: space-between; align-items: center; }
  .rule { background: var(--code-bg); border-left: 3px solid var(--accent);
          padding: 11px 14px; border-radius: 0 8px 8px 0; font-size: 14px; margin-bottom: 16px; }
  .done { text-align: center; padding: 40px 20px; }
  @media (max-width: 640px) { main { padding: 12px 16px; } .card { padding: 14px; } }
</style>
</head>
<body>
<header>
  <strong>Grounding labels</strong>
  <div class="bar"><i id="fill"></i></div>
  <span class="counts" id="counts"></span>
  <button id="jump">Next unlabelled</button>
  <button id="save">Download labels.json</button>
</header>
<main>
  <div class="rule">
    The question is not <em>is the assertion true</em> — it is
    <strong>do these lines establish it</strong>. A correct assertion whose
    citation does not show the behaviour is <strong>Silent</strong>, because
    a reviewer opening that exact location could not confirm it.
  </div>
  <div id="app"></div>
</main>
<script>
const CLAIMS = __CLAIMS__;
const KEY = "grounding-labels-v1";
let labels = {};
try { labels = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { labels = {}; }
let at = 0;

const save = () => { try { localStorage.setItem(KEY, JSON.stringify(labels)); } catch (e) {} };
const esc = (s) => String(s == null ? "" : s);

function counts() {
  const n = Object.keys(labels).length;
  document.getElementById("fill").style.width = (100 * n / CLAIMS.length) + "%";
  const by = (v) => Object.values(labels).filter((x) => x === v).length;
  document.getElementById("counts").innerHTML =
    `<b>${n}</b>/${CLAIMS.length} &nbsp; supports ${by("SUPPORTS")} · ` +
    `silent ${by("SILENT")} · contradicts ${by("CONTRADICTS")} · skipped ${by("SKIP")}`;
}

function choose(v) {
  labels[CLAIMS[at].id] = v;
  save(); counts();
  if (at < CLAIMS.length - 1) { at++; render(); } else { render(); }
}

function render() {
  const c = CLAIMS[at];
  const app = document.getElementById("app");
  if (!c) { app.innerHTML = '<div class="done">All done. Download labels.json.</div>'; return; }
  const picked = labels[c.id];
  const lines = c.code.map((l) =>
    `<div class="ln${l.hit ? " hit" : ""}"><span class="num">${l.n}</span>${esc(l.text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")}</div>`).join("");
  app.innerHTML = `
    <div class="card">
      <div class="meta">
        <span class="tag">${at + 1} of ${CLAIMS.length}</span>
        <span class="tag">${esc(c.key)}</span>
        <span class="tag">${esc(c.section)}:${c.index}</span>
        ${picked ? `<span class="tag">labelled ${picked.toLowerCase()}</span>` : ""}
      </div>
      <h2>Assertion</h2>
      <div class="assertion">${esc(c.expected).replace(/&/g, "&amp;").replace(/</g, "&lt;")}</div>
      <h2>Cites</h2>
      <div class="cites">${esc(c.source).replace(/&/g, "&amp;").replace(/</g, "&lt;")}</div>
      <h2>Code at that commit</h2>
      <pre><code>${lines}</code></pre>
      <h2>Do the cited lines establish the assertion?</h2>
      <div class="choices">
        <button class="s ${picked === "SUPPORTS" ? "chosen" : ""}" onclick="choose('SUPPORTS')">
          <kbd>S</kbd>Supports<small>a reader of these lines could stop checking</small></button>
        <button class="i ${picked === "SILENT" ? "chosen" : ""}" onclick="choose('SILENT')">
          <kbd>I</kbd>Silent<small>real and related, but does not decide it</small></button>
        <button class="c ${picked === "CONTRADICTS" ? "chosen" : ""}" onclick="choose('CONTRADICTS')">
          <kbd>C</kbd>Contradicts<small>the code says otherwise</small></button>
      </div>
      <div class="choices" style="margin-top:14px">
        <button onclick="choose('SKIP')" style="flex:0 0 auto"><kbd>?</kbd>Can't tell</button>
      </div>
    </div>
    <div class="nav">
      <button onclick="go(-1)">&larr; Previous</button>
      <span class="counts">arrow keys move · S / I / C label</span>
      <button onclick="go(1)">Next &rarr;</button>
    </div>`;
  window.scrollTo(0, 0);
}

function go(d) { at = Math.min(CLAIMS.length - 1, Math.max(0, at + d)); render(); }

document.getElementById("jump").onclick = () => {
  const i = CLAIMS.findIndex((c) => !labels[c.id]);
  if (i >= 0) { at = i; render(); } else { alert("Everything is labelled."); }
};
document.getElementById("save").onclick = () => {
  const out = {};
  for (const [k, v] of Object.entries(labels)) if (v !== "SKIP") out[k] = v;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], {type: "application/json"}));
  a.download = "labels.json";
  a.click();
};
document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "s") choose("SUPPORTS");
  else if (k === "i") choose("SILENT");
  else if (k === "c") choose("CONTRADICTS");
  else if (k === "?" || k === "/") choose("SKIP");
  else if (k === "arrowright") go(1);
  else if (k === "arrowleft") go(-1);
});

counts();
const first = CLAIMS.findIndex((c) => !labels[c.id]);
at = first >= 0 ? first : 0;
render();
</script>
</body>
</html>
"""


def build(claims_for_page: list[dict]) -> str:
    """`claims_for_page` carries only what a labeller may see.

    The payload is real source code, so it can contain `</script>` — inside a
    template literal, a test fixture, a string in an HTML helper. Dropped raw
    into a script block that ends the block early and the page renders the
    rest of the file as text. Escaping `</` is the standard fix; `\u2028` and
    `\u2029` are line terminators in JS but not in JSON, and break the parse.
    """
    payload = (
        json.dumps(claims_for_page)
        .replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return HEAD.replace("__CLAIMS__", payload)


def snippet_to_lines(snippet: str) -> list[dict]:
    """Parse the rendered snippet back into {n, text, hit} rows."""
    rows = []
    for raw in snippet.splitlines():
        hit = raw.startswith(">>>")
        rest = raw[3:] if hit else raw[3:] if raw.startswith("   ") else raw
        num, _, text = rest.partition("|")
        try:
            n = int(num.strip())
        except ValueError:
            continue
        rows.append({"n": n, "text": text[1:] if text.startswith(" ") else text, "hit": hit})
    return rows
