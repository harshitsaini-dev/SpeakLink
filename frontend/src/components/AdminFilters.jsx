import React from "react";
import { Search, X, Loader2 } from "lucide-react";

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
    <div className="border border-slate-200 rounded-md bg-white p-3 space-y-3"
         data-testid="filter-bar">
      <div className="flex flex-wrap items-end gap-2">{children}</div>
      <div className="flex items-center justify-between border-t border-slate-100 pt-2">
        <div className="text-xs text-slate-500" data-testid="result-count">
          {loading ? (
            <span className="inline-flex items-center gap-1">
              <Loader2 size={12} className="animate-spin" /> Loading…
            </span>
          ) : (
            <>
              <span className="font-semibold text-slate-700">{total ?? 0}</span>
              {" "}result{total === 1 ? "" : "s"}
              {activeCount > 0 && (
                <span className="text-slate-400"> · {activeCount} filter{activeCount === 1 ? "" : "s"} active</span>
              )}
            </>
          )}
        </div>
        {activeCount > 0 && (
          <button type="button" onClick={onClear} data-testid="clear-filters"
                  className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-300 rounded hover:bg-slate-50">
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
      <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" />
      <input
        data-testid={testId}
        className="w-full pl-7 pr-2 py-1.5 text-sm border border-slate-300 rounded-md"
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
export function FilterSelect({ label, value, onChange, options, testId,
                               allLabel = "All", multiple = true }) {
  const [open, setOpen] = React.useState(false);
  //: Searching WITHIN a filter.
  //:
  //: Forty Stores is a scroll; two hundred is a hunt. The list a filter offers
  //: is exactly as long as the estate, so a control that can only be scrolled
  //: gets slower to use the more there is to use it on - which is backwards.
  const [needle, setNeedle] = React.useState("");
  const holder = React.useRef(null);

  const normalised = options.map((option) => (
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
      if (holder.current && !holder.current.contains(event.target)) setOpen(false);
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
        <span className="text-[10px] uppercase tracking-widest text-slate-500">{label}</span>
        <select data-testid={testId} value={value ?? ""}
                onChange={(e) => onChange(e.target.value)}
                className="px-2 py-1.5 text-sm border border-slate-300 rounded-md bg-white min-w-[120px]">
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

  const summary = chosen.length === 0
    ? allLabel
    : chosen.length === 1
      ? (normalised.find((option) => option.value === chosen[0])?.label || chosen[0])
      : `${chosen.length} selected`;

  return (
    <div className="flex flex-col gap-1 relative" ref={holder}>
      <span className="text-[10px] uppercase tracking-widest text-slate-500">{label}</span>
      <button type="button" data-testid={testId} onClick={() => setOpen((was) => !was)}
              className="px-2 py-1.5 text-sm border border-slate-300 rounded-md bg-white min-w-[140px] text-left">
        {summary}
      </button>
      {open && (
        <div data-testid={`${testId}-panel`}
             className="absolute z-20 top-full mt-1 w-56 max-h-64 overflow-y-auto
                        border border-slate-300 rounded-md bg-white shadow-lg p-2">
          {normalised.length > 8 && (
            <input value={needle} onChange={(event) => setNeedle(event.target.value)}
                   data-testid={`${testId}-search`} placeholder="Search…"
                   className="w-full mb-1 px-2 py-1 text-sm border border-slate-300 rounded" />
          )}
          <button type="button" data-testid={`${testId}-clear`}
                  onClick={() => onChange("")}
                  className="w-full text-left px-2 py-1 text-sm rounded hover:bg-slate-50 text-slate-600">
            {allLabel}
          </button>
          {shown.map((option) => (
            <label key={option.value}
                   className="flex items-center gap-2 px-2 py-1 text-sm rounded hover:bg-slate-50 cursor-pointer">
              <input type="checkbox" checked={chosen.includes(String(option.value))}
                     data-testid={`${testId}-option-${option.value}`}
                     onChange={() => toggle(String(option.value))} />
              {option.label}
            </label>
          ))}
          {normalised.length === 0 && (
            <p className="px-2 py-1 text-xs text-slate-500">Nothing to choose from yet.</p>
          )}
          {normalised.length > 0 && shown.length === 0 && (
            <p className="px-2 py-1 text-xs text-slate-500"
               data-testid={`${testId}-no-match`}>
              Nothing here matches “{needle}”.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

export function FilterDate({ label, value, onChange, testId }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-widest text-slate-500">{label}</span>
      <input type="date" data-testid={testId} value={value || ""}
             onChange={(e) => onChange(e.target.value)}
             className="px-2 py-1.5 text-sm border border-slate-300 rounded-md bg-white" />
    </label>
  );
}

/** Loading / error / empty, kept apart on purpose. */
export function ListState({ loading, error, empty, emptyText, colSpan, onRetry }) {
  if (loading) {
    return (
      <tr><td colSpan={colSpan} className="px-3 py-8 text-center text-slate-500" data-testid="list-loading">
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
                  className="mt-2 text-xs px-2 py-1 border border-slate-300 rounded hover:bg-slate-50">
            Try again
          </button>
        )}
      </td></tr>
    );
  }
  if (empty) {
    return (
      <tr><td colSpan={colSpan} className="px-3 py-8 text-center text-slate-500"
              data-testid="list-empty">{emptyText}</td></tr>
    );
  }
  return null;
}

export function Pager({ page, pages, total, hasMore, onPage }) {
  if (!total) return null;
  return (
    <div className="flex items-center justify-between px-3 py-2 border-t border-slate-200 text-xs"
         data-testid="pager">
      <span className="text-slate-500">Page {page} of {Math.max(pages, 1)}</span>
      <div className="flex gap-1">
        <button type="button" data-testid="page-prev" disabled={page <= 1}
                onClick={() => onPage(page - 1)}
                className="px-2 py-1 border border-slate-300 rounded disabled:opacity-40 hover:bg-slate-50">
          Previous
        </button>
        <button type="button" data-testid="page-next" disabled={!hasMore}
                onClick={() => onPage(page + 1)}
                className="px-2 py-1 border border-slate-300 rounded disabled:opacity-40 hover:bg-slate-50">
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
    <div className="flex flex-wrap items-center gap-2 border border-slate-200 rounded-md bg-slate-50 px-3 py-2"
         data-testid="bulk-bar">
      <button type="button" onClick={selection.selectPage} data-testid="select-page"
              className="text-xs px-2 py-1 border border-slate-300 rounded bg-white hover:bg-slate-50">
        Select Page ({pageCount})
      </button>
      {showSelectAll && (
        <button type="button" onClick={selection.selectAllFiltered} data-testid="select-all-filtered"
                className="text-xs px-2 py-1 border border-blue-300 text-blue-800 rounded bg-white hover:bg-blue-50">
          Select All Filtered ({total})
        </button>
      )}
      {selection.hasSelection && (
        <>
          <span className="text-xs text-slate-600" data-testid="selected-count">
            {selection.selectedCount} selected
            {selection.mode === "filtered" && " (all matches, including other pages)"}
          </span>
          <button type="button" onClick={selection.clear} data-testid="clear-selection"
                  className="text-xs px-2 py-1 border border-slate-300 rounded bg-white hover:bg-slate-50">
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
    <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
         data-testid={`${testIdPrefix}-modal`}>
      <div className="bg-white rounded-lg w-full max-w-md p-5 space-y-3">
        <h3 className="font-semibold text-red-900">{title}</h3>
        <p className="text-sm text-slate-700" data-testid={`${testIdPrefix}-count`}>
          This will permanently remove{" "}
          <strong>{count} {countNoun}{count === 1 ? "" : "s"}</strong>.
        </p>
        <p className="text-sm text-red-800">{warning}</p>

        <label htmlFor={`${testIdPrefix}-confirm`}
               className="block text-xs font-bold uppercase tracking-widest text-slate-500">
          Type <span className="font-mono">{confirmWord}</span> to confirm
        </label>
        <input id={`${testIdPrefix}-confirm`} data-testid={`${testIdPrefix}-confirm-input`}
               className="w-full rounded border border-slate-300 px-3 py-2 text-sm font-mono"
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
                  className="flex-1 px-4 py-2 border border-slate-300 rounded-md text-sm">
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
