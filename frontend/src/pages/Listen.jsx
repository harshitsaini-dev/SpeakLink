import React from "react";
import { useParams } from "react-router-dom";
import { Loader2, Volume2, AlertCircle } from "lucide-react";
import SpeakLinkMark from "@/components/SpeakLinkMark";
import { api, wsUrl } from "@/lib/api";
import {
  ListenerPlaybackState,
  WebListenerPlayer,
} from "@/lib/audio/WebListenerPlayer";

/**
 * The public SpeakLink listener.
 *
 * Deliberately the smallest page in the product. Someone opens a link on a
 * phone, types their name, and hears the announcement. There is no navigation,
 * because there is nowhere else for them to go, and nothing here names a Store,
 * a Zone, a Receiver or an operator - a listener is not an operator with fewer
 * buttons, they are a different kind of user entirely.
 *
 * There is no volume control either. That was asked for explicitly, and it is
 * also right: the device already has one, and a second slider that only attenuates
 * the stream would leave someone with the volume up wondering why it is quiet.
 */

const Phase = {
  //: The page starts here, not on the form. A refresh must not present
  //: credentials to somebody who is already admitted - the HttpOnly session
  //: cookie is still there, and the server is the authority on whether it is
  //: valid. Starting on the form and discovering the session afterwards also
  //: flashes a password box at every returning listener.
  BOOTSTRAPPING: "BOOTSTRAPPING",
  FORM: "FORM",
  WAITING: "WAITING",
  DENIED: "DENIED",
  LIVE: "LIVE",
  KICKED: "KICKED",
  ENDED: "ENDED",
  //: This browser has no valid listener session. Distinct from ENDED, which
  //: means the Broadcast itself is over - conflating them told an approved
  //: listener their Broadcast had finished.
  LOST: "LOST",
  //: Admitted before the microphone opened. Waiting, not finished.
  WAITING_BROADCAST: "WAITING_BROADCAST",
};

//: How long playback may fail to progress before the listener is told.
//: Measured against the relay: one Cluster is 300 ms and the bootstrap is two
//: of them, so anything past a few seconds is a real failure rather than a
//: slow start. Buffering for ever while nothing happens is not a state, it is
//: a bug wearing one.
const NO_PROGRESS_TIMEOUT_MS = 8000;

//: Bounded exponential backoff with jitter, in the same spirit as the Receiver.
//: A listener whose Wi-Fi dropped should come back quickly; a hundred listeners
//: whose HQ restarted should not all arrive in the same millisecond.
const RECONNECT_BASE_MS = 800;
const RECONNECT_MAX_MS = 15_000;

function reconnectDelay(attempt) {
  const backoff = Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
  return backoff * (0.7 + Math.random() * 0.6);
}

