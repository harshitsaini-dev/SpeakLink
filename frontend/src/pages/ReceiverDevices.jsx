import React from "react";
import { useParams, Link } from "react-router-dom";
import { api } from "@/lib/api";
import { RefreshCw, Plus, KeyRound, Star, Ban, Trash2, ArrowLeft, Archive, ArchiveRestore } from "lucide-react";

// Receiver Device administration for one Store.
//
// Two secrets can appear on this page, and each appears exactly once, ever: a
// one-time enrolment code and a rotated Device credential. The server does not
// keep either - it holds a verifier, not the value - so there is no "show it
// again". That is not a limitation to work around; it is the reason a leaked
// credential cannot be recovered from a database backup.
//
// So both live in React state and nowhere else. Not localStorage, not the URL,
// not a query parameter. A reload loses them, which is correct: if an operator
// navigates away before copying, the honest answer is to issue another one.

const SHOWN_ONCE_WARNING =
  "This is shown once and cannot be retrieved again. Copy it now; if you lose it, issue a new one.";

/** Enough to recognise a Device without printing an identifier in full. */
function shortId(publicId) {
  return typeof publicId === "string" && publicId.length > 8 ? `${publicId.slice(0, 8)}…` : publicId;
}

function RoleBadge({ role }) {
  const isPrimary = role === "PRIMARY";
  return (
    <span
      data-testid={`device-role-${role}`}
      className={
        "inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-semibold uppercase tracking-wide " +
        (isPrimary
          ? "bg-blue-50 text-blue-800 border border-blue-200"
          : "bg-slate-100 text-slate-600 border border-slate-200")
      }
    >
      {isPrimary && <Star size={11} aria-hidden="true" />}
      {isPrimary ? "Primary" : "Standby"}
    </span>
  );
}

/**
 * The one-time enrolment code, with a live countdown and an honest state.
 *
 * The countdown is not decoration. A code is short-lived and shown once, and
 * "expires in 15 minutes" written at the moment of issue is wrong from the
 * second after - an operator reading it ten minutes later has no idea how long
 * they have. The state is derived from the clock rather than from a flag,
 * because nothing tells this page when the code was redeemed.
 *
 * USED now comes from the backend rather than being guessed. The distinction
 * matters: a code redeemed thirty seconds into a fifteen-minute life is USED for
 * ever, and letting the countdown relabel it EXPIRED would report a Store that
 * IS enrolled as one whose setup failed. When the server says USED, the server
 * wins - the clock is only consulted while nothing better is known.
 *
 * The setup stages come from the same response and are each backed by stored or
 * currently-true evidence. None is inferred from elapsed time, so a stage that
 * is absent means "not proved", never "probably fine by now".
 */
const STAGE_LABELS = {
  CODE_CREATED: "Code created",
  CODE_REDEEMED: "Code redeemed",
  DEVICE_CREATED: "Device created",
  DEVICE_CONNECTED: "Device connected",
  PRIMARY_ASSIGNED: "Primary assigned",
};
const STAGE_ORDER = Object.keys(STAGE_LABELS);

function SetupProgress({ progress }) {
  const reached = new Set(progress || []);
  return (
    <ol data-testid="setup-progress" className="mt-2 space-y-0.5 text-xs">
      {STAGE_ORDER.map((stage) => {
        const done = reached.has(stage);
        return (
          <li key={stage} data-testid={`setup-stage-${stage}`}
              data-reached={done ? "true" : "false"}
              className={done ? "text-emerald-800" : "text-slate-400"}>
            <span aria-hidden="true">{done ? "✓" : "○"}</span>{" "}
            {STAGE_LABELS[stage]}
            {!done && <span className="sr-only"> (not proved)</span>}
          </li>
        );
      })}
    </ol>
  );
}

