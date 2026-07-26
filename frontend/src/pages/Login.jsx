import React from "react";
import { useNavigate } from "react-router-dom";
import { Radio, LogIn } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

const BG_IMAGE =
  "https://images.unsplash.com/photo-1621947081720-86970823b77a?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NDk1NzZ8MHwxfHNlYXJjaHwxfHxzb3VuZCUyMHdhdmUlMjBhYnN0cmFjdCUyMGJhY2tncm91bmR8ZW58MHx8fHwxNzgzNDIzMzQ1fDA&ixlib=rb-4.1.0&q=85";

export default function Login() {
  const nav = useNavigate();
  const { user, login } = useAuth();
  // Both fields start empty on purpose. A pre-filled credential is a credential
  // the operator never has to know, so it never gets changed from the default -
  // and anyone who reaches this page can read it straight off the form.
  const [u, setU] = React.useState("");
  const [p, setP] = React.useState("");
  const [err, setErr] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => { if (user) nav("/console"); }, [user, nav]);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      await login(u.trim(), p);
      nav("/console");
    } catch (e2) {
      // 429 covers both "too fast" and "this account is temporarily locked".
      // The backend deliberately answers the same way for both, because saying
      // which one applied would say whether the account exists. The wording is
      // fixed here rather than echoed, so no future server detail can leak a
      // count, a threshold or an unlock time into the page.
      if (e2?.response?.status === 429) {
        setErr("Too many sign-in attempts. Please wait a while and try again.");
      } else {
        setErr(e2?.response?.data?.detail || "Login failed");
      }
    } finally { setBusy(false); }
  };

  return (
    <div className="min-h-screen relative flex items-center justify-center p-6">
      <div className="absolute inset-0 bg-slate-900">
        <img src={BG_IMAGE} className="absolute inset-0 w-full h-full object-cover opacity-40" alt="" />
        <div className="absolute inset-0 bg-gradient-to-br from-slate-900/90 via-slate-900/80 to-blue-900/70" />
      </div>
      <div className="relative z-10 w-full max-w-md">
        <div className="mb-8 text-center">
          <div className="inline-flex items-center gap-3 text-white">
            <div className="p-3 bg-red-600 rounded-md shadow-lg"><Radio size={26} /></div>
            <div className="text-left">
              <div className="text-3xl font-bold tracking-tight">SpeakLink</div>
              <div className="text-xs uppercase tracking-[0.2em] text-slate-300 mt-1">HQ → Store Live Broadcast</div>
            </div>
          </div>
        </div>

        <form onSubmit={submit} className="bg-white rounded-md shadow-xl border border-slate-200 p-6 space-y-4">
          <div>
            <h2 className="text-xl font-semibold text-slate-900">HQ Admin Sign In</h2>
            <p className="text-sm text-slate-500 mt-1">Enter your credentials to access the broadcast console.</p>
          </div>

          <div>
            <label htmlFor="login-username" className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">Username</label>
            <input
              id="login-username"
              name="username"
              data-testid="login-username-input"
              autoComplete="username"
              className="w-full px-3 py-2.5 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              value={u} onChange={(e) => setU(e.target.value)} required autoFocus
            />
          </div>
          <div>
            <label htmlFor="login-password" className="block text-xs font-bold uppercase tracking-[0.1em] text-slate-500 mb-1.5">Password</label>
            <input
              id="login-password"
              name="password"
              data-testid="login-password-input"
              type="password"
              autoComplete="current-password"
              className="w-full px-3 py-2.5 border border-slate-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              value={p} onChange={(e) => setP(e.target.value)} required
            />
          </div>

          {err && <div data-testid="login-error" className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-md px-3 py-2">{err}</div>}

          <button
            data-testid="login-submit-btn"
            disabled={busy}
            className="w-full bg-blue-700 hover:bg-blue-800 disabled:bg-slate-400 text-white font-medium py-2.5 rounded-md flex items-center justify-center gap-2 transition-colors"
          >
            <LogIn size={16} /> {busy ? "Signing in…" : "Sign In"}
          </button>

          <p className="text-[11px] text-slate-500 text-center pt-2 border-t border-slate-100">
            Use the HQ credentials issued to you. If you do not have them, ask
            your SpeakLink administrator — they are never shown on this page.
          </p>
        </form>
      </div>
    </div>
  );
}
