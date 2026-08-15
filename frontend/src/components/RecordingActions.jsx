import React from "react";
import { Play, Download, AlertTriangle, FileX } from "lucide-react";
import { api } from "@/lib/api";

/**
 * One Broadcast History row's recording cell.
 *
 * Deliberately owns NO playback. It reports the recording's status, offers
 * Download, and asks the page to make this recording the active one - the page
 * renders a single player at the bottom of the screen.
 *
 * That split is what guarantees one recording plays at a time: there is only
 * ever one audio element, so two cannot overlap even in principle. An earlier
 * version gave every row its own floating player and had to coordinate them
 * through a module-level subscription, which is a lot of machinery for a rule
 * the architecture can simply make true.
 *
 * Play and Download use the same shape as Rights and Scope in User Management,
 * so a recording action looks like every other action an operator knows.
 */

const ACTION_BUTTON =
  "inline-flex items-center gap-1 rounded border px-2 py-1 text-xs "
  + "hover:bg-surface-muted disabled:opacity-50";

export function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** The one place a recording's bytes are fetched, for playback or download. */
export async function fetchRecording(sessionId, kind) {
  const response = await api.get(
    `/broadcast/sessions/${sessionId}/recording/${kind}`,
    { responseType: "blob" });
  return response.data;
}

export function downloadName(sessionId) {
  // Session id only - a campaign name could carry something private into
  // somebody's downloads folder.
  return `broadcast-${String(sessionId).padStart(6, "0")}.webm`;
}

export async function saveRecording(sessionId) {
  const blob = await fetchRecording(sessionId, "download");
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = downloadName(sessionId);
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export default function RecordingActions({ sessionId, recording, onPlay,
                                           isActive = false }) {
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState(null);

  if (!recording) {
    return (
      <span data-testid={`recording-none-${sessionId}`}
            className="text-xs text-faint">
        No recording
      </span>
    );
  }

  const { status } = recording;

  if (status === "recording") {
    return (
      <span data-testid={`recording-inprogress-${sessionId}`}
            className="text-xs text-muted">
        Recording…
      </span>
    );
  }

  if (status === "failed" || status === "missing") {
    const Icon = status === "missing" ? FileX : AlertTriangle;
    return (
      <span
        data-testid={`recording-problem-${sessionId}`}
        title={recording.error || undefined}
        className="inline-flex items-center gap-1 text-xs text-red-700"
      >
        <Icon size={13} />
        {status === "missing" ? "Recording missing" : "Recording failed"}
      </span>
    );
  }

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await saveRecording(sessionId);
    } catch (failure) {
      setError(failure?.response?.status === 403
        ? "You do not have access to this recording."
        : "This recording could not be downloaded.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-1" data-testid={`recording-${sessionId}`}>
      <div className="flex items-center gap-2 flex-wrap">
        {status === "partial" && (
          <span
            data-testid={`recording-partial-${sessionId}`}
            title={recording.error || undefined}
            className="inline-flex items-center gap-1 text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded bg-amber-100 text-amber-800"
          >
            Partial
          </span>
        )}
        <span data-testid={`recording-meta-${sessionId}`}
              className="text-xs text-muted">
          {formatSize(recording.byte_size) || "—"}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onPlay(sessionId)}
          aria-pressed={isActive}
          data-testid={`recording-play-${sessionId}`}
          className={ACTION_BUTTON}
        >
          <Play size={14} /> Play
        </button>

        <button
          type="button"
          onClick={save}
          disabled={saving}
          data-testid={`recording-download-${sessionId}`}
          className={ACTION_BUTTON}
        >
          <Download size={14} /> {saving ? "Preparing…" : "Download"}
        </button>
      </div>

      {error && (
        <p role="alert" data-testid={`recording-error-${sessionId}`}
           className="text-xs text-red-700">{error}</p>
      )}
    </div>
  );
}