function EnrolmentCodePanel({ issued, record, onDismiss }) {
  const issuedAt = React.useMemo(() => Date.now(), [issued]);
  const total = issued.expires_in_seconds || 0;
  const [remaining, setRemaining] = React.useState(total);

  React.useEffect(() => {
    setRemaining(total);
    const timer = setInterval(() => {
      const left = Math.max(0, total - Math.floor((Date.now() - issuedAt) / 1000));
      setRemaining(left);
      if (left === 0) clearInterval(timer);
    }, 1000);
    return () => clearInterval(timer);
  }, [issuedAt, total]);

  // The server's answer is authoritative whenever there is one. The clock is a
  // fallback for the window before the first poll returns, not a second opinion.
  const used = record?.state === "USED";
  const expired = !used && remaining <= 0;
  const state = used ? "USED" : expired ? "EXPIRED" : "UNUSED";
  const minutes = Math.floor(remaining / 60);
  const seconds = String(remaining % 60).padStart(2, "0");

  const tone = used
    ? "border-emerald-200 bg-emerald-50"
    : expired
      ? "border-slate-300 bg-slate-50"
      : "border-amber-200 bg-amber-50";
  const badgeTone = used
    ? "border-emerald-300 bg-emerald-100 text-emerald-900"
    : expired
      ? "border-slate-300 bg-slate-200 text-slate-700"
      : "border-amber-300 bg-amber-100 text-amber-900";

  return (
    <div data-testid="enrolment-code-panel" className={`rounded border px-3 py-2 ${tone}`}>
      <div className="mb-1 flex items-center gap-2 text-xs">
        <span className="font-medium text-slate-700">One-time enrolment code</span>
        <span data-testid="enrolment-code-state"
              className={`rounded border px-2 py-0.5 font-medium ${badgeTone}`}>
          {state}
        </span>
        {!used && !expired && (
          <span data-testid="enrolment-code-countdown" className="text-slate-600"
                aria-live="polite">
            expires in {minutes}:{seconds}
          </span>
        )}
      </div>

      {used ? (
        // The code is gone from the DOM the moment it is spent. Keeping it on
        // screen after redemption is keeping a secret on screen for no reason.
        <div data-testid="enrolment-code-used">
          <p className="text-sm text-emerald-900">
            This code was used. It cannot be used again and cannot be shown again.
          </p>
          {record?.device_public_id && (
            <p className="mt-1 text-xs text-slate-700">
              Enrolled Device:{" "}
              <code data-testid="enrolled-device-public-id">{record.device_public_id}</code>
            </p>
          )}
        </div>
      ) : expired ? (
        // The value is removed from the DOM entirely once it can no longer be
        // used. A dead secret left on screen is still a secret on screen.
        <p className="text-sm text-slate-700" data-testid="enrolment-code-expired">
          This code has run out of time and was removed. Generate a new one and
          use it straight away.
        </p>
      ) : (
        <ShownOnce
          testId="issued-enrolment-code"
          label="Read this out to the Store now. It is shown once and works once."
          value={issued.code}
          onDismiss={onDismiss}
        />
      )}

      {record && <SetupProgress progress={record.progress} />}

      {(expired || used) && (
        <button type="button" onClick={onDismiss} data-testid="enrolment-code-dismiss"
                className="mt-2 rounded border px-2 py-1 text-xs">
          Dismiss
        </button>
      )}
    </div>
  );
}

function ShownOnce({ label, value, testId, onDismiss }) {
  return (
    <div
      data-testid={testId}
      role="alert"
      className="border-2 border-amber-300 bg-amber-50 rounded-md px-4 py-3 space-y-2"
    >
      <div className="text-sm font-semibold text-amber-900">{label}</div>
      <code
        data-testid={`${testId}-value`}
        className="block break-all bg-white border border-amber-200 rounded px-3 py-2 font-mono text-sm"
      >
        {value}
      </code>
      <div className="text-xs text-amber-900">{SHOWN_ONCE_WARNING}</div>
      <button
        type="button"
        data-testid={`${testId}-dismiss`}
        onClick={onDismiss}
        className="px-3 py-1.5 border border-amber-400 rounded text-xs font-medium hover:bg-amber-100"
      >
        I have copied it
      </button>
    </div>
  );
}

