import React from "react";
import ReactDOM from "react-dom";
import { Search, X, Loader2, Download } from "lucide-react";
import { api } from "@/lib/api";

/**
 * The shared controls every server-filtered admin list needs.
 *
 * Six screens use these, so a fix here fixes all six - and, more usefully,
 * none of them can quietly disagree about what "no results" looks like.
 * Three states are deliberately distinct, because collapsing them is how a
 * blank page gets mistaken for an empty result:
 *
 *   loading  - a request is in flight; say so rather than showing an empty table
 *   error    - the request failed; say THAT rather than "nothing found"
 *   empty    - the request succeeded and genuinely matched nothing
 */

export function FilterBar({ children, onClear, activeCount = 0, total, loading }) {
  return (
    <div className="glass rounded-xl p-3 space-y-3"
         data-testid="filter-bar">
      <div className="flex flex-wrap items-end gap-2">{children}</div>
      <div className="flex items-center justify-between border-t border-line pt-2">
        <div className="text-xs text-muted" data-testid="result-count">
          {loading ? (
            <span className="inline-flex items-center gap-1">
              <Loader2 size={12} className="animate-spin" /> Loading…
            </span>
          ) : (
            <>
              <span className="font-semibold text-body">{total ?? 0}</span>
              {" "}result{total === 1 ? "" : "s"}
              {activeCount > 0 && (
                <span className="text-faint"> · {activeCount} filter{activeCount === 1 ? "" : "s"} active</span>
              )}
            </>
          )}
        </div>
        {activeCount > 0 && (
          <button type="button" onClick={onClear} data-testid="clear-filters"
                  className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-line-strong rounded hover:bg-surface-muted">
            <X size={12} /> Clear Filters
          </button>
        )}
      </div>
    </div>
  );
}

export function SearchInput({ value, onChange, placeholder = "Search…", testId = "search-input" }) {
  return (
    <label className="relative flex-1 min-w-[180px]">
      <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-faint" />
      <input
        data-testid={testId}
        className="w-full pl-7 pr-2 py-1.5 text-sm border border-line-strong rounded-md"
        value={value || ""} placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  );
}

/**
 * A filter dropdown.
 *
 * `allLabel` renders the empty "no filter" option. Pass `allLabel={null}` for
 * an EXCLUSIVE filter whose options already cover every case - a lifecycle,
 * say. Keeping the placeholder there would add a fifth meaning nobody chose,
 * and when that meaning happens to compute the same rows as one of the real
 * options you get two labels for one state. That is exactly what "Active
 * only" and "Active" were on the Receiver Devices screen.
 */
/**
 * One filter, which may name SEVERAL values.
 *
 * The single-value dropdown answered "how is Nehru Place doing" and could not
 * answer "how are these six shops doing" - a zone with an exception in it, a
 * handful in one market, the three that were complaining this morning. Running
 * the search six times and comparing six screens is arithmetic done by the
 * reader.
 *
 * The value on the wire stays a plain comma-separated string, so every
 * existing link, bookmark and test keeps working: one value is a list of one.
 *
 * Checkboxes rather than a <select multiple>. That control needs Ctrl-click to
 * take a second item, gives no sign that it does, and REPLACES the selection
 * on a plain click - so choosing a fifth item looks like it deselected the
 * other four.
 */
