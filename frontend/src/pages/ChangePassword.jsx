/**
 * Change your own password.
 *
 * The current password is required. Without it, an unattended signed-in desktop
 * is a permanent account takeover rather than a session somebody can end.
 *
 * Succeeding signs you out - including the token that made the request - because
 * "change your password, you may have been compromised" achieves nothing if the
 * old sessions keep working for the next eight hours. The page says so before
 * you press the button rather than after.
 *
 * Nothing typed here reaches a URL, localStorage, the console or the DOM beyond
 * the password fields themselves.
 */
import React from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import { KeyRound } from "lucide-react";

export default function ChangePassword() {
  const navigate = useNavigate();
  const { logout } = useAuth();
  const [current, setCurrent] = React.useState("");
  const [next, setNext] = React.useState("");
  const [repeat, setRepeat] = React.useState("");
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const tooShort = next.length > 0 && next.length < 8;
  const mismatched = repeat.length > 0 && next !== repeat;

  async function submit(event) {
    event.preventDefault();
    setError("");
    if (next !== repeat) {
      setError("The two new passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await api.post("/auth/change-password", {
        current_password: current, new_password: next,
      });
      // logout(), not clearToken(). Clearing the stored token alone leaves the
      // auth context still holding a user, so the protected-route guard sends
      // /login straight back to /console and the operator never sees the
      // sign-in page - looking, from their side, as though nothing happened.
      logout();
      navigate("/login", { replace: true });
    } catch (failure) {
      const status = failure?.response?.status;
      setError(status === 403
        ? "That is not your current password."
        : failure?.response?.data?.detail || "The password could not be changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-6" data-testid="change-password-page">
      <h1 className="mb-1 text-2xl font-semibold">Change your password</h1>
      <p className="mb-4 text-sm text-slate-500">
        You will be signed out on every device once this succeeds, and will need to sign in again.
      </p>

      {error && (
        <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
             role="alert" data-testid="change-password-error">
          {error}
        </div>
      )}

      <form onSubmit={submit} className="max-w-sm space-y-3" data-testid="change-password-form">
        <label className="block text-sm">
          <span className="mb-1 block text-slate-600">Current password</span>
          <input type="password" className="w-full rounded border px-2 py-1" required
                 value={current} onChange={(event) => setCurrent(event.target.value)}
                 autoComplete="current-password" data-testid="current-password" />
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-slate-600">New password (at least 8 characters)</span>
          <input type="password" className="w-full rounded border px-2 py-1" required minLength={8}
                 value={next} onChange={(event) => setNext(event.target.value)}
                 autoComplete="new-password" data-testid="new-password" />
          {tooShort && (
            <span className="mt-1 block text-xs text-amber-700" data-testid="too-short">
              A little longer. Length protects you here far more than punctuation does.
            </span>
          )}
        </label>
        <label className="block text-sm">
          <span className="mb-1 block text-slate-600">New password again</span>
          <input type="password" className="w-full rounded border px-2 py-1" required
                 value={repeat} onChange={(event) => setRepeat(event.target.value)}
                 autoComplete="new-password" data-testid="repeat-password" />
          {mismatched && (
            <span className="mt-1 block text-xs text-amber-700" data-testid="mismatch">
              These two do not match.
            </span>
          )}
        </label>
        <button type="submit" disabled={busy || tooShort || mismatched}
                className="inline-flex items-center gap-2 rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-50"
                data-testid="change-password-submit">
          <KeyRound size={16} /> Change password and sign out
        </button>
      </form>
    </div>
  );
}
