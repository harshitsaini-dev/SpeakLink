/**
 * Filtering a chat transcript, in one place for both readers.
 *
 * The live console panel and the Broadcast History transcript show the same
 * messages for different reasons, and both need to find one. Two
 * implementations would eventually disagree about whether a removed message
 * matches "everything" - and the answer that matters is the one an operator
 * gets while looking for what somebody said.
 *
 * Deliberate decisions:
 *
 *   * a REMOVED message still matches an empty search. It is part of what
 *     happened, and hiding it by default would make a filtered transcript
 *     quietly shorter than the real one;
 *   * search covers the author as well as the body, because "what did Priya
 *     say" is the more common question than any particular word;
 *   * a removed message never matches a text search, because its words are
 *     gone - matching on them would mean the client still had them.
 */

export const CHAT_FILTERS = [
  { value: "all", label: "All messages" },
  { value: "listeners", label: "From listeners" },
  { value: "host", label: "From the host" },
  { value: "private", label: "Private only" },
  { value: "images", label: "With an image" },
  { value: "removed", label: "Removed" },
];

function matchesKind(message, kind) {
  switch (kind) {
    case "listeners": return message.author_kind === "LISTENER";
    case "host": return message.author_kind === "HOST";
    case "private": return message.visibility === "PRIVATE";
    case "images": return Boolean(message.has_image);
    case "removed": return Boolean(message.deleted);
    default: return true;
  }
}

export function filterChatMessages(messages, { query = "", kind = "all" } = {}) {
  const needle = query.trim().toLowerCase();
  return (messages || []).filter((message) => {
    if (!matchesKind(message, kind)) return false;
    if (!needle) return true;
    const author = (message.author_name || "").toLowerCase();
    // A removed message has no body to search - its words are gone, which is
    // the point of removing it.
    const body = (message.body || "").toLowerCase();
    return author.includes(needle) || body.includes(needle);
  });
}
