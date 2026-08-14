/**
 * A permission group the catalog has must never be invisible in the editor.
 *
 * The group order was ALSO the list of groups that exist, so a group added to
 * the backend catalog simply did not appear here: the rights existed, the API
 * enforced them, and there was no way to grant them to anybody. Announcements
 * was invisible for exactly that reason.
 *
 * A new group in the wrong position is cosmetic. A new group that is absent is
 * a right nobody can give.
 */
import { orderGroups } from "./UserManagement";

test("known groups keep their order", () => {
  expect(orderGroups(["Users", "Broadcast", "Stores"]))
    .toEqual(["Broadcast", "Stores", "Users"]);
});

test("a group this file has never heard of is still shown, after the known ones", () => {
  expect(orderGroups(["Users", "Something New", "Broadcast"]))
    .toEqual(["Broadcast", "Users", "Something New"]);
});

test("Announcements sits with the operational groups rather than at the end", () => {
  const ordered = orderGroups(["Users", "Announcements", "Broadcast", "Logs"]);
  expect(ordered.indexOf("Announcements")).toBe(1);
  expect(ordered.indexOf("Announcements")).toBeLessThan(ordered.indexOf("Logs"));
});

test("nothing is invented when there are no groups", () => {
  expect(orderGroups([])).toEqual([]);
});
