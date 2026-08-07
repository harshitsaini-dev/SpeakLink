import React from "react";
import { useParams } from "react-router-dom";
import { Radio, Loader2, Volume2, AlertCircle } from "lucide-react";
import { api, wsUrl } from "@/lib/api";
import {
  ListenerPlaybackState,
  WebListenerPlayer,
} from "@/lib/audio/WebListenerPlayer";

/**
 * The public EchoCast listener.
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
  FORM: "FORM",
  WAITING: "WAITING",
  DENIED: "DENIED",
  LIVE: "LIVE",
  KICKED: "KICKED",
  ENDED: "ENDED",
};

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
  const [phase, setPhase] = React.useState(Phase.FORM);
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
  const stoppedRef = React.useRef(false);
  //: The player reads this rather than `playback`, so the heartbeat always
  //: reports what the element is doing now and not what it was doing when the
  //: interval was created.
  const playbackRef = React.useRef(ListenerPlaybackState.CONNECTING);
  playbackRef.current = playback;

  const teardown = React.useCallback(() => {
    if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
    if (retryRef.current) { clearTimeout(retryRef.current); retryRef.current = null; }
    if (socketRef.current) {
      const socket = socketRef.current;
      socketRef.current = null;
      try { socket.close(); } catch (ignored) { /* already closed */ }
    }
    if (playerRef.current) { playerRef.current.detach(); playerRef.current = null; }
  }, []);

  React.useEffect(() => () => { stoppedRef.current = true; teardown(); }, [teardown]);

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
    const socket = new WebSocket(wsUrl("/api/listen/ws"));
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
      // 4401 means the session is no longer valid - kicked, denied or the room
      // has ended. Retrying that would be a loop that can never succeed.
      if (event.code === 4401) {
        stoppedRef.current = true;
        setPhase(Phase.ENDED);
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

  // While waiting, the browser asks about ITSELF - never about the room's
  // participants - so an Approve reaches it without anybody refreshing.
  const pollAdmission = React.useCallback(() => {
    if (retryRef.current) clearTimeout(retryRef.current);
    const tick = async () => {
      if (stoppedRef.current) return;
      try {
        const { data } = await api.get("/listen/me");
        setBroadcastLive(!!data.broadcast_live);
        if (data.admitted) { setPhase(Phase.LIVE); connect(); return; }
        if (data.admission_status === "DENIED") { setPhase(Phase.DENIED); return; }
        if (data.admission_status === "KICKED") { setPhase(Phase.KICKED); return; }
        if (data.admission_status === "ROOM_ENDED") { setPhase(Phase.ENDED); return; }
      } catch (failure) {
        if (failure?.response?.status === 401) { setPhase(Phase.ENDED); return; }
      }
      retryRef.current = setTimeout(tick, 2500);
    };
    retryRef.current = setTimeout(tick, 1500);
  }, [connect]);

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
          <Radio size={20} className="text-red-500" />
          <h1 className="text-lg font-bold tracking-[0.2em] uppercase">EchoCast Live</h1>
        </div>

        {/* Hidden: the listener controls playback through this page, and their
            device controls the volume. No native player chrome. */}
        <audio ref={audioRef} className="hidden" data-testid="listener-audio" />

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
                placeholder="EC-7K4P92"
                autoComplete="off"
                className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm uppercase tracking-widest focus:outline-none focus:ring-2 focus:ring-red-500"
              />
            </Field>
            <Field label="Your Name">
              <input
                data-testid="listen-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Harshit"
                maxLength={40}
                className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-500"
              />
            </Field>
            <Field label="Password">
              <input
                data-testid="listen-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="off"
                className="w-full rounded-md border border-slate-700 bg-slate-950 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-red-500"
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
              className="w-full rounded-md bg-red-600 px-4 py-2 text-sm font-semibold text-white hover:bg-red-700 disabled:opacity-60"
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
          </Panel>
        )}

        {phase === Phase.KICKED && (
          <Panel testId="listen-kicked">
            <p className="font-semibold">You were removed from this Broadcast.</p>
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
                className="mt-4 w-full rounded-md bg-red-600 px-4 py-3 text-sm font-semibold text-white hover:bg-red-700"
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
