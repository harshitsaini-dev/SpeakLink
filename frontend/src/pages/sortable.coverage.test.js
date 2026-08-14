/**
 * Every data column on every list is sortable.
 *
 * WHY A SOURCE-READING TEST
 *
 * This was reported to me one column at a time - Mode, then Type, then Role,
 * then Play Status, then Lifecycle - and each report cost a round trip. The
 * fault was never in any one column: it was that nothing checked the whole
 * set, so a table gained a column and nobody noticed it was not sortable.
 *
 * This reads the JSX and fails on any labelled <th> that is not a SortableTh
 * or PickerTh. Columns that genuinely cannot be sorted are named below WITH A
 * REASON, so the exception is a decision somebody made rather than an
 * oversight nobody spotted.
 */
import fs from "fs";
import path from "path";

const PAGES = [
  "StoreManagement", "UserManagement", "SystemLogs", "BroadcastHistory",
  "ReceiverDeviceFleet", "ReceiverStatus", "ActiveBroadcasts", "ReceiverDevices",
  "Announcements", "AnnouncementTemplates", "AnnouncementRecordings",
  "AnnouncementHistory", "BroadcastConsole",
];

//: Columns that are deliberately not sortable, and why. Sorting these would
//: order rows by something the column does not actually contain.
const NOT_SORTABLE = {
  Actions: "buttons, not data",
  Recording: "a player and a download button, not a value",
  "Plays in": "a list of lines per template; there is no single value to order by",
  Status: "per-Store page, not paginated",
  Volume: "a slider, not a value",
  "Play Status": "the Console picker sorts this locally, through PickerTh",
  "In Broadcast": "add and remove buttons for a live session, not a value",
  "Store Output": "a volume slider per Store, not a value",
  Primary: "rendered from a flag the Devices column already orders",
};

function labelledHeaders(source) {
  // <th ...>Some Label</th> where the label is real text rather than markup.
  const found = [];
  const pattern = /<th\b[^>]*>\s*([A-Za-z#][^<{]*?)\s*<\/th>/g;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    const label = match[1].trim();
    if (label) found.push(label);
  }
  return found;
}

test.each(PAGES)("%s: every labelled column is sortable or explained", (name) => {
  const source = fs.readFileSync(
    path.join(__dirname, `${name}.jsx`), "utf8");

  const unexplained = labelledHeaders(source)
    .filter((label) => !(label in NOT_SORTABLE));

  expect(unexplained).toEqual([]);
});

test("the exceptions are stated with a reason, not just listed", () => {
  // An allowlist of bare names would decay into "whatever was easiest at the
  // time". Each entry has to say why.
  for (const [column, reason] of Object.entries(NOT_SORTABLE)) {
    expect(typeof reason).toBe("string");
    expect(reason.length).toBeGreaterThan(10);
    expect(column.length).toBeGreaterThan(0);
  }
});
