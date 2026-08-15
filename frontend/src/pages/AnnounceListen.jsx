import React from "react";
import { api } from "@/lib/api";
import { Volume2, LogOut } from "lucide-react";
import SpeakLinkMark from "@/components/SpeakLinkMark";
import ThemeToggle from "@/components/ThemeToggle";

/**
 * Listening to a recorded announcement through a shared link.
 *
 * WHAT THIS PAGE PROMISES, AND WHAT IT DOES NOT
 *
 * It plays the recording in this browser, from its own beginning. It is NOT a
 * mirror of what a particular shop's speaker is doing at this instant: two
 * people opening the link a minute apart are a minute apart in the audio, and
 * saying otherwise would be inventing a sync this design cannot deliver.
 *
 * What it DOES follow is whether the announcement is running at all. When HQ
 * pauses the campaign this page stops - because a link that kept playing
 * something HQ had stopped is the one failure that would embarrass somebody in
 * front of a customer.
 *
 * NOBODY HERE HAS AN ACCOUNT
 *
 * Whoever holds the link is not a user of this product and must never need to
 * be. Every message on this page is written for somebody who has never seen
 * the rest of it.
 */
export default function AnnounceListen() {
  const params = new URLSearchParams(window.location.search);
  const [id, setId] = React.useState(params.get("id") || "");
  // A link may carry its own password. Whoever HQ gave it to is one click
  // from listening, which is the point of that kind of link - and also its
  // risk, which is why HQ has to choose it per link rather than get it by
  // default.
  const [password, setPassword] = React.useState(params.get("k") || "");
  const [name, setName] = React.useState("");
  const [token, setToken] = React.useState(
    () => sessionStorage.getItem("speaklink.announce.token") || "");
  const [state, setState] = React.useState(null);
  const [error, setError] = React.useState("");
  // A leftover token from a previous visit, being reported over the top of a
  // link the person has just opened.
  //
  // Somebody arriving at /announce with nothing has to be told why they are
  // looking at a form: their old session ended. Somebody arriving by a fresh
  // link is about to be admitted on it, and "this listening link is no longer
  // open" over that is a sentence about a DIFFERENT link - which is exactly
  // what made it look broken at login. So the message is suppressed only
  // where a live link is in hand.
  const arrivedByLink = React.useRef(Boolean(params.get("id")));
  const inherited = React.useRef(Boolean(token));
  const [busy, setBusy] = React.useState(false);
  // The browser will not start audio until somebody has interacted with the
  // page. Rather than failing silently - a page that looks like it is playing
  // and is not - the button says what it is for.
  const [started, setStarted] = React.useState(false);
  const audioRef = React.useRef(null);

  const authHeader = React.useMemo(
    () => (token ? { Authorization: `Bearer ${token}` } : {}), [token]);

  const poll = React.useCallback(async () => {
    if (!token) return;
    try {
      const { data } = await api.get("/announce/state", { headers: authHeader });
      setState(data);
      setError("");
    } catch (failure) {
      if (failure?.response?.status === 401) {
        // The link was closed, or the campaign ended. Said plainly, and the
        // token dropped so a reload does not look like a bug - unless this
        // was somebody else's leftover token from a previous visit, which is
        // nothing to report.
        const leftover = inherited.current && arrivedByLink.current;
        inherited.current = false;
        setToken("");
        sessionStorage.removeItem("speaklink.announce.token");
        if (!leftover) {
          setError(failure?.response?.data?.detail
                   || "This listening link is no longer open.");
        }
      }
    }
  }, [token, authHeader]);

  React.useEffect(() => {
    if (!token) return undefined;
    poll();
    // A poll rather than a socket: the state changes when somebody at HQ
    // presses play or pause - minutes apart, not milliseconds - and a socket
    // per listener would hold a connection open for a sentence every few
    // seconds.
    const timer = setInterval(poll, 5000);
    return () => clearInterval(timer);
  }, [token, poll]);

  // Follow the campaign. Pausing here is what makes this a link to the
  // announcement rather than a link to a file.
  React.useEffect(() => {
    const element = audioRef.current;
    if (!element || !started) return;
    if (state?.playing) {
      element.play().catch(() => { /* the browser will say why on the button */ });
    } else {
      element.pause();
    }
  }, [state?.playing, started]);

  const attemptJoin = React.useCallback(async (code, secret, who) => {
    setBusy(true);
    setError("");
    try {
      const { data } = await api.post("/announce/join",
                                      { id: code, password: secret, name: who });
      inherited.current = false;
      setToken(data.token);
      sessionStorage.setItem("speaklink.announce.token", data.token);
      return true;
    } catch (failure) {
      setError(failure?.response?.data?.detail
               || "That listening ID or password is not right.");
      return false;
    } finally {
      setBusy(false);
    }
  }, []);

  // A link that carries its ID and its password fills them in and stops
  // there.
  //
  // It used to walk straight in. That was right until the name became
  // required, and then it was a contradiction: HQ can see who is on a link
  // and can throw somebody off it, and a link that admitted people
  // anonymously made both of those useless. So the link still removes the
  // retyping it was meant to remove - the ID and the password are already in
  // the boxes - and the one thing it cannot know is the one thing it asks
  // for.
  //
  // (The fields are seeded from the URL where they are declared, above.)

  async function join(event) {
    event.preventDefault();
    await attemptJoin(id, password, name);
  }

  async function leave() {
    try { await api.post("/announce/leave", {}, { headers: authHeader }); }
    finally {
      setToken("");
      sessionStorage.removeItem("speaklink.announce.token");
      setState(null);
      setStarted(false);
    }
  }

  if (!token) {
    return (
      <div className="min-h-screen flex items-center justify-center p-6"
           data-testid="announce-join-page">
        <div className="absolute right-4 top-4"><ThemeToggle compact /></div>
        <form onSubmit={join}
              className="glass w-full max-w-sm rounded-xl p-6 space-y-4">
          <div className="flex items-center gap-2">
            <SpeakLinkMark className="text-blue-600" size={28} />
            <div>
              <div className="font-bold text-strong">SpeakLink</div>
              <div className="text-xs text-muted">Listen to an announcement</div>
            </div>
          </div>

          <label className="block">
            <span className="text-xs uppercase tracking-widest text-muted">
              Listening ID
            </span>
            <input value={id} onChange={(event) => setId(event.target.value)}
                   data-testid="announce-id" required placeholder="AN-XXXXXX"
                   className="mt-1 w-full px-3 py-2 border border-line-strong rounded-md font-mono" />
          </label>
          <label className="block">
            <span className="text-xs uppercase tracking-widest text-muted">
              Password
            </span>
            <input value={password} type="password" required
                   onChange={(event) => setPassword(event.target.value)}
                   data-testid="announce-password"
                   className="mt-1 w-full px-3 py-2 border border-line-strong rounded-md font-mono" />
          </label>
          <label className="block">
            <span className="text-xs uppercase tracking-widest text-muted">
              Your name
            </span>
            {/* Required, not optional.
                HQ can see who is on a link and can throw somebody off it, and
                both of those are useless against a list of anonymous rows. A
                name typed by the person is worth exactly what a self-declared
                name is worth - which is why the times sit beside it on the HQ
                card - but "somebody" is worth nothing at all. */}
            <input value={name} onChange={(event) => setName(event.target.value)}
                   data-testid="announce-name" required minLength={2}
                   placeholder="so HQ knows who is listening"
                   className="mt-1 w-full px-3 py-2 border border-line-strong rounded-md" />
          </label>

          {error && (
            <p className="text-sm text-rose-700" data-testid="announce-error">{error}</p>
          )}

          <button type="submit" disabled={busy} data-testid="announce-join"
                  className="w-full px-3 py-2 rounded-md text-white bg-blue-700 hover:bg-blue-800 disabled:opacity-50">
            {busy ? "Checking…" : "Listen"}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-6"
         data-testid="announce-listen-page">
      <div className="absolute right-4 top-4"><ThemeToggle compact /></div>
      <div className="glass w-full max-w-md rounded-xl p-6 space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="font-bold text-strong" data-testid="announce-label">
              {state?.label || state?.template_name || "Announcement"}
            </div>
            <div className="text-xs text-muted">{state?.window}</div>
          </div>
          <button onClick={leave} data-testid="announce-leave"
                  className="row-action">
            <LogOut size={14} /> Leave
          </button>
        </div>

        {error && (
          <p className="text-sm text-rose-700" data-testid="announce-error">{error}</p>
        )}

        {state?.audio ? (
          <>
            <audio ref={audioRef} src={`${state.audio.url}?token=${token}`}
                   loop data-testid="announce-audio" />
            {!started ? (
              // Browsers refuse to start audio before an interaction. Saying
              // so is better than a page that looks like it is playing.
              <button onClick={() => setStarted(true)} data-testid="announce-start"
                      className="w-full px-3 py-3 rounded-md text-white bg-blue-700 hover:bg-blue-800">
                <Volume2 className="inline w-4 h-4 mr-1" /> Start listening
              </button>
            ) : (
              <div className="rounded-md border border-line px-3 py-3 text-sm"
                   data-testid="announce-status">
                {state.playing
                  ? <span className="text-emerald-700">Playing now</span>
                  : <span className="text-amber-700">
                      {state.reason || "Paused right now."}
                    </span>}
              </div>
            )}
          </>
        ) : (
          <p className="text-sm text-body" data-testid="announce-nothing">
            {state?.reason || "There is nothing to play on this link yet."}
          </p>
        )}

        {/* Said plainly, because somebody will otherwise assume it. */}
        <p className="text-xs text-muted">
          This plays the recording in your browser, from its beginning. It is
          not a live feed of a particular shop's speaker - two people opening
          this link a minute apart will be a minute apart in the audio.
        </p>
      </div>
    </div>
  );
}
