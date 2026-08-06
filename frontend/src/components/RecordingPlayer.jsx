import React from "react";
import { Play, Download, AlertTriangle, FileX } from "lucide-react";
import { api } from "@/lib/api";

/**
 * Plays back one broadcast's recording, honestly.
 *
 * The audio never comes from a public folder. It is fetched through an
 * authenticated API route with the caller's token attached, turned into a
 * blob URL for the browser's audio element, and revoked when this unmounts.
 * That is why the recordings directory is not, and must never be, a static
 * mount: playback is a permissioned read of a real announcement.
 *
 * A recording has five possible states and only one of them is a Play button.
 * PARTIAL is playable and says so - a recording with a gap is still the best
 * evidence of what went out - while FAILED and MISSING explain themselves
 * rather than offering a control that would do nothing.
 */

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  return `${minutes}:${String(whole % 60).padStart(2, "0")}`;
}

function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function RecordingPlayer({ sessionId, recording }) {
  const [source, setSource] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState(null);

  React.useEffect(() => () => {
    // A blob URL holds the whole recording in memory until it is revoked.
    if (source) URL.revokeObjectURL(source);
  }, [source]);

  if (!recording) {
    return (
      <span data-testid={`recording-none-${sessionId}`}
            className="text-xs text-slate-400">
        No recording
      </span>
    );
  }

  const { status } = recording;

  if (status === "recording") {
    return (
      <span data-testid={`recording-inprogress-${sessionId}`}
            className="text-xs text-slate-500">
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

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      // Through the API client, so the Authorization header goes with it.
      // A bare <audio src> would be an unauthenticated request and would
      // rightly be refused.
      const response = await api.get(
        `/broadcast/sessions/${sessionId}/recording/audio`,
        { responseType: "blob" });
      setSource(URL.createObjectURL(response.data));
    } catch (failure) {
      setError(failure?.response?.status === 403
        ? "You do not have access to this recording."
        : "This recording could not be loaded.");
    } finally {
      setLoading(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Through the API client for the same reason playback is: the token has
      // to travel with the request, and the recordings folder is not public.
      const response = await api.get(
        `/broadcast/sessions/${sessionId}/recording/download`,
        { responseType: "blob" });
      const url = URL.createObjectURL(response.data);
      const link = document.createElement("a");
      link.href = url;
      // Session id only. A campaign name could carry something private into
      // somebody's downloads folder.
      link.download = `broadcast-${String(sessionId).padStart(6, "0")}.webm`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
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
      <div className="flex items-center gap-2">
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
              className="text-xs text-slate-500">
          {[formatDuration(recording.duration_seconds),
            formatSize(recording.byte_size)].filter(Boolean).join(" · ") || "—"}
        </span>
      </div>

      <div className="flex items-center gap-3">
        {!source && (
          <button
            type="button"
            onClick={load}
            disabled={loading}
            data-testid={`recording-play-${sessionId}`}
            className="inline-flex items-center gap-1 text-xs text-blue-700 hover:text-blue-900 disabled:opacity-50"
          >
            <Play size={13} /> {loading ? "Loading…" : "Play Recording"}
          </button>
        )}

        <button
          type="button"
          onClick={save}
          disabled={saving}
          data-testid={`recording-download-${sessionId}`}
          className="inline-flex items-center gap-1 text-xs text-blue-700 hover:text-blue-900 disabled:opacity-50"
        >
          <Download size={13} /> {saving ? "Preparing…" : "Download"}
        </button>
      </div>

      {source && (
        // Seeking works because the API answers byte-range requests; without
        // that some browsers refuse to seek in a WebM at all.
        <audio
          controls
          src={source}
          data-testid={`recording-audio-${sessionId}`}
          className="h-8 w-full max-w-xs"
        >
          <track kind="captions" />
        </audio>
      )}

      {error && (
        <p role="alert" data-testid={`recording-error-${sessionId}`}
           className="text-xs text-red-700">{error}</p>
      )}
    </div>
  );
}