export function FilterSelect({ label, value, onChange, options = [], testId,
                               allLabel = "All", multiple = true,
                               disabled = false, selectedSummary = null }) {
  const [open, setOpen] = React.useState(false);
  //: Searching WITHIN a filter.
  //:
  //: Forty Stores is a scroll; two hundred is a hunt. The list a filter offers
  //: is exactly as long as the estate, so a control that can only be scrolled
  //: gets slower to use the more there is to use it on - which is backwards.
  const [needle, setNeedle] = React.useState("");
  const holder = React.useRef(null);

  // Defaulted, and defensively so. A page passes `options={options.stores}`
  // where that list arrives from a second request - and until it does, or if
  // it fails, the value is undefined. Reading .map off it took a whole page
  // white, which is the failure mode this control must never have: a filter
  // with nothing to offer is a filter with nothing to offer, not a broken
  // screen.
  const normalised = (options || []).map((option) => (
    typeof option === "string" ? { value: option, label: option } : option));
  const chosen = String(value ?? "").split(",").map((v) => v.trim()).filter(Boolean);
  const term = needle.trim().toLowerCase();
  // Chosen values stay visible whatever is typed. Filtering them out would
  // hide what is already in effect, and somebody would untick something they
  // could no longer see.
  const shown = term
    ? normalised.filter((option) =>
        String(option.label).toLowerCase().includes(term)
        || chosen.includes(String(option.value)))
    : normalised;

  // Closing on an outside click, because a panel that only closes via its own
  // button is a panel that covers the table while somebody reads it.
  React.useEffect(() => {
    if (!open) return undefined;
    const away = (event) => {
      // The panel is in <body> now, so "inside the control" means inside
      // EITHER the holder or the panel. Without this, clicking a checkbox
      // closes the very list it is in.
      if (holder.current?.contains(event.target)) return;
      if (panel.current?.contains(event.target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  // A stale search term inside a closed panel is a filter that looks empty
  // when it is not.
  React.useEffect(() => { if (!open) setNeedle(""); }, [open]);

  if (!multiple) {
    return (
      <label className="flex flex-col gap-1">
        <span className="text-[10px] uppercase tracking-widest text-muted">{label}</span>
        <select data-testid={testId} value={value ?? ""}
                onChange={(e) => onChange(e.target.value)}
                className="px-2 py-1.5 text-sm border border-line-strong rounded-md bg-surface min-w-[120px]">
          {allLabel !== null && <option value="">{allLabel}</option>}
          {normalised.map((option) => (
            <option key={option.value} value={option.value}>{option.label}</option>
          ))}
        </select>
      </label>
    );
  }

  const toggle = (candidate) => {
    const next = chosen.includes(candidate)
      ? chosen.filter((entry) => entry !== candidate)
      : [...chosen, candidate];
    onChange(next.join(","));
  };

  const summary = selectedSummary !== null
    ? selectedSummary
    : chosen.length === 0
    ? allLabel
    : chosen.length === 1
      ? (normalised.find((option) => option.value === chosen[0])?.label || chosen[0])
      : `${chosen.length} selected`;

  const panel = React.useRef(null);
  const [placement, setPlacement] = React.useState({ top: 0, left: 0 });

  // DRAWN OUTSIDE THE PAGE, POSITIONED OVER IT.
  //
  // The panel used to be an absolutely positioned child of the filter bar,
  // and every filter bar in this product sits inside a panel that clips its
  // own overflow - so on every page the list of Zones or Stores was cut off
  // at the bottom of the card, and a long one was mostly invisible. No amount
  // of z-index fixes that: an ancestor's overflow wins.
  //
  // So it is rendered into <body> and placed against the trigger's rectangle.
  // It also flips above the button when there is more room up there, which is
  // what a filter at the bottom of a table needs.
  React.useLayoutEffect(() => {
    if (!open) return undefined;

    const place = () => {
      const trigger = holder.current?.querySelector("button");
      if (!trigger) return;
      const rect = trigger.getBoundingClientRect();
      const height = panel.current?.offsetHeight || 260;
      const below = window.innerHeight - rect.bottom;
      const flip = below < height + 12 && rect.top > below;
      setPlacement({
        top: flip ? Math.max(8, rect.top - height - 4) : rect.bottom + 4,
        left: Math.min(Math.max(8, rect.left),
                       Math.max(8, window.innerWidth - 232)),
      });
    };

    place();
    // A scroll or a resize moves the button; the panel has to follow it or it
    // ends up pointing at nothing.
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, shown.length]);

  return (
    <div className="flex flex-col gap-1 relative" ref={holder}>
      <span className="text-[10px] uppercase tracking-widest text-muted">{label}</span>
      <button type="button" data-testid={testId} disabled={disabled}
              onClick={() => setOpen((was) => !was)}
              className="px-2 py-1.5 text-sm border border-line-strong rounded-md bg-surface min-w-[140px] text-left disabled:opacity-50">
        {summary}
      </button>
      {open && ReactDOM.createPortal(
        <div data-testid={`${testId}-panel`}
             ref={panel}
             style={placement}
             className="fixed z-50 w-56 max-h-64 overflow-y-auto
                        border border-line-strong rounded-md bg-surface shadow-lg p-2">
          {/* Always, not only on long lists.
              I gated this on the list being long and the reasoning was wrong:
              the person opening a filter does not know how long the list is
              until it is open, and a control that sometimes has a search box
              teaches nobody where to type. A field above four options costs a
              line; a missing one costs a hunt through two hundred. */}
          <input value={needle} onChange={(event) => setNeedle(event.target.value)}
                 data-testid={`${testId}-search`} placeholder="Search…"
                 className="w-full mb-1 px-2 py-1 text-sm border border-line-strong rounded" />
          <button type="button" data-testid={`${testId}-clear`}
                  onClick={() => onChange("")}
                  className="w-full text-left px-2 py-1 text-sm rounded hover:bg-surface-muted text-body">
            {allLabel}
          </button>
          {shown.map((option) => (
            <label key={option.value}
                   className="flex items-center gap-2 px-2 py-1 text-sm rounded hover:bg-surface-muted cursor-pointer">
              <input type="checkbox" checked={chosen.includes(String(option.value))}
                     data-testid={`${testId}-option-${option.value}`}
                     onChange={() => toggle(String(option.value))} />
              {option.label}
            </label>
          ))}
          {normalised.length === 0 && (
            <p className="px-2 py-1 text-xs text-muted">Nothing to choose from yet.</p>
          )}
          {normalised.length > 0 && shown.length === 0 && (
            <p className="px-2 py-1 text-xs text-muted"
               data-testid={`${testId}-no-match`}>
              Nothing here matches “{needle}”.
            </p>
          )}
        </div>,
        document.body)}
    </div>
  );
}

/**
 * One value, chosen from a list that may be long.
 *
 * A plain <select> is fine for four fixed options and useless for two hundred
 * Stores: it can only be scrolled, and it gets slower to use the more there is
 * to use it on. This is the same panel the multi-value filter uses, with the
 * choosing part made exclusive - so "Zone", "City" and "Store" pickers behave
 * the same way everywhere, whether they take one value or several.
 */
export function SearchableSelect({ label, value, onChange, options = [], testId,
                                   placeholder = "— select —", disabled }) {
  const normalised = (options || []).map((option) => (
    typeof option === "string" ? { value: option, label: option } : option));
  const chosen = normalised.find((option) => String(option.value) === String(value ?? ""));
  return (
    <FilterSelect label={label} testId={testId} allLabel={placeholder}
                  value={value ?? ""} options={normalised} disabled={disabled}
                  // Exclusive: choosing replaces rather than adds. The panel
                  // and its search are identical, which is the point - one
                  // gesture to learn, not two.
                  onChange={(next) => {
                    const values = String(next).split(",").map((v) => v.trim())
                      .filter(Boolean);
                    const added = values.find((v) => v !== String(value ?? ""));
                    onChange(added ?? "");
                  }}
                  selectedSummary={chosen ? chosen.label : placeholder} />
  );
}



/**
 * A sortable column heading.
 *
 * WHY SORTING GOES TO THE SERVER
 *
 * Sorting the rows the browser is holding would order one page of fifty and
 * leave the other three hundred where they were - a table that claims to be
 * sorted and is not. That is worse than an unsorted one, because the reader
 * stops checking after the first time it looks right.
 *
 * So a click sets `sort` and `dir` on the query, and the server orders the
 * whole set before paginating it.
 */
export function SortableTh({ column, label, list, className = "",
                             align = "left", thTestId = null }) {
  const active = list.filters.sort === column;
  const direction = active ? (list.filters.dir || "asc") : null;

  const toggle = () => {
    if (!active) {
      list.setFilters((current) => ({ ...current, sort: column, dir: "asc" }));
      return;
    }
    // Third click clears the sort rather than cycling back to ascending.
    // Without it there is no way back to the list's own order, which for a
    // history is "newest first" - the order somebody actually wants most of
    // the time.
    if (direction === "asc") {
      list.setFilters((current) => ({ ...current, dir: "desc" }));
    } else {
      list.setFilters((current) => ({ ...current, sort: "", dir: "asc" }));
    }
  };

  return (
    <th className={`px-3 py-2 ${className}`}
        // Kept when a caller asks for it: a column that exists only for
        // accounts holding a permission is tested by that id, and losing it
        // would quietly retire the test that proves the column is hidden.
        data-testid={thTestId || undefined}
        style={{ textAlign: align }}
        aria-sort={active ? (direction === "desc" ? "descending" : "ascending")
                          : "none"}>
      <button type="button" onClick={toggle}
              data-testid={`sort-${column}`}
              className="inline-flex items-center gap-1 hover:text-strong">
        {label}
        <span aria-hidden="true" className={active ? "text-strong" : "text-faint"}>
          {active ? (direction === "desc" ? "↓" : "↑") : "⇅"}
        </span>
      </button>
    </th>
  );
}

/**
 * Download the CURRENT filters as a spreadsheet.
 *
 * The whole filtered set, not the page on screen: an export giving fifty rows
 * while the table says 184 would be read as the answer and acted on, and
 * nobody would know to check.
 */
export function ExportButton({ dataset, list, testId = "export-button",
                               disabled = false }) {
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState("");

  async function download() {
    setBusy(true);
    setFailure("");
    try {
      const response = await api.get(`/export/${dataset}`, {
        params: { ...activeParams(list.filters) },
        responseType: "blob",
      });
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `speaklink-${dataset}.csv`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      // Said out loud rather than left as a download that silently never
      // arrives - the most confusing failure a button like this can have.
      setFailure(error?.response?.status === 403
        ? "You do not have permission to export this list."
        : "That export could not be produced. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-2">
      <button type="button" onClick={download} disabled={busy || disabled}
              data-testid={testId}
              className="inline-flex items-center gap-1 px-3 py-2 border border-line-strong rounded-md text-sm hover:bg-surface-muted disabled:opacity-50">
        <Download size={14} /> {busy ? "Preparing…" : "Export"}
      </button>
      {failure && (
        <span className="text-xs text-rose-700" data-testid={`${testId}-error`}>
          {failure}
        </span>
      )}
    </span>
  );
}

function activeParams(filters) {
  const params = {};
  for (const [key, value] of Object.entries(filters || {})) {
    if (value !== "" && value !== null && value !== undefined) params[key] = value;
  }
  return params;
}

export function FilterDate({ label, value, onChange, testId }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-widest text-muted">{label}</span>
      <input type="date" data-testid={testId} value={value || ""}
             onChange={(e) => onChange(e.target.value)}
             className="px-2 py-1.5 text-sm border border-line-strong rounded-md bg-surface" />
    </label>
  );
}

/** Loading / error / empty, kept apart on purpose. */
export function ListState({ loading, error, empty, emptyText, colSpan, onRetry }) {
  if (loading) {
    return (
      <tr><td colSpan={colSpan} className="px-3 py-8 text-center text-muted" data-testid="list-loading">
        <Loader2 size={16} className="inline animate-spin mr-1" /> Loading…
      </td></tr>
    );
  }
  if (error) {
    return (
      <tr><td colSpan={colSpan} className="px-3 py-8 text-center" data-testid="list-error">
        <div className="text-sm text-red-700">{error}</div>
        {onRetry && (
          <button type="button" onClick={onRetry}
                  className="mt-2 text-xs px-2 py-1 border border-line-strong rounded hover:bg-surface-muted">
            Try again
          </button>
        )}
      </td></tr>
    );
  }
  if (empty) {
    return (
      <tr><td colSpan={colSpan} className="px-3 py-8 text-center text-muted"
              data-testid="list-empty">{emptyText}</td></tr>
    );
  }
  return null;
}

export function Pager({ page, pages, total, hasMore, onPage }) {
  if (!total) return null;
  return (
    <div className="flex items-center justify-between px-3 py-2 border-t border-line text-xs"
         data-testid="pager">
      <span className="text-muted">Page {page} of {Math.max(pages, 1)}</span>
      <div className="flex gap-1">
        <button type="button" data-testid="page-prev" disabled={page <= 1}
                onClick={() => onPage(page - 1)}
                className="px-2 py-1 border border-line-strong rounded disabled:opacity-40 hover:bg-surface-muted">
          Previous
        </button>
        <button type="button" data-testid="page-next" disabled={!hasMore}
                onClick={() => onPage(page + 1)}
                className="px-2 py-1 border border-line-strong rounded disabled:opacity-40 hover:bg-surface-muted">
          Next
        </button>
      </div>
    </div>
  );
}

/**
 * The bulk-selection bar.
 *
 * "Select All Filtered" is offered only when there is more than one page of
 * matches, and it says how many rows it means. The count comes from the
 * server's own total, so the number the operator agrees to is the number the
 * backend will act on - React never enumerates the ids.
 */
export function BulkBar({ selection, total, pageCount, children }) {
  if (!selection.hasSelection && pageCount === 0) return null;
  const showSelectAll = selection.mode !== "filtered" && total > pageCount;
  return (
    <div className="flex flex-wrap items-center gap-2 border border-line rounded-md bg-surface-muted px-3 py-2"
         data-testid="bulk-bar">
      <button type="button" onClick={selection.selectPage} data-testid="select-page"
              className="text-xs px-2 py-1 border border-line-strong rounded bg-surface hover:bg-surface-muted">
        Select Page ({pageCount})
      </button>
      {showSelectAll && (
        <button type="button" onClick={selection.selectAllFiltered} data-testid="select-all-filtered"
                className="text-xs px-2 py-1 border border-blue-300 text-blue-800 rounded bg-surface hover:bg-blue-50">
          Select All Filtered ({total})
        </button>
      )}
      {selection.hasSelection && (
        <>
          <span className="text-xs text-body" data-testid="selected-count">
            {selection.selectedCount} selected
            {selection.mode === "filtered" && " (all matches, including other pages)"}
          </span>
          <button type="button" onClick={selection.clear} data-testid="clear-selection"
                  className="text-xs px-2 py-1 border border-line-strong rounded bg-surface hover:bg-surface-muted">
            Clear
          </button>
          <span className="flex-1" />
          {children}
        </>
      )}
    </div>
  );
}

/**
 * Destructive confirmation: exact count, a typed word, and a separate
 * acknowledgement. Both are required before the button enables - neither
 * alone can be got through by muscle memory.
 */
export function DestructiveModal({
  title, count, countNoun, confirmWord, warning, busy, error,
  onCancel, onConfirm, testIdPrefix,
}) {
  const [typed, setTyped] = React.useState("");
  const [acknowledged, setAcknowledged] = React.useState(false);
  const ready = typed === confirmWord && acknowledged && !busy;

  return (
    <div className="fixed inset-0 z-50 scrim flex items-center justify-center p-4"
         data-testid={`${testIdPrefix}-modal`}>
      <div className="glass w-full max-w-md p-5 space-y-3">
        <h3 className="font-semibold text-red-900">{title}</h3>
        <p className="text-sm text-body" data-testid={`${testIdPrefix}-count`}>
          This will permanently remove{" "}
          <strong>{count} {countNoun}{count === 1 ? "" : "s"}</strong>.
        </p>
        <p className="text-sm text-red-800">{warning}</p>

        <label htmlFor={`${testIdPrefix}-confirm`}
               className="block text-xs font-bold uppercase tracking-widest text-muted">
          Type <span className="font-mono">{confirmWord}</span> to confirm
        </label>
        <input id={`${testIdPrefix}-confirm`} data-testid={`${testIdPrefix}-confirm-input`}
               className="w-full rounded border border-line-strong px-3 py-2 text-sm font-mono"
               value={typed} autoComplete="off" placeholder={confirmWord}
               onChange={(e) => setTyped(e.target.value)} />

        <label className="flex items-start gap-2 text-sm pt-1">
          <input type="checkbox" data-testid={`${testIdPrefix}-acknowledge`}
                 checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
          <span>I understand this cannot be undone.</span>
        </label>

        {error && (
          <div role="alert" data-testid={`${testIdPrefix}-error`}
               className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="flex gap-2 pt-1">
          <button type="button" data-testid={`${testIdPrefix}-cancel`} onClick={onCancel}
                  className="flex-1 px-4 py-2 border border-line-strong rounded-md text-sm">
            Cancel
          </button>
          <button type="button" data-testid={`${testIdPrefix}-confirm-btn`} disabled={!ready}
                  onClick={() => onConfirm({ typed, acknowledged })}
                  className="flex-1 px-4 py-2 bg-red-700 text-white rounded-md text-sm disabled:opacity-40">
            {busy ? "Working…" : "Delete Permanently"}
          </button>
        </div>
      </div>
    </div>
  );
}
