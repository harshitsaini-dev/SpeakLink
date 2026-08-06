import React from "react";
import { Link } from "react-router-dom";
import { Volume2 } from "lucide-react";
import { api } from "@/lib/api";

/**
 * A compact "how loud is the estate" card for the Console.
 *
 * Deliberately NOT the control surface. It answers one question at a glance -
 * is anything obviously wrong - and sends the operator to the Master Volume
 * page for anything more.
 *
 * The counts are careful about what they mean. Muted and low-volume are
 * counted from LIVE Stores only: a shop that was left muted before it was
 * switched off says nothing about what it is doing now, and folding those in
 * would produce a number that drifts quietly away from reality as machines go
 * off overnight.
 */
export default function StoreAudioSummary() {
  const [summary, setSummary] = React.useState(null);
  const [denied, setDenied] = React.useState(false);

  React.useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const response = await api.get("/store-audio/master/summary");
        if (!cancelled) { setSummary(response.data); setDenied(false); }
      } catch (failure) {
        // A 403 is a normal answer for an account without the permission, not
        // an error worth shouting about. The card simply does not appear.
        if (!cancelled && failure?.response?.status === 403) setDenied(true);
      }
    };

    load();
    const timer = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  if (denied || !summary) return null;

  return (
    <section
      data-testid="store-audio-summary"
      className="rounded-lg border border-slate-200 bg-white p-4 space-y-3"
    >
      <header className="flex items-center gap-2">
        <Volume2 size={16} className="text-slate-500" />
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-600">
          Store Audio
        </h2>
      </header>

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        <dt className="text-slate-500">Online Receivers</dt>
        <dd data-testid="summary-online" className="text-right font-medium">
          {summary.online}
        </dd>

        <dt className="text-slate-500">Offline Receivers</dt>
        <dd data-testid="summary-offline" className="text-right font-medium">
          {summary.offline}
        </dd>

        {/* Named for what it is. "Muted: 4" would invite reading it as the
            whole estate rather than the part we can currently see. */}
        <dt className="text-slate-500">Muted (online)</dt>
        <dd data-testid="summary-muted" className="text-right font-medium">
          {summary.muted_online}
        </dd>

        <dt className="text-slate-500">Low volume (online)</dt>
        <dd data-testid="summary-low" className="text-right font-medium">
          {summary.low_volume_online}
        </dd>

        {/* Sync counts describe HQ's INTENTION against reality, which is a
            different question from how loud anything is right now. */}
        <dt className="text-slate-500">Synced</dt>
        <dd data-testid="summary-synced" className="text-right font-medium">
          {summary.synced}
        </dd>

        <dt className="text-slate-500">Waiting for sync</dt>
        <dd data-testid="summary-waiting" className="text-right font-medium">
          {summary.waiting_for_sync}
        </dd>

        {summary.out_of_sync > 0 && (
          <>
            <dt className="text-slate-500">Changed at the Store</dt>
            <dd data-testid="summary-out-of-sync" className="text-right font-medium">
              {summary.out_of_sync}
            </dd>
          </>
        )}

        {summary.desired_muted > 0 && (
          <>
            <dt className="text-slate-500">Desired muted</dt>
            <dd data-testid="summary-desired-muted" className="text-right font-medium">
              {summary.desired_muted}
            </dd>
          </>
        )}
      </dl>

      <p className="text-xs text-slate-400">
        Muted and low counts are live Stores only; an offline Store's last
        reported value is not current. Sync counts include every Store.
      </p>

      <Link
        to="/master-volume"
        data-testid="summary-open-master-volume"
        className="inline-block text-sm text-red-600 hover:text-red-700 font-medium"
      >
        Open Master Volume →
      </Link>
    </section>
  );
}