export default function Listen() {
  const { publicCode } = useParams();

  const [code, setCode] = React.useState(publicCode || "");
  const [name, setName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [phase, setPhase] = React.useState(Phase.BOOTSTRAPPING);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState(null);
  const [playback, setPlayback] = React.useState(ListenerPlaybackState.CONNECTING);
  const [needsTap, setNeedsTap] = React.useState(false);
  const [broadcastLive, setBroadcastLive] = React.useState(true);

  const audioRef = React.useRef(null);
  const playerRef = React.useRef(null);
  const socketRef = React.useRef(null);
  const heartbeatRef = React.useRef(null);
  const attemptRef = React.useRef(0);
  const retryRef = React.useRef(null);
  const progressRef = React.useRef(null);
  //: The restore effect runs on mount, before connect/pollAdmission exist in
  //: this scope. Refs rather than reordering the whole component.
  const connectRef = React.useRef(() => {});
  const pollRef = React.useRef(() => {});
  const stoppedRef = React.useRef(false);
  //: The player reads this rather than `playback`, so the heartbeat always
  //: reports what the element is doing now and not what it was doing when the
  //: interval was created.
  const playbackRef = React.useRef(ListenerPlaybackState.CONNECTING);
  playbackRef.current = playback;

  const teardown = React.useCallback(() => {
    if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
    if (retryRef.current) { clearTimeout(retryRef.current); retryRef.current = null; }
    if (progressRef.current) { clearInterval(progressRef.current); progressRef.current = null; }
    if (socketRef.current) {
      const socket = socketRef.current;
      socketRef.current = null;
      try { socket.close(); } catch (ignored) { /* already closed */ }
    }
    if (playerRef.current) { playerRef.current.detach(); playerRef.current = null; }
  }, []);

  React.useEffect(() => {
    // Reset on mount, not only on unmount. React 18 mounts, tears down and
    // remounts in development, so a flag that is only ever SET by the cleanup
    // stays set - and the page could then never open its socket at all.
    stoppedRef.current = false;
    return () => { stoppedRef.current = true; teardown(); };
  }, [teardown]);

  // Ask the server who we are before drawing anything.
  //
  // A page refresh is not a new person: same browser, same cookie, same room
  // means the same participant. The cookie is HttpOnly, so the page cannot read
  // it - it simply calls the listener endpoint and the browser attaches it.
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        // Scoped to the Broadcast in the URL. Asking "what is my state" without
        // saying which room answered for whatever room this browser last
        // touched, so a listener kicked from one Broadcast opened a completely
        // different one and was told they had been removed from it.
        const { data } = await api.get("/listen/me", {
          params: publicCode ? { public_code: publicCode } : undefined,
        });
        if (cancelled) return;
        setBroadcastLive(!!data.broadcast_live);
        if (data.display_name) setName(data.display_name);
        if (data.public_code) setCode(data.public_code);

        if (data.admitted) {
          setPhase(Phase.LIVE);
          connectRef.current();
          return;
        }
        if (data.admission_status === "REQUESTED") {
          setPhase(Phase.WAITING);
          pollRef.current();
          return;
        }
        if (data.admission_status === "KICKED") { setPhase(Phase.KICKED); return; }
        if (data.admission_status === "DENIED") { setPhase(Phase.DENIED); return; }
        if (data.admission_status === "ROOM_ENDED") { setPhase(Phase.ENDED); return; }
        setPhase(Phase.FORM);
      } catch (failure) {
        // 401 simply means this browser has no session for any room yet, which
        // is the ordinary first visit.
        if (!cancelled) setPhase(Phase.FORM);
      }
    })();
    return () => { cancelled = true; };
    // Deliberately once, on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---- the live socket ---------------------------------------------------
  const connect = React.useCallback(() => {
    if (stoppedRef.current) return;
    const audio = audioRef.current;
    if (!audio) return;

    const player = new WebListenerPlayer(audio, {
      onState: (next) => {
        setPlayback(next);
        if (next === ListenerPlaybackState.LISTENING) setNeedsTap(false);
      },
      onError: (message) => setError(message),
    });
    playerRef.current = player;
    if (!player.attach()) return;

    // No token, no password and no identifier of any kind in this URL. The
    // listener session travels in an HttpOnly cookie the browser attaches
    // itself - a credential in a query string is a credential in a server log.
    // wsUrl() supplies the /api prefix itself.
    const socket = new WebSocket(wsUrl("/listen/ws"));
    socket.binaryType = "arraybuffer";
    socketRef.current = socket;

    socket.onopen = () => { attemptRef.current = 0; };

    socket.onmessage = async (event) => {
      if (typeof event.data !== "string") {
        player.pushCluster(event.data);
        return;
      }
      let message;
      try { message = JSON.parse(event.data); } catch (ignored) { return; }
      if (message.type === "bootstrap") {
        // The join click is the user gesture, so this normally starts. When the
        // browser refuses anyway the listener is asked for a tap, and the state
        // stays READY_TO_PLAY - never LISTENING.
        const started = await player.play();
        setNeedsTap(!started);
        startHeartbeat(message.heartbeat_seconds || 10);
      } else if (message.type === "refused") {
        // The server accepted the socket purely to say this. A refusal is not
        // a network problem and must not be retried in a loop.
        stoppedRef.current = true;
        setPhase(message.reason === "not_started" ? Phase.WAITING_BROADCAST
                                                  : Phase.LOST);
        teardown();
      } else if (message.type === "kicked") {
        stoppedRef.current = true;
        setPhase(Phase.KICKED);
        teardown();
      } else if (message.type === "room_ended") {
        stoppedRef.current = true;
        setPhase(Phase.ENDED);
        teardown();
      }
    };

    socket.onclose = (event) => {
      if (stoppedRef.current) return;
      if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
      if (progressRef.current) { clearInterval(progressRef.current); progressRef.current = null; }
      // A refusal already set the phase and stopped us; anything else here is
      // a genuine disconnect. The server now ACCEPTS before refusing, so a
      // refusal arrives as a message rather than as a close code - a close
      // before the handshake completes only ever reaches a browser as 1006,
      // which is indistinguishable from a dropped network.
      if (event.code === 4401) {
        stoppedRef.current = true;
        setPhase(Phase.LOST);
        return;
      }
      setPlayback(ListenerPlaybackState.BUFFERING);
      const attempt = attemptRef.current;
      attemptRef.current = attempt + 1;
      retryRef.current = setTimeout(() => {
        if (playerRef.current) { playerRef.current.detach(); playerRef.current = null; }
        connect();                       // a reconnect is a fresh bootstrap
      }, reconnectDelay(attempt));
    };

    // Watchdog: connected, but is anything actually playing?
    if (progressRef.current) clearInterval(progressRef.current);
    let lastTime = -1;
    let stuckSince = Date.now();
    progressRef.current = setInterval(() => {
      const element = audioRef.current;
      if (!element || stoppedRef.current) return;
      if (element.paused) { stuckSince = Date.now(); return; }
      if (element.currentTime > lastTime) {
        lastTime = element.currentTime;
        stuckSince = Date.now();
        return;
      }
      if (Date.now() - stuckSince < NO_PROGRESS_TIMEOUT_MS) return;
      // Connected and receiving, yet the clock has not moved. Say so, and try
      // again from a fresh bootstrap rather than sitting on Buffering.
      setError("Unable to start live audio. Reconnecting…");
      stuckSince = Date.now();
      try { socket.close(); } catch (ignored) { /* already closing */ }
    }, 1000);

    function startHeartbeat(seconds) {
      if (heartbeatRef.current) clearInterval(heartbeatRef.current);
      heartbeatRef.current = setInterval(() => {
        if (socket.readyState !== WebSocket.OPEN) return;
        socket.send(JSON.stringify({
          type: "heartbeat", playback_state: playbackRef.current,
        }));
      }, Math.max(5, seconds) * 1000);
    }
  }, [teardown]);

  // ---- joining -----------------------------------------------------------
  const submit = React.useCallback(async (mode) => {
    setError(null);
    const trimmed = name.trim();
    if (!trimmed) { setError("Please enter your name."); return; }
    const room = (code || "").trim();
    if (!room) { setError("Please enter the Broadcast ID."); return; }

    setBusy(true);
    try {
      const path = mode === "request"
        ? `/listen/rooms/${encodeURIComponent(room)}/request-access`
        : `/listen/rooms/${encodeURIComponent(room)}/join`;
      const body = mode === "request"
        ? { display_name: trimmed }
        : { display_name: trimmed, password };
      const { data } = await api.post(path, body);

      setBroadcastLive(!!data.broadcast_live);
      if (data.admitted) {
        setPhase(Phase.LIVE);
        connect();
      } else {
        setPhase(Phase.WAITING);
        pollAdmission();
      }
    } catch (failure) {
      const status = failure?.response?.status;
      if (status === 401) {
        setError("Incorrect password.");
      } else if (status === 404) {
        setError("No live Broadcast with that ID.");
      } else if (status === 429) {
        setError("Too many attempts. Please wait a moment and try again.");
      } else {
        setError(failure?.response?.data?.detail || "Could not join this Broadcast.");
      }
    } finally {
      setBusy(false);
    }
  }, [code, name, password, connect]);

  // ---- starting over after a removal or a denial --------------------------
  // Discards the spent session and returns to the join form. It admits nobody
  // and requests nothing: the listener still has to supply the current
  // password or ask again, and the broadcaster still decides. Without an
  // explicit action here a Kick or a Deny would either be permanent or undo
  // itself, and neither is what either of them means.
  //
  // One function for both, because both are the same thing: an attempt that is
  // over, and a listener who may make another one.
  const startOver = React.useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post("/listen/forget");
    } catch (ignored) {
      // The cookies are being discarded either way; a failure here only means
      // the server did not get to clear them, and the form still works.
    } finally {
      teardown();
      // The Kick set the permanent stop flag, which is what stops the page
      // reconnecting on its own. Choosing to come back has to clear it, or
      // connect() returns immediately and the page shows a stale Listening
      // over a socket it never opened. The playback state is reset for the
      // same reason: it is the LAST session's, and this is a new one.
      stoppedRef.current = false;
      setPlayback(ListenerPlaybackState.CONNECTING);
      setNeedsTap(false);
      setPassword("");
      setPhase(Phase.FORM);
      setBusy(false);
    }
  }, [teardown]);

  // While waiting, the browser asks about ITSELF - never about the room's
  // participants - so an Approve reaches it without anybody refreshing.
  const pollAdmission = React.useCallback(() => {
    if (retryRef.current) clearTimeout(retryRef.current);
    const tick = async () => {
      if (stoppedRef.current) return;
      try {
        // Scoped, exactly like the bootstrap. Unscoped, this asked "what is my
        // state" with no room attached and took whatever came back - so a
        // leftover session or claim from a DIFFERENT Broadcast could answer,
        // and this page would show that other room's denial, removal or
        // admission as though it were this one's.
        const room = (code || "").trim();
        const { data } = await api.get("/listen/me", {
          params: room ? { public_code: room } : undefined,
        });
        setBroadcastLive(!!data.broadcast_live);
        if (data.admitted) { setPhase(Phase.LIVE); connect(); return; }
        if (data.admission_status === "DENIED") { setPhase(Phase.DENIED); return; }
        if (data.admission_status === "KICKED") { setPhase(Phase.KICKED); return; }
        if (data.admission_status === "ROOM_ENDED") { setPhase(Phase.ENDED); return; }
      } catch (failure) {
        if (failure?.response?.status === 401) {
          // NOT "the Broadcast ended". 401 here means this browser has no
          // valid listener session - which is what happened when the cookie
          // was rejected - and telling somebody their Broadcast finished the
          // moment they were approved is the worst possible reading of it.
          setPhase(Phase.LOST);
          return;
        }
      }
      retryRef.current = setTimeout(tick, 2500);
    };
    retryRef.current = setTimeout(tick, 1500);
  }, [connect, code]);

  // Kept current so the mount-time restore can call them without the whole
  // component having to be reordered around it.
  connectRef.current = connect;
  pollRef.current = pollAdmission;

  const tapToStart = React.useCallback(async () => {
    if (!playerRef.current) return;
    const started = await playerRef.current.play();
    setNeedsTap(!started);
  }, []);

  // ---- rendering ---------------------------------------------------------
  const statusLabel = needsTap ? "Tap to Start Listening"
    : playback === ListenerPlaybackState.LISTENING ? "Listening"
    : playback === ListenerPlaybackState.BUFFERING ? "Buffering…"
    : playback === ListenerPlaybackState.PAUSED ? "Paused"
    : playback === ListenerPlaybackState.ERROR ? "Playback error"
    : "Connecting…";

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center justify-center gap-2 mb-6">
          <SpeakLinkMark size={34} className="text-blue-500" />
          <h1 className="text-lg font-bold tracking-[0.2em] uppercase">SpeakLink</h1>
        </div>

        {/* Hidden: the listener controls playback through this page, and their
            device controls the volume. No native player chrome. */}
        <audio ref={audioRef} className="hidden" data-testid="listener-audio" />

        {phase === Phase.BOOTSTRAPPING && (
          <Panel testId="listen-bootstrapping">
            <Loader2 size={28} className="mx-auto mb-3 animate-spin text-slate-400" />
            <p className="text-sm text-slate-400">Checking your access…</p>
          </Panel>
        )}

        {phase === Phase.FORM && (
          <form
            className="rounded-lg border border-slate-800 bg-slate-900 p-5 space-y-4"
            onSubmit={(event) => { event.preventDefault(); submit("password"); }}
          >
            <Field label="Broadcast ID">
              <input
                data-testid="listen-code"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                placeholder="SL-7K4P92"
                autoComplete="off"
                className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm uppercase tracking-widest focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </Field>
            <Field label="Your Name">
              <input
                data-testid="listen-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Harshit"
                maxLength={40}
                className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </Field>
            <Field label="Password">
              <input
                data-testid="listen-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="off"
                className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </Field>

            {error && (
              <p data-testid="listen-error"
                 className="flex items-start gap-2 text-sm text-red-400">
                <AlertCircle size={14} className="mt-0.5 shrink-0" />
                <span>{error}</span>
              </p>
            )}

            <button
              type="submit"
              data-testid="listen-join"
              disabled={busy}
              className="w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-60"
            >
              {busy ? "Joining…" : "Join Broadcast"}
            </button>

            <div className="pt-1 text-center">
              <p className="text-xs text-slate-500 mb-2">Don&rsquo;t have the password?</p>
              <button
                type="button"
                data-testid="listen-request"
                disabled={busy}
                onClick={() => submit("request")}
                className="w-full rounded-md border border-slate-700 px-4 py-2 text-sm font-semibold text-slate-200 hover:bg-slate-800 disabled:opacity-60"
              >
                Request Access
              </button>
            </div>
          </form>
        )}

        {phase === Phase.WAITING && (
          <Panel testId="listen-waiting">
            <Loader2 size={28} className="mx-auto mb-3 animate-spin text-slate-400" />
            <p className="font-semibold">Waiting for broadcaster approval…</p>
            <p className="mt-1 text-sm text-slate-400">
              You&rsquo;ll start listening automatically once you&rsquo;re let in.
            </p>
          </Panel>
        )}

        {phase === Phase.DENIED && (
          <Panel testId="listen-denied">
            <p className="font-semibold">Request denied</p>
            <p className="mt-1 text-sm text-slate-400">
              The broadcaster did not admit you to this Broadcast.
            </p>
            {/* A denial ends one attempt, not the listener's day. The button
                returns them to the form; it does not resend the request, so
                the broadcaster is never asked twice by a page nobody
                touched. */}
            <button
              type="button"
              data-testid="listen-request-again"
              onClick={startOver}
              disabled={busy}
              className="mt-4 rounded-lg bg-white px-4 py-2 text-sm font-semibold
                         text-slate-900 disabled:opacity-60">
              Request again
            </button>
          </Panel>
        )}

        {phase === Phase.KICKED && (
          <Panel testId="listen-kicked">
            <p className="font-semibold">You were removed from this Broadcast.</p>
            <p className="mt-1 text-sm text-slate-400">
              You can ask to join again. The broadcaster decides.
            </p>
            {/* Deliberately a button and not an automatic retry. Kick has to
                terminate the current admission, so returning has to be
                something the listener chooses - and it returns them to the
                join form, not to the Broadcast. */}
            <button
              type="button"
              data-testid="listen-join-again"
              onClick={startOver}
              disabled={busy}
              className="mt-4 rounded-lg bg-white px-4 py-2 text-sm font-semibold
                         text-slate-900 disabled:opacity-60">
              Join again
            </button>
          </Panel>
        )}

        {phase === Phase.WAITING_BROADCAST && (
          <Panel testId="listen-not-started-yet">
            <Loader2 size={28} className="mx-auto mb-3 animate-spin text-slate-400" />
            <p className="font-semibold">The Broadcast hasn&rsquo;t started yet</p>
            <p className="mt-1 text-sm text-slate-400">
              You&rsquo;re admitted. Audio will begin when the broadcaster goes live.
            </p>
          </Panel>
        )}

        {phase === Phase.LOST && (
          <Panel testId="listen-session-lost">
            <p className="font-semibold">You&rsquo;re not connected to this Broadcast</p>
            <p className="mt-1 text-sm text-slate-400">
              Your access may have been removed, or this browser is blocking the
              session. Try joining again.
            </p>
          </Panel>
        )}

        {phase === Phase.ENDED && (
          <Panel testId="listen-ended">
            <p className="font-semibold">Broadcast ended</p>
            <p className="mt-1 text-sm text-slate-400">Thanks for listening.</p>
          </Panel>
        )}

        {phase === Phase.LIVE && (
          <div data-testid="listen-live"
               className="rounded-lg border border-slate-800 bg-slate-900 p-6 text-center">
            <div className="mb-4 flex items-center justify-center gap-2">
              <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-red-500" />
              <span className="text-sm font-bold tracking-[0.2em] text-red-400">LIVE</span>
            </div>

            <p className="text-xs uppercase tracking-widest text-slate-500">Broadcast</p>
            <p data-testid="listen-room-code"
               className="font-mono text-lg font-bold">{(code || "").toUpperCase()}</p>

            <p className="mt-4 text-xs uppercase tracking-widest text-slate-500">You</p>
            <p data-testid="listen-display-name" className="font-semibold">{name.trim()}</p>

            <div className="mt-5 flex items-center justify-center gap-2 text-sm">
              <Volume2 size={15} className="text-slate-400" />
              <span data-testid="listen-status" className="font-semibold">{statusLabel}</span>
            </div>

            {!broadcastLive && (
              <p className="mt-3 text-sm text-slate-400" data-testid="listen-not-started">
                The Broadcast hasn&rsquo;t started yet.
              </p>
            )}

            {needsTap && (
              <button
                data-testid="listen-tap-to-start"
                onClick={tapToStart}
                className="mt-4 w-full rounded-md bg-blue-600 px-4 py-3 text-sm font-semibold text-white hover:bg-blue-700"
              >
                Tap to Start Listening
              </button>
            )}

            {error && (
              <p data-testid="listen-live-error" className="mt-4 text-sm text-red-400">{error}</p>
            )}

            <p className="mt-5 text-xs text-slate-600">
              Use your device&rsquo;s volume buttons to adjust how loud this is.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-bold uppercase tracking-[0.1em] text-slate-500">
        {label}
      </span>
      {children}
    </label>
  );
}

function Panel({ testId, children }) {
  return (
    <div data-testid={testId}
         className="rounded-lg border border-slate-800 bg-slate-900 p-6 text-center">
      {children}
    </div>
  );
}
