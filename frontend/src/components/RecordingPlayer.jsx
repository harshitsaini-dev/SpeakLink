import React from "react";
import {
  Play, Pause, Download, X, Volume2, VolumeX, RotateCcw, RotateCw,
} from "lucide-react";
import { fetchRecording, formatSize, saveRecording } from "./RecordingActions";

/**
 * The Broadcast History recording player: one fixed bar along the bottom.
 *
 * WHY A BAR AND NOT A PANEL BESIDE THE ROW
 *
 * An earlier version floated a card next to whichever Play button was pressed.
 * It had to measure the button, flip above the row near the bottom of the
 * screen, follow scrolling, and close itself when the operator clicked
 * anywhere else - and it still disappeared the moment somebody scrolled to
 * find the next recording. A player belongs where a player belongs: pinned to
 * the bottom, out of the table's way, and still there while the operator keeps
 * working.
 *
 * ONE PLAYER, BY CONSTRUCTION
 *
 * The History page owns which recording is active and renders exactly one of
 * these. Two recordings cannot overlap because there is only ever one audio
 * element - not because instances coordinate with each other.
 *
 * THE VOLUME HERE IS LOCAL PLAYBACK ONLY
 *
 * It sets `audio.volume` on this browser's element. It is not a Store control,
 * not HQ microphone gain, and it never reaches a Windows endpoint. In every
 * other part of EchoCast a slider labelled "volume" moves a shop's speakers,
 * so this one says on screen what it does and the tests assert that no Store
 * request is ever generated.
 *
 * SECURITY IS UNCHANGED
 *
 * Audio is fetched through the authenticated API and turned into a blob URL.
 * The recordings folder is not public, no token appears in a URL, and no
 * filesystem path reaches the browser.
 */

/** Roughly the bar's height, so the page can keep its last row reachable. */
export const PLAYER_BAR_HEIGHT = 96;

