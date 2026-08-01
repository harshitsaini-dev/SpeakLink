/**
 * The header literally contained "HQ Broadcast Console Â· v1.0" - a
 * double-decode mojibake artifact pasted directly into the JSX source, not a
 * build/charset pipeline defect. This reads the actual source file so the
 * assertion is against what ships, not a paraphrase of it.
 */
import fs from "fs";
import path from "path";

const SOURCE = fs.readFileSync(path.join(__dirname, "Layout.jsx"), "utf8");

test("the header never contains the mojibake byte sequence", () => {
  expect(SOURCE).not.toContain("Â");
});

test("the header renders the clean UTF-8 middle dot", () => {
  expect(SOURCE).toContain("HQ Broadcast Console · v1.0");
});

test("the Windows 11 / Local Server / SQLite environment banner is gone", () => {
  expect(SOURCE).not.toContain("Windows 11");
  expect(SOURCE).not.toContain("Local Server");
  expect(SOURCE).not.toMatch(/text-slate-500 hidden sm:block[^<]*SQLite/);
});