export default function ReceiverDevices() {
  const { storeId } = useParams();
  const [devices, setDevices] = React.useState([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState("");
  // Deliberately component state. See the note at the top of this file.
  const [issuedCode, setIssuedCode] = React.useState(null);
  const [issuedCredential, setIssuedCredential] = React.useState(null);
  // The authenticated evidence for the code on screen. Null means "not known
  // yet", which is why the panel falls back to the clock until it arrives -
  // and never the other way round.
  const [codeRecord, setCodeRecord] = React.useState(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const { data } = await api.get(`/stores/${storeId}/receiver-devices/roles`);
      setDevices(Array.isArray(data) ? data : []);
    } catch (e) {
      // Neutral: a failure here must not describe the server's internals.
      setError("Receiver Devices could not be loaded. Try again.");
    } finally {
      setLoading(false);
    }
  }, [storeId]);

  React.useEffect(() => {
    load();
  }, [load]);

  // Poll the authenticated enrollment record while a code is on screen, so USED
  // and the setup stages come from the server instead of being guessed. Stops as
  // soon as the code is spent: there is nothing further to learn, and a page
  // left open on a Store desk should not keep asking for ever.
  React.useEffect(() => {
    if (!issuedCode) {
      setCodeRecord(null);
      return undefined;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const { data } = await api.get(`/stores/${storeId}/enrollment-codes`);
        if (cancelled) return;
        const newest = Array.isArray(data) && data.length ? data[0] : null;
        setCodeRecord(newest);
        if (newest?.state === "USED") {
          // A redemption changes which Devices exist, so refresh the list too.
          load();
          clearInterval(timer);
        }
      } catch (e) {
        // Leave the last known record in place. Losing one poll must not make a
        // USED code look UNUSED again.
      }
    };
    poll();
    const timer = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [issuedCode, storeId, load]);

  const act = async (key, request, onDone) => {
    setBusy(key);
    setError("");
    try {
      const { data } = await request();
      if (onDone) onDone(data);
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || "That action could not be completed.");
    } finally {
      setBusy("");
    }
  };

  const createCode = () =>
    act(
      "create-code",
      () => api.post("/receiver-devices/enrollment-codes", { store_id: Number(storeId) }),
      (data) => setIssuedCode(data),
    );

  const promote = (device) => {
    if (
      !window.confirm(
        `Make "${device.display_name}" the primary Receiver for this Store?\n\n` +
          "It will start receiving live announcements. The current primary will stop.",
      )
    )
      return;
    return act("promote-" + device.public_id, () =>
      api.post(`/receiver-devices/${device.public_id}/promote`),
    );
  };

  const rotate = (device) => {
    if (
      !window.confirm(
        `Rotate the credential for "${device.display_name}"?\n\n` +
          "The old credential stops working immediately. This Device will be offline " +
          "until an operator enters the new credential on that computer.",
      )
    )
      return;
    return act(
      "rotate-" + device.public_id,
      () => api.post(`/receiver-devices/${device.public_id}/rotate-credential`),
      (data) => setIssuedCredential(data),
    );
  };

  const disable = (device) => {
    if (
      !window.confirm(
        `Disable "${device.display_name}"?\n\n` +
          "It will stop connecting. Its Store and every other Device keep working. " +
          "If it is the primary, this Store will have no primary until you promote another.",
      )
    )
      return;
    return act("disable-" + device.public_id, () =>
      api.post(`/receiver-devices/${device.public_id}/disable`),
    );
  };

  const revoke = (device) => {
    if (
      !window.confirm(
        `Revoke "${device.display_name}" permanently?\n\n` +
          "Its credential stops working and cannot be restored. This cannot be undone.",
      )
    )
      return;
    return act("revoke-" + device.public_id, () =>
      api.post(`/receiver-devices/${device.public_id}/revoke`),
    );
  };

  const archive = (device) => {
    if (
      !window.confirm(
        `Archive "${device.display_name}"?\n\n` +
          "It is disabled and hidden from active setup, but its credential history stays " +
          "readable and it can be restored later.",
      )
    )
      return;
    return act("archive-" + device.public_id, () =>
      api.post(`/receiver-devices/${device.public_id}/archive`),
    );
  };

  const restore = (device) => {
    return act("restore-" + device.public_id, () =>
      api.post(`/receiver-devices/${device.public_id}/restore`),
    );
  };

  const [deletingDevice, setDeletingDevice] = React.useState(null);

  const hasPrimary = devices.some((d) => d.role === "PRIMARY" && d.status === "active");

  return (
    <div className="space-y-4" data-testid="receiver-devices-page">
      <div className="flex items-center justify-between">
        <div>
          <Link
            to="/stores"
            data-testid="back-to-stores"
            className="inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-800"
          >
            <ArrowLeft size={12} /> Back to Stores
          </Link>
          <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Receiver Devices</h1>
          <p className="text-sm text-slate-500">
            The computers enrolled in this Store. One primary plays announcements; standbys stay
            connected and play nothing. Credentials are shown once and never stored here.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            data-testid="devices-refresh-btn"
            onClick={load}
            className="inline-flex items-center gap-1 px-3 py-2 border border-slate-300 rounded-md text-sm hover:bg-slate-50"
          >
            <RefreshCw size={14} /> Refresh
          </button>
          <button
            data-testid="create-enrolment-code-btn"
            onClick={createCode}
            disabled={busy === "create-code"}
            className="inline-flex items-center gap-1 px-3 py-2 bg-blue-700 hover:bg-blue-800 disabled:opacity-60 text-white rounded-md text-sm font-medium"
          >
            <Plus size={16} /> {busy === "create-code" ? "Creating…" : "Create enrolment code"}
          </button>
        </div>
      </div>

      {error && (
        <div
          data-testid="devices-error"
          role="alert"
          className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2"
        >
          {error}
        </div>
      )}

      {issuedCode && (
        <EnrolmentCodePanel
          issued={issuedCode}
          record={codeRecord}
          onDismiss={() => setIssuedCode(null)}
        />
      )}

      {issuedCredential && (
        <ShownOnce
          testId="issued-credential"
          label="New Device credential — enter this on that computer with the Agent's rotate-local-credential command"
          value={issuedCredential.credential}
          onDismiss={() => setIssuedCredential(null)}
        />
      )}

      {!loading && devices.length > 0 && !hasPrimary && (
        <div
          data-testid="no-primary-warning"
          role="status"
          className="text-sm text-amber-900 bg-amber-50 border border-amber-200 rounded px-3 py-2"
        >
          This Store has no primary Device, so it will not receive announcements. Promote one.
        </div>
      )}

      <div className="border border-slate-200 rounded-md bg-white overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Receiver Devices enrolled in this Store</caption>
          <thead className="bg-slate-50 text-left text-[11px] uppercase tracking-wider text-slate-500 border-b border-slate-200">
            <tr>
              <th scope="col" className="px-3 py-2">Device</th>
              <th scope="col" className="px-3 py-2">Identifier</th>
              <th scope="col" className="px-3 py-2">Role</th>
              <th scope="col" className="px-3 py-2">Status</th>
              <th scope="col" className="px-3 py-2">Enrolled</th>
              <th scope="col" className="px-3 py-2 text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={6} data-testid="devices-loading" className="px-3 py-6 text-center text-slate-500">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && devices.length === 0 && (
              <tr>
                <td colSpan={6} data-testid="devices-empty" className="px-3 py-6 text-center text-slate-500">
                  No Receiver Devices are enrolled in this Store yet. Create an enrolment code and
                  run the Agent on that computer.
                </td>
              </tr>
            )}
            {devices.map((device) => (
              <tr
                key={device.public_id}
                data-testid={`device-row-${device.public_id}`}
                className="border-b border-slate-100 even:bg-slate-50/50"
              >
                <td className="px-3 py-2 font-medium">{device.display_name}</td>
                <td className="px-3 py-2 font-mono text-xs text-slate-500" title="Shortened identifier">
                  {shortId(device.public_id)}
                </td>
                <td className="px-3 py-2">
                  <RoleBadge role={device.role} />
                </td>
                <td className="px-3 py-2">
                  <span
                    data-testid={`device-status-${device.public_id}`}
                    className="text-xs uppercase tracking-wide text-slate-600"
                  >
                    {device.status}
                  </span>
                </td>
                <td className="px-3 py-2 text-xs text-slate-500">
                  {(device.enrolled_at || "").slice(0, 16).replace("T", " ")}
                </td>
                <td className="px-3 py-2 text-right space-x-1 whitespace-nowrap">
                  {device.status === "active" && device.role !== "PRIMARY" && (
                    <button
                      data-testid={`promote-${device.public_id}`}
                      onClick={() => promote(device)}
                      disabled={busy.startsWith("promote-")}
                      title="Make this the primary Device"
                      aria-label={`Make ${device.display_name} the primary Device`}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-blue-200 text-blue-800 rounded hover:bg-blue-50 disabled:opacity-50"
                    >
                      <Star size={12} /> Promote
                    </button>
                  )}
                  {device.status === "active" && (
                    <button
                      data-testid={`rotate-${device.public_id}`}
                      onClick={() => rotate(device)}
                      disabled={busy.startsWith("rotate-")}
                      title="Rotate this Device's credential"
                      aria-label={`Rotate the credential for ${device.display_name}`}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-amber-50 disabled:opacity-50"
                    >
                      <KeyRound size={12} /> Rotate
                    </button>
                  )}
                  {device.status === "active" && (
                    <button
                      data-testid={`disable-${device.public_id}`}
                      onClick={() => disable(device)}
                      disabled={busy.startsWith("disable-")}
                      title="Disable this Device"
                      aria-label={`Disable ${device.display_name}`}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-slate-50 disabled:opacity-50"
                    >
                      <Ban size={12} /> Disable
                    </button>
                  )}
                  {device.status !== "retired" && (
                    <button
                      data-testid={`revoke-${device.public_id}`}
                      onClick={() => revoke(device)}
                      disabled={busy.startsWith("revoke-")}
                      title="Revoke this Device permanently"
                      aria-label={`Revoke ${device.display_name} permanently`}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-200 text-red-700 rounded hover:bg-red-50 disabled:opacity-50"
                    >
                      <Trash2 size={12} /> Revoke
                    </button>
                  )}
                  {!device.archived_at ? (
                    <button
                      data-testid={`archive-${device.public_id}`}
                      onClick={() => archive(device)}
                      disabled={busy.startsWith("archive-")}
                      title="Archive this Device"
                      aria-label={`Archive ${device.display_name}`}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-slate-50 disabled:opacity-50"
                    >
                      <Archive size={12} /> Archive
                    </button>
                  ) : (
                    <button
                      data-testid={`restore-${device.public_id}`}
                      onClick={() => restore(device)}
                      disabled={busy.startsWith("restore-")}
                      title="Restore this Device"
                      aria-label={`Restore ${device.display_name}`}
                      className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-slate-200 rounded hover:bg-slate-50 disabled:opacity-50"
                    >
                      <ArchiveRestore size={12} /> Restore
                    </button>
                  )}
                  <button
                    data-testid={`delete-${device.public_id}`}
                    onClick={() => setDeletingDevice(device)}
                    title="Permanently delete this Device"
                    aria-label={`Permanently delete ${device.display_name}`}
                    className="inline-flex items-center gap-1 text-xs px-2 py-1 border border-red-300 text-red-700 rounded hover:bg-red-50 disabled:opacity-50"
                  >
                    <Trash2 size={12} /> Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {deletingDevice && (
        <DeleteDeviceDialog
          device={deletingDevice}
          onCancel={() => setDeletingDevice(null)}
          onConfirm={async (typed) => {
            await act(
              "delete-" + deletingDevice.public_id,
              () => api.delete(`/receiver-devices/${deletingDevice.public_id}/permanently`,
                               { params: { confirm: typed } }),
            );
            setDeletingDevice(null);
          }}
        />
      )}

      <p className="text-xs text-slate-400">
        Playing here means a Device accepted and decoded audio. It does not mean anyone heard it -
        only EchoGuard acoustic verification can establish that.
      </p>
    </div>
  );
}

/**
 * Permanent deletion, refused far more often than it succeeds - the same
 * shape as UserManagement's DeleteUserDialog. Every enrolled Device has at
 * least one issued credential, so this is refused unless the row was never
 * really used.
 */
function DeleteDeviceDialog({ device, onCancel, onConfirm }) {
  const [summary, setSummary] = React.useState(null);
  const [failed, setFailed] = React.useState("");
  const [typed, setTyped] = React.useState("");

  React.useEffect(() => {
    let cancelled = false;
    api.get(`/receiver-devices/${device.public_id}/dependencies`)
      .then((response) => { if (!cancelled) setSummary(response.data); })
      .catch(() => { if (!cancelled) setFailed("The dependency check could not be run."); });
    return () => { cancelled = true; };
  }, [device.public_id]);

  const matches = typed === device.public_id;

  return (
    <div className="rounded border border-red-200 bg-red-50 p-4 space-y-3"
         data-testid="delete-device-dialog">
      <h2 className="font-medium text-red-900">
        Permanently delete {device.display_name}
      </h2>

      {summary === null && !failed && (
        <p className="text-sm text-slate-600" data-testid="delete-device-checking">
          Checking what still refers to this Device…
        </p>
      )}
      {failed && (
        <p className="text-sm text-red-800" data-testid="delete-device-check-failed">
          {failed} Nothing has been deleted. Archive this Device instead.
        </p>
      )}
      {summary && (
        <>
          <p className="text-sm text-slate-700" data-testid="delete-device-summary">
            {summary.explanation}
          </p>
          {!summary.deletable && (
            <p className="text-sm text-red-800" data-testid="delete-device-blocked">
              This Device cannot be deleted. Archive it instead — its history stays readable
              and it can be restored.
            </p>
          )}
          {summary.deletable && (
            <>
              <p className="text-sm text-red-800">
                This cannot be undone. Type the Device id exactly to confirm:
                <br /><code className="text-xs">{device.public_id}</code>
              </p>
              <input
                className="w-full max-w-md rounded border px-2 py-1" value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off" data-testid="delete-device-confirm-input"
              />
            </>
          )}
        </>
      )}

      <div className="flex gap-2">
        <button
          type="button"
          disabled={!summary || !summary.deletable || !matches}
          onClick={() => onConfirm(typed)}
          className="rounded bg-red-700 px-3 py-2 text-sm text-white disabled:opacity-40"
          data-testid="delete-device-confirm"
        >
          Delete permanently
        </button>
        <button type="button" onClick={onCancel} className="rounded border px-3 py-2 text-sm"
                data-testid="delete-device-cancel">
          Cancel
        </button>
      </div>
    </div>
  );
}