function formatClock(seconds) {
  if (seconds === null || seconds === undefined
      || Number.isNaN(seconds) || !Number.isFinite(seconds)) {
    // Deliberately not 0:00, which would read as a real position.
    return "—:—";
  }
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export default function RecordingPlayer({ session, onClose }) {
  const sessionId = session?.id ?? null;

  const [source, setSource] = React.useState(null);
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState(null);

  // Playback state comes from the audio element's own events, never from the
  // fact that a button was pressed. "Playing" has to mean playing.
  const [playing, setPlaying] = React.useState(false);
  const [ended, setEnded] = React.useState(false);
  const [position, setPosition] = React.useState(0);
  const [duration, setDuration] = React.useState(null);
  const [volume, setVolume] = React.useState(1);
  const [muted, setMuted] = React.useState(false);

  const audioRef = React.useRef(null);
  const sourceRef = React.useRef(null);

  const release = React.useCallback(() => {
    if (sourceRef.current) {
      // A blob URL pins the whole recording in memory until it is revoked.
      URL.revokeObjectURL(sourceRef.current);
      sourceRef.current = null;
    }
  }, []);

  // Selecting a different recording, or leaving the page, tears the previous
  // one down completely: paused, revoked, and forgotten.
  React.useEffect(() => {
    let cancelled = false;
    setPlaying(false);
    setEnded(false);
    setPosition(0);
    setDuration(null);
    setError(null);
    setSource(null);
    if (audioRef.current) audioRef.current.pause();
    release();

    if (sessionId === null) return undefined;

    setLoading(true);
    fetchRecording(sessionId, "audio")
      .then((blob) => {
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        sourceRef.current = url;
        setSource(url);
      })
      .catch((failure) => {
        if (cancelled) return;
        setError(failure?.response?.status === 403
          ? "You do not have access to this recording."
          : "This recording could not be loaded.");
      })
      .finally(() => { if (!cancelled) setLoading(false); });

    return () => { cancelled = true; };
  }, [sessionId, release]);

  React.useEffect(() => () => {
    if (audioRef.current) audioRef.current.pause();
    release();
  }, [release]);

  React.useEffect(() => {
    if (sessionId === null) return undefined;
    const onKey = (event) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sessionId, onClose]);

  if (sessionId === null) return null;

  const togglePlay = async () => {
    const audio = audioRef.current;
    if (!audio) return;
    try {
      if (audio.paused) await audio.play();
      else audio.pause();
    } catch {
      setError("Playback failed.");
      setPlaying(false);
    }
  };

  const nudge = (seconds) => {
    const audio = audioRef.current;
    if (!audio) return;
    const limit = Number.isFinite(duration) && duration
      ? duration : audio.currentTime;
    audio.currentTime = Math.max(0, Math.min(limit, audio.currentTime + seconds));
    setPosition(audio.currentTime);
  };

  const seekTo = (value) => {
    const audio = audioRef.current;
    if (!audio || !Number.isFinite(duration) || !duration) return;
    audio.currentTime = value;
    setPosition(value);
  };

  const changeVolume = (value) => {
    const audio = audioRef.current;
    setVolume(value);
    // LOCAL playback only. This never becomes a Store command.
    if (audio) {
      audio.volume = value;
      if (value > 0 && audio.muted) {
        audio.muted = false;
        setMuted(false);
      }
    }
  };

  const toggleMute = () => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.muted = !audio.muted;      // the chosen level survives a mute
    setMuted(audio.muted);
  };

  const save = async () => {
    setSaving(true);
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

  const seekable = Number.isFinite(duration) && duration > 0;
  const state = error ? "Playback failed"
    : loading ? "Preparing…"
    : ended ? "Finished"
    : playing ? "Playing"
    : source ? "Paused"
    : "Ready";

  return (
    <section
      aria-label="Broadcast recording player"
      data-testid="recording-player-bar"
      style={{ minHeight: PLAYER_BAR_HEIGHT }}
      // Starts where the sidebar ends, so it never covers the navigation.
      // No backdrop and no overlay: History's filters, checkboxes and
      // pagination all stay usable while this is open.
      className="fixed bottom-0 left-0 md:left-64 right-0 z-40 border-t border-slate-700 bg-slate-900 text-slate-100 shadow-lg px-4 py-3"
    >
      {/* The audio element is REAL but never shown: no browser-native widget,
          and every control here drives this directly. */}
      <audio
        ref={audioRef}
        src={source || undefined}
        data-testid="recording-audio"
        className="hidden"
        onPlay={() => { setPlaying(true); setEnded(false); }}
        onPause={() => setPlaying(false)}
        onEnded={() => { setPlaying(false); setEnded(true); }}
        onError={() => setError("Playback failed.")}
        onTimeUpdate={(event) => setPosition(event.target.currentTime)}
        onLoadedMetadata={(event) => {
          const value = event.target.duration;
          setDuration(Number.isFinite(value) ? value : null);
        }}
        onDurationChange={(event) => {
          const value = event.target.duration;
          setDuration(Number.isFinite(value) ? value : null);
        }}
      >
        <track kind="captions" />
      </audio>

      <div className="flex items-center gap-4 flex-wrap md:flex-nowrap">
        {/* ---- what is playing ---- */}
        <div className="min-w-0 md:w-64">
          <p className="text-[10px] uppercase tracking-[0.15em] text-slate-400">
            Broadcast Recording
          </p>
          <p className="text-sm font-semibold truncate"
             data-testid="recording-campaign">
            {session.campaign_name || "—"}
          </p>
          <p className="text-xs text-slate-400 truncate" data-testid="recording-session">
            Broadcast #{sessionId}
            {session.recording?.byte_size
              ? ` · ${formatSize(session.recording.byte_size)}` : ""}
          </p>
        </div>

        {/* ---- transport ---- */}
        <div className="flex-1 min-w-0 flex flex-col items-center gap-1">
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => nudge(-10)}
                    aria-label="Back 10 seconds"
                    data-testid="recording-back"
                    className="rounded border border-slate-700 p-1.5 hover:bg-slate-800">
              <RotateCcw size={14} />
            </button>
            <button type="button" onClick={togglePlay}
                    disabled={!source}
                    aria-label={playing ? "Pause" : "Play"}
                    data-testid="recording-toggle"
                    className="rounded-full bg-red-600 hover:bg-red-500 p-2 disabled:opacity-50">
              {playing ? <Pause size={16} /> : <Play size={16} />}
            </button>
            <button type="button" onClick={() => nudge(10)}
                    aria-label="Forward 10 seconds"
                    data-testid="recording-forward"
                    className="rounded border border-slate-700 p-1.5 hover:bg-slate-800">
              <RotateCw size={14} />
            </button>
            <span className="text-[11px] text-slate-400 ml-2"
                  data-testid="recording-state">{state}</span>
          </div>

          <div className="w-full flex items-center gap-2">
            <span className="text-[11px] tabular-nums text-slate-400 w-10 text-right"
                  data-testid="recording-position">
              {formatClock(position)}
            </span>
            <input
              type="range"
              min="0"
              max={seekable ? duration : 1}
              step="0.1"
              value={seekable ? Math.min(position, duration) : 0}
              disabled={!seekable}
              aria-label="Seek"
              data-testid="recording-seek"
              onChange={(event) => seekTo(Number(event.target.value))}
              className="flex-1 accent-red-500"
            />
            <span className="text-[11px] tabular-nums text-slate-400 w-10"
                  data-testid="recording-duration">
              {/* A WebM written by MediaRecorder carries no duration in its
                  header, so the element may never expose one. An em dash
                  rather than a number invented from the session's length. */}
              {formatClock(duration)}
            </span>
          </div>
        </div>

        {/* ---- local playback volume, download, close ---- */}
        <div className="flex items-center gap-2">
          <button type="button" onClick={toggleMute}
                  aria-label={muted ? "Unmute playback" : "Mute playback"}
                  aria-pressed={muted}
                  data-testid="recording-mute"
                  className="rounded border border-slate-700 p-1.5 hover:bg-slate-800">
            {muted ? <VolumeX size={14} /> : <Volume2 size={14} />}
          </button>
          <input
            type="range"
            min="0" max="1" step="0.05"
            value={volume}
            aria-label="Playback volume"
            title="Affects this browser only"
            data-testid="recording-volume"
            onChange={(event) => changeVolume(Number(event.target.value))}
            className="w-20 accent-red-500"
          />
          <button
            type="button"
            onClick={save}
            disabled={saving}
            data-testid="recording-bar-download"
            className="inline-flex items-center gap-1 rounded border border-slate-700 px-2 py-1 text-xs hover:bg-slate-800 disabled:opacity-50"
          >
            <Download size={13} /> {saving ? "Preparing…" : "Download"}
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close recording player"
            data-testid="recording-close"
            className="rounded border border-slate-700 p-1.5 hover:bg-slate-800"
          >
            <X size={14} />
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" data-testid="recording-bar-error"
           className="text-xs text-red-400 mt-1">{error}</p>
      )}
    </section>
  );
}
