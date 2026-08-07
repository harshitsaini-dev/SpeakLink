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
 * other part of SpeakLink a slider labelled "volume" moves a shop's speakers,
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

export default function RecordingPlayer({ session, onClose,
                                          playToken = 0,
                                          pauseToken = 0 }) {
  const sessionId = session?.id ?? null;

  // A source is never just a URL. It carries the recording it belongs to, so
  // a play request for B can never be executed against A's audio.
  const [loaded, setLoaded] = React.useState(null);   // { sessionId, url }
  const [loading, setLoading] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState(null);

  // Playback state comes from the audio element's own events, never from the
  // fact that a button was pressed. "Playing" has to mean playing.
  const [playing, setPlaying] = React.useState(false);
  const [ended, setEnded] = React.useState(false);
  const [position, setPosition] = React.useState(0);
  //: The one animation frame handle. A ref rather than state because starting
  //: or stopping the loop must not itself cause a render, and because the
  //: cleanup paths need the CURRENT handle, not the one captured by whichever
  //: render happened to schedule it.
  const frameRef = React.useRef(null);
  const [duration, setDuration] = React.useState(null);
  const [volume, setVolume] = React.useState(1);
  const [muted, setMuted] = React.useState(false);

  const audioRef = React.useRef(null);
  const loadedRef = React.useRef(null);
  //: Bumped on every selection. A fetch that resolves after a newer one has
  //: started belongs to a recording the operator has already moved on from,
  //: and must not replace what is attached now.
  const generation = React.useRef(0);
  //: "<sessionId>:<playToken>" of the request this element has already acted
  //: on. Keyed by SESSION as well as token, so a token alone can never start
  //: whatever source happens to still be attached.
  const startedFor = React.useRef(null);
  //: The selected recording, readable synchronously. Media events can fire
  //: while React is still committing, so the handlers below compare identity
  //: against refs rather than against a render closure that may be one
  //: render behind - which is how a genuine `play` event ended up ignored.
  const selectedRef = React.useRef(null);
  selectedRef.current = sessionId;

  /** Is the element's current source the recording the operator selected? */
  const isCurrent = React.useCallback(() => (
    loadedRef.current !== null
    && loadedRef.current.sessionId === selectedRef.current
  ), []);

  /**
   * Take the current recording off the element, then let its memory go.
   *
   * The order matters. Revoking a blob URL the browser is still reading from
   * leaves the element holding a source it can no longer fetch - which is how
   * a switch ended up with the footer naming B while nothing was audible. So
   * the element is paused, the src attribute is removed, and load() is called
   * to make the detach take effect BEFORE the URL is revoked.
   */
  const detachAndRelease = React.useCallback(() => {
    // The previous recording's frame loop dies with the previous recording. A
    // surviving loop would keep writing A's position while B is attached.
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }
    if (loadedRef.current) {
      URL.revokeObjectURL(loadedRef.current.url);
      loadedRef.current = null;
    }
  }, []);

  // Selecting a different recording tears the previous one down completely
  // before anything of the new one is attached.
  React.useEffect(() => {
    const mine = generation.current + 1;
    generation.current = mine;

    setPlaying(false);
    setEnded(false);
    setPosition(0);
    setDuration(null);
    setError(null);
    setLoaded(null);
    detachAndRelease();

    if (sessionId === null) return undefined;

    setLoading(true);
    fetchRecording(sessionId, "audio")
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        if (mine !== generation.current) {
          // A slower request for a recording the operator has already left.
          // Its URL is revoked immediately rather than leaked, and it never
          // touches the element.
          URL.revokeObjectURL(url);
          return;
        }
        loadedRef.current = { sessionId, url };
        setLoaded({ sessionId, url });
      })
      .catch((failure) => {
        if (mine !== generation.current) return;
        setError(failure?.response?.status === 403
          ? "You do not have access to this recording."
          : "This recording could not be loaded.");
      })
      .finally(() => { if (mine === generation.current) setLoading(false); });

    return undefined;
  }, [sessionId, detachAndRelease]);

  React.useEffect(() => () => { detachAndRelease(); }, [detachAndRelease]);

  // ONE click on History's Play has to mean play, and exactly one place
  // decides that.
  //
  // There used to be two effects that could each call play() for the same
  // request. Between them, a play intent for B could run while A's source was
  // still attached - which is how a switch produced a footer saying B with
  // nothing audible. This fires only when the attached source IS the selected
  // recording, and the request is keyed by session as well as token so a
  // token can never start the wrong audio.
  React.useEffect(() => {
    if (!playToken || !loaded || loaded.sessionId !== sessionId) return;
    const key = `${sessionId}:${playToken}`;
    if (startedFor.current === key) return;
    const audio = audioRef.current;
    if (!audio) return;

    startedFor.current = key;
    const started = audio.play();
    if (started && typeof started.catch === "function") {
      started
        // Read the element rather than assume. The `play` event is the primary
        // signal, but asking the element what it is doing once its own promise
        // has settled is strictly MORE truthful than trusting an event to be
        // delivered - and it is what keeps the label honest if one is missed.
        .then(() => { if (isCurrent()) setPlaying(!audio.paused); })
        .catch(() => {
          // Chromium can refuse if the user gesture has expired by the time
          // the fetch finished. Said plainly, with the transport still
          // available, rather than silently doing nothing.
          setError("Playback could not start automatically. Press play.");
        });
    } else if (isCurrent()) {
      setPlaying(!audio.paused);
    }
  }, [loaded, sessionId, playToken, isCurrent]);

  // A live broadcast starting: pause, but keep the selection so the operator
  // can carry on afterwards. A recording playing out of the HQ speakers can be
  // picked up by the HQ microphone and go out over the announcement.
  React.useEffect(() => {
    if (!pauseToken) return;
    if (audioRef.current) audioRef.current.pause();
  }, [pauseToken]);

  React.useEffect(() => {
    if (sessionId === null) return undefined;
    const onKey = (event) => { if (event.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [sessionId, onClose]);

  /**
   * Stop following playback. Safe to call when nothing is running.
   *
   * Every path that ends playback calls this - pause, ended, error, close,
   * unmount, a switch to another recording - because a loop that outlives its
   * audio keeps painting a bar for a recording nobody is playing.
   */
  const stopFollowing = React.useCallback(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
  }, []);

  /**
   * Follow real playback, one frame at a time.
   *
   * `timeupdate` fires roughly every 265 ms in Chromium - measured, not
   * assumed - so a bar driven by it alone advances in visible quarter-second
   * jumps. This samples the media element every animation frame instead.
   *
   * The element remains the clock. Nothing here advances position by elapsed
   * wall time: a loop that counted milliseconds would drift away from the audio
   * the moment the browser throttled it, and would keep "playing" through a
   * stall. If the audio is not moving, neither is the bar.
   */
  const startFollowing = React.useCallback(() => {
    // Exactly one loop. Re-entering Play while already following must not
    // schedule a second one - two loops would both call setPosition, and
    // cancelling one would leave the other running invisibly.
    if (frameRef.current !== null) return;
    const step = () => {
      const audio = audioRef.current;
      if (!audio || audio.paused || audio.ended) {
        frameRef.current = null;
        return;
      }
      setPosition(audio.currentTime);
      frameRef.current = requestAnimationFrame(step);
    };
    frameRef.current = requestAnimationFrame(step);
  }, []);

  // Cleanup belongs to THIS component, which lives above the router - not to
  // whichever page happened to start the recording. Navigating from History to
  // Active Broadcasts must not stop the audio or the bar.
  React.useEffect(() => stopFollowing, [stopFollowing]);

  if (sessionId === null) return null;

  const togglePlay = async () => {
    const audio = audioRef.current;
    if (!audio) return;
    try {
      if (audio.paused) await audio.play();
      else audio.pause();
      // Same reason: the element is the authority on whether it is playing.
      setPlaying(!audio.paused);
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
    // Immediately, not on the next frame and not animated: a seek is a jump,
    // and a bar that slid to the new position would be describing playback
    // that never happened.
    setPosition(value);
    setEnded(false);
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

  // Only a source that belongs to the SELECTED recording counts as attached.
  const attached = loaded && loaded.sessionId === sessionId ? loaded : null;
  const seekable = Number.isFinite(duration) && duration > 0;
  /**
   * How full the bar LOOKS. Deliberately separate from position and duration,
   * which stay exactly what the media element reports.
   *
   * A finished recording is 100% by definition, and it has to be stated rather
   * than derived: the last timeupdate fires slightly before the end, so
   * currentTime at `ended` is routinely a few tens of milliseconds short of
   * duration. Deriving the bar from that leaves it visibly unfinished on a
   * recording that has finished - and "nearly" is not what Finished means.
   *
   * This never changes the audio or the clock. Only the paint.
   */
  const visualProgressPercent = ended ? 100
    : seekable ? Math.min(100, Math.max(0, (position / duration) * 100))
    : 0;
  const state = error ? "Playback failed"
    : loading ? "Preparing…"
    : ended ? "Finished"
    : playing ? "Playing"
    : attached ? "Paused"
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
        src={attached ? attached.url : undefined}
        // Which recording is REALLY attached, so a test can prove the element
        // is playing what the footer claims rather than trusting the label.
        data-active-session-id={attached ? String(attached.sessionId) : ""}
        data-testid="recording-audio"
        className="hidden"
        // Every event is ignored unless the attached source is still the
        // selected recording: a late `play` from the previous one must never
        // mark the new one as playing.
        onPlay={() => {
          if (!isCurrent()) return;
          setPlaying(true);
          setEnded(false);
          startFollowing();
        }}
        onPause={() => {
          if (!isCurrent()) return;
          setPlaying(false);
          // Cancelled here rather than left to notice on its own, so the bar
          // stops exactly where the audio stopped.
          stopFollowing();
          const audio = audioRef.current;
          if (audio) setPosition(audio.currentTime);
        }}
        onEnded={() => {
          if (!isCurrent()) return;
          setPlaying(false);
          stopFollowing();
          setEnded(true);
        }}
        onError={() => {
          if (!isCurrent()) return;
          stopFollowing();
          setError("Playback failed.");
        }}
        // Still honoured: it is the only position source while a seek happens
        // on a paused element, and it costs nothing beside the frame loop.
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
                    disabled={!attached}
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
            {/* The played portion is a real element, not the browser's own
                paint.

                A native range draws its accent fill up to the THUMB CENTRE, and
                the thumb's travel is inset by half its width at each end - so
                at value === max the fill stops about nine pixels short and the
                operator sees a grey sliver on a recording that has finished.
                Nothing in the DOM represents that fill either, so it could
                neither be corrected nor measured.

                So the fill is drawn here and the range sits on top of it,
                transparent. The control is still a real input: keyboard, focus
                and the Seek label are unchanged. */}
            {/* Everything visible is ours; the range is real but invisible.
                A native thumb's CENTRE travels inside an inset equal to half
                its width, so at value === max it sits ~6px short of the right
                edge - measured, not assumed. That is unfixable from outside the
                widget, so the thumb that is SEEN is our own element, positioned
                by the same percentage as the fill.

                The input keeps its size, focus, keyboard and pointer handling.
                It is not hidden, not display:none and not pointer-events:none -
                only its own track and thumb are made transparent. */}
            <div className="relative flex-1 h-4 flex items-center overflow-visible"
                 data-testid="recording-seek-track">
              <div className="pointer-events-none absolute inset-x-0 h-1 rounded bg-slate-700"
                   data-testid="recording-seek-background" />
              <div
                className="pointer-events-none absolute left-0 h-1 rounded bg-red-500"
                data-testid="recording-seek-fill"
                style={{ width: `${visualProgressPercent}%` }}
              />
              {seekable && (
                <div
                  className="pointer-events-none absolute h-3 w-3 rounded-full bg-red-500 shadow"
                  data-testid="recording-seek-thumb"
                  // translateX(-50%) is what makes `left` mean the thumb's
                  // CENTRE. At 100% the centre lands exactly on the track's
                  // right edge and the circle overhangs by half its width,
                  // which is correct - the centre is the position.
                  style={{ left: `${visualProgressPercent}%`,
                           transform: "translateX(-50%)" }}
                />
              )}
              <input
                type="range"
                min="0"
                max={seekable ? duration : 1}
                step="0.01"
                value={seekable ? Math.min(position, duration) : 0}
                disabled={!seekable}
                aria-label="Seek"
                data-testid="recording-seek"
                onChange={(event) => seekTo(Number(event.target.value))}
                className="absolute inset-x-0 w-full h-4 appearance-none bg-transparent
                           cursor-pointer focus:outline-none
                           [&::-webkit-slider-runnable-track]:bg-transparent
                           [&::-webkit-slider-thumb]:appearance-none
                           [&::-webkit-slider-thumb]:h-3
                           [&::-webkit-slider-thumb]:w-3
                           [&::-webkit-slider-thumb]:rounded-full
                           [&::-webkit-slider-thumb]:bg-transparent
                           [&::-moz-range-track]:bg-transparent
                           [&::-moz-range-thumb]:h-3
                           [&::-moz-range-thumb]:w-3
                           [&::-moz-range-thumb]:border-0
                           [&::-moz-range-thumb]:bg-transparent"
              />
            </div>
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
