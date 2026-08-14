import React from "react";
import { Trash2 } from "lucide-react";

/**
 * Select Page / Select All Filtered, and the actions that follow.
 *
 * WHY THE TWO MODES ARE DIFFERENT THINGS
 *
 * The browser only ever holds one page. A selection built there can act on
 * fifty rows; a button saying "Select All Filtered (184)" is a promise about
 * 184. So "all filtered" is not a list of ids at all - it is the current
 * filters, sent to the server, which resolves them against the same query
 * that produced the page. Anything else is a button that lies about its own
 * count.
 *
 * WHY ONE CONFIRMATION FOR THE WHOLE SELECTION
 *
 * Asking two hundred times is not two hundred times the protection. It is a
 * person clicking through the same dialog until they stop reading it. Once,
 * with the number in the sentence, is the version somebody actually reads.
 */
export function useBulkSelection(list) {
  const [selected, setSelected] = React.useState(() => new Set());
  const [allFiltered, setAllFiltered] = React.useState(false);

  // A page or filter change invalidates a selection built on the rows that
  // were there before it. Keeping it would act on rows the operator can no
  // longer see, which is the shape of every "it deleted the wrong things"
  // report.
  React.useEffect(() => {
    setSelected(new Set());
    setAllFiltered(false);
  }, [list.page, list.filters]);

  const clear = React.useCallback(() => {
    setSelected(new Set());
    setAllFiltered(false);
  }, []);

  const toggle = React.useCallback((id) => {
    setAllFiltered(false);
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  return {
    selected, allFiltered, clear, toggle,
    count: allFiltered ? list.total : selected.size,
    isChosen: (id) => allFiltered || selected.has(id),
    selectPage: () => { setAllFiltered(false);
                        setSelected(new Set(list.items.map((row) => row.id))); },
    selectAllFiltered: () => { setSelected(new Set()); setAllFiltered(true); },
    chooseOnly: (id) => { setAllFiltered(false); setSelected(new Set([id])); },
    body: () => (allFiltered
      ? { mode: "filtered", filters: list.filters }
      : { mode: "ids", ids: Array.from(selected) }),
  };
}

export function BulkBar({ bulk, list, testIdPrefix, onArchive, onDelete,
                          archiveLabel = "Archive selected",
                          deleteLabel = "Delete selected", busy }) {
  return (
    <div className="flex flex-wrap items-center gap-2 border border-slate-200 rounded-md bg-white px-3 py-2"
         data-testid={`${testIdPrefix}-bulk-bar`}>
      <button data-testid={`${testIdPrefix}-select-page`} onClick={bulk.selectPage}
              className="px-3 py-1.5 rounded border border-slate-300 text-sm hover:bg-slate-50">
        Select Page ({list.items.length})
      </button>
      <button data-testid={`${testIdPrefix}-select-all`} onClick={bulk.selectAllFiltered}
              className="px-3 py-1.5 rounded border border-slate-300 text-sm hover:bg-slate-50">
        Select All Filtered ({list.total})
      </button>
      {bulk.count > 0 && (
        <>
          <span className="text-sm text-slate-600" data-testid={`${testIdPrefix}-chosen`}>
            {bulk.count} selected
          </span>
          <button data-testid={`${testIdPrefix}-clear-selection`} onClick={bulk.clear}
                  className="px-3 py-1.5 rounded border border-slate-300 text-sm hover:bg-slate-50">
            Clear
          </button>
          {onArchive && (
            <button data-testid={`${testIdPrefix}-bulk-archive`} disabled={busy}
                    onClick={onArchive}
                    className="px-3 py-1.5 rounded border border-slate-300 text-sm hover:bg-slate-50 disabled:opacity-50">
              {archiveLabel}
            </button>
          )}
          {onDelete && (
            <button data-testid={`${testIdPrefix}-bulk-delete`} onClick={onDelete}
                    className="inline-flex items-center gap-1 px-3 py-1.5 rounded border border-rose-300 text-sm text-rose-700 hover:bg-rose-50">
              <Trash2 className="w-4 h-4" /> {deleteLabel}
            </button>
          )}
        </>
      )}
    </div>
  );
}

/** The typed word, once, with the count in the sentence. */
export function BulkDeleteConfirm({ count, noun, warning, testIdPrefix,
                                    onConfirm, onCancel }) {
  const [word, setWord] = React.useState("");
  return (
    <div className="rounded-md border border-rose-200 bg-rose-50 px-4 py-3 space-y-2"
         data-testid={`${testIdPrefix}-delete-confirm`}>
      <p className="text-sm text-rose-900">
        Delete <strong>{count}</strong> {count === 1 ? noun : `${noun}s`}{" "}
        permanently? {warning}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <label className="text-sm text-rose-900">Type DELETE to confirm:</label>
        <input value={word} onChange={(event) => setWord(event.target.value)}
               data-testid={`${testIdPrefix}-delete-word`}
               className="px-3 py-2 border border-rose-300 rounded-md text-sm w-32" />
        <button data-testid={`${testIdPrefix}-delete-confirm-btn`}
                disabled={word.trim().toUpperCase() !== "DELETE"}
                onClick={() => onConfirm(word)}
                className="px-3 py-2 rounded-md text-sm text-white bg-rose-700 hover:bg-rose-800 disabled:opacity-40">
          Delete permanently
        </button>
        <button onClick={onCancel} data-testid={`${testIdPrefix}-delete-cancel`}
                className="px-3 py-2 rounded-md text-sm border border-slate-300 hover:bg-white">
          Cancel
        </button>
      </div>
    </div>
  );
}
