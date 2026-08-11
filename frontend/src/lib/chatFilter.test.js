/**
 * The chat filter, which both readers share.
 *
 * The console panel and the history transcript filter the same way because
 * they call the same function - and the decisions worth pinning down are the
 * ones a second implementation would get wrong: a removed message is still
 * part of the transcript, but its words are gone and cannot be searched.
 */
import { filterChatMessages } from "./chatFilter";

const MESSAGES = [
  { id: 1, author_kind: "LISTENER", author_name: "Harshit",
    body: "We cannot hear you at the till", visibility: "PUBLIC",
    deleted: false, has_image: false },
  { id: 2, author_kind: "HOST", author_name: "superadmin",
    body: "Repeating it now", visibility: "PUBLIC",
    deleted: false, has_image: false },
  { id: 3, author_kind: "LISTENER", author_name: "Priya",
    body: "the amplifier is off", visibility: "PRIVATE",
    deleted: false, has_image: true },
  { id: 4, author_kind: "LISTENER", author_name: "Harshit",
    body: null, visibility: "PUBLIC", deleted: true, has_image: false },
];

const ids = (result) => result.map((message) => message.id);

test("no filter returns everything, removed messages included", () => {
  // A filtered transcript that is quietly shorter than the real one would
  // misrepresent what happened.
  expect(ids(filterChatMessages(MESSAGES))).toEqual([1, 2, 3, 4]);
});

test("search matches the body, case-insensitively", () => {
  expect(ids(filterChatMessages(MESSAGES, { query: "AMPLIFIER" }))).toEqual([3]);
});

test("search matches the author, because that is the commoner question", () => {
  expect(ids(filterChatMessages(MESSAGES, { query: "harshit" }))).toEqual([1, 4]);
});

test("a removed message never matches a word search", () => {
  // Its words are gone. Matching them would mean the client still had them.
  expect(ids(filterChatMessages(MESSAGES, { query: "till" }))).toEqual([1]);
});

test("the kind filters pick out each audience", () => {
  expect(ids(filterChatMessages(MESSAGES, { kind: "listeners" }))).toEqual([1, 3, 4]);
  expect(ids(filterChatMessages(MESSAGES, { kind: "host" }))).toEqual([2]);
  expect(ids(filterChatMessages(MESSAGES, { kind: "private" }))).toEqual([3]);
  expect(ids(filterChatMessages(MESSAGES, { kind: "images" }))).toEqual([3]);
  expect(ids(filterChatMessages(MESSAGES, { kind: "removed" }))).toEqual([4]);
});

test("a search and a filter apply together", () => {
  expect(ids(filterChatMessages(MESSAGES, { query: "priya", kind: "private" })))
    .toEqual([3]);
  expect(ids(filterChatMessages(MESSAGES, { query: "priya", kind: "host" })))
    .toEqual([]);
});

test("whitespace is not a search", () => {
  expect(ids(filterChatMessages(MESSAGES, { query: "   " }))).toEqual([1, 2, 3, 4]);
});

test("an absent transcript filters to nothing rather than throwing", () => {
  expect(filterChatMessages(undefined, { query: "x" })).toEqual([]);
});
