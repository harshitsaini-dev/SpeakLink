/**
 * The theme is a design, not a correction.
 *
 * WHY THIS EXISTS
 *
 * Dark mode shipped first as a "bridge": a block of `.dark .bg-white { ... }`
 * rules that re-pointed literal Tailwind colours, so that pages written
 * before dark mode existed were legible in it. That was the right way to get
 * a whole product converted in one step, and it leaked exactly the way
 * overrides leak - a rule for `green` while the page used `emerald`, and a
 * rule for `input` that painted over a slider meant to be transparent, which
 * made the recording player look like it had stopped playing.
 *
 * The bridge is gone and every page names what a thing IS. This file keeps it
 * gone. A literal surface colour added to a page tomorrow is invisible in
 * review - it looks completely normal - and shows up only as one white card
 * in a dark window, on somebody else's screen.
 */

const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "..");

//: The sidebar and the player strip are dark in BOTH themes by design: they
//: are the product's fixed point, and semantic names there would make them
//: follow the page instead. They are allowed their literals, deliberately.
const SHELL = new Set([
  "Layout.jsx", "RecordingPlayer.jsx", "ThemeToggle.jsx",
  "EmergencyStopControl.jsx",
]);

//: Surface, text and line colours only. Tinted colours - emerald, amber,
//: rose, blue - carry MEANING and stay literal: a pill is green because the
//: thing it describes is fine.
const BANNED = [
  "bg-white", "bg-slate-50", "bg-slate-100", "bg-slate-200",
  "bg-slate-800", "bg-slate-900", "bg-slate-950",
  "text-slate-100", "text-slate-300", "text-slate-400", "text-slate-500",
  "text-slate-600", "text-slate-700", "text-slate-800", "text-slate-900",
  "border-slate-100", "border-slate-200", "border-slate-300",
  "border-slate-700", "border-slate-800",
  "divide-slate-100", "divide-slate-200",
];

function sourceFiles() {
  const out = [];
  for (const folder of ["pages", "components"]) {
    const directory = path.join(SRC, folder);
    for (const name of fs.readdirSync(directory)) {
      if (!name.endsWith(".jsx")) continue;
      if (SHELL.has(name)) continue;
      out.push([`${folder}/${name}`, path.join(directory, name)]);
    }
  }
  return out;
}

test("no page names a literal surface, text or line colour", () => {
  const offenders = [];
  for (const [label, file] of sourceFiles()) {
    const source = fs.readFileSync(file, "utf8");
    for (const banned of BANNED) {
      const pattern = new RegExp(`(?<![\\w-])${banned}(?![\\w-])`);
      if (pattern.test(source)) offenders.push(`${label} -> ${banned}`);
    }
  }
  expect(offenders).toEqual([]);
});

test("the theme stylesheet does not re-point literal classes", () => {
  const theme = fs.readFileSync(path.join(SRC, "styles", "theme.css"), "utf8")
    // Comments stripped first. The block explaining what the bridge WAS
    // necessarily contains the words this test bans, and a guard that trips
    // over its own explanation teaches people to delete the explanation.
    .replace(/\/\*[\s\S]*?\*\//g, "");
  // Anything of the shape `.dark .bg-white { ... }` is a bridge rule, whatever
  // it is called. The palette for MEANING colours - `.dark .bg-emerald-100` -
  // is a different thing and is expected.
  const bridge = theme.match(
    /\.dark\s+\.(bg-white|bg-slate-\d+|text-slate-\d+|border-slate-\d+)\b/g);
  expect(bridge).toBeNull();
});

test("the semantic vocabulary defines every name the pages use", () => {
  const semantic = fs.readFileSync(path.join(SRC, "styles", "semantic.css"), "utf8");
  for (const name of [
    "bg-surface", "bg-surface-muted", "bg-surface-alt",
    "text-strong", "text-body", "text-muted", "text-faint",
    "border-line", "border-line-strong", "divide-line",
  ]) {
    expect(semantic).toContain(`.${name}`);
  }
});
