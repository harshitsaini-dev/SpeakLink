/**
 * Who can sign in to EchoCast Live, and what they are allowed to do.
 *
 * Two things about this page are deliberate and easy to get wrong.
 *
 * It hides controls an account may not use - but hiding a control is a courtesy
 * to the person looking at the screen, never a security measure. Every refusal
 * here is also enforced by the backend, which answers 403 to the request
 * whatever the page chose to render. When that happens the message is shown
 * plainly rather than swallowed, because a button that silently does nothing is
 * worse than one that explains itself.
 *
 * No password, hash, token or session counter is ever put in the DOM, a URL,
 * localStorage or the console. A password an administrator sets is held in
 * component state until the request is sent, and then dropped.
 */
import React from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";
import {
  UserPlus, RefreshCw, KeyRound, Pencil, Power, Archive, ArchiveRestore, ShieldAlert,
} from "lucide-react";

const ROLES = ["OWNER", "ADMIN", "BROADCASTER", "VIEWER"];

const ROLE_STYLE = {
  OWNER: "bg-purple-100 text-purple-800 border-purple-200",
  ADMIN: "bg-blue-100 text-blue-800 border-blue-200",
  BROADCASTER: "bg-emerald-100 text-emerald-800 border-emerald-200",
  VIEWER: "bg-slate-100 text-slate-700 border-slate-200",
};

const STATE_STYLE = {
  active: "bg-emerald-100 text-emerald-800 border-emerald-200",
  disabled: "bg-amber-100 text-amber-900 border-amber-200",
  archived: "bg-slate-200 text-slate-600 border-slate-300",
};

/** Which roles a role may act on. Mirrors rbac._MANAGEABLE_ROLES exactly.
 *
 * A copy of a server rule in the client is a liability the moment the two
 * disagree, so this decides only what to SHOW. The server decides what happens.
 */
const MANAGEABLE = {
  SUPER_ADMIN: new Set(ROLES),
  ADMIN: new Set(["BROADCASTER", "VIEWER"]),
  BROADCASTER: new Set(),
  VIEWER: new Set(),
};

function Badge({ text, styles }) {
  return (
    <span className={`inline-block rounded border px-2 py-0.5 text-xs font-medium ${styles}`}>
      {text}
    </span>
  );
}

export default function UserManagement() {
  const { user: me } = useAuth();
  const [users, setUsers] = React.useState(null);
  const [error, setError] = React.useState("");
  const [notice, setNotice] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [creating, setCreating] = React.useState(false);
  const [editing, setEditing] = React.useState(null);
  const [resetting, setResetting] = React.useState(null);

  const myRole = me?.role || "VIEWER";
  const canManage = (role) => (MANAGEABLE[myRole] || new Set()).has(role);
  const isSuperAdmin = myRole === "OWNER";

  const load = React.useCallback(async () => {
    setError("");
    try {
      const response = await api.get("/users");
      setUsers(response.data);
    } catch (failure) {
      setUsers([]);
      setError(describe(failure, "The list of Users could not be loaded."));
    }
  }, []);

  React.useEffect(() => { load(); }, [load]);

  /** Show what the server said, not a guess.
   *
   * A 403 here usually means the page offered something this account may not
   * do. Replacing that with "Something went wrong" hides a real answer.
   */
  function describe(failure, fallback) {
    const status = failure?.response?.status;
    const detail = failure?.response?.data?.detail;
    if (status === 403) return detail || "You do not have permission to do that.";
    if (status === 409) return detail || "That change was refused.";
    if (status === 404) return "That account no longer exists.";
    if (typeof detail === "string") return detail;
    return fallback;
  }

  async function act(request, successMessage) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await request();
      setNotice(successMessage);
      await load();
      return true;
    } catch (failure) {
      setError(describe(failure, "That action could not be completed."));
      return false;
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="p-6 space-y-4" data-testid="user-management-page">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">User Management</h1>
          <p className="text-sm text-slate-500">
            Accounts are archived, never deleted, so the record of who did what stays readable.
          </p>
          {/* Said here rather than only in a refusal message, so an ADMIN
              understands why a control is missing instead of thinking the page
              is broken. */}
          <p className="mt-1 text-xs text-slate-500" data-testid="owner-protection-note">
            <strong>OWNER</strong> and <strong>ADMIN</strong> both have full operational
            access. The one difference: only an OWNER can change another OWNER, and the
            last enabled OWNER cannot be disabled, archived or demoted — otherwise nobody
            would be able to administer EchoCast again.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button" onClick={load} disabled={busy}
            className="inline-flex items-center gap-2 rounded border px-3 py-2 text-sm"
            data-testid="refresh-users"
          >
            <RefreshCw size={16} /> Refresh
          </button>
          {MANAGEABLE[myRole]?.size > 0 && (
            <button
              type="button" onClick={() => setCreating(true)} disabled={busy}
              className="inline-flex items-center gap-2 rounded bg-slate-900 px-3 py-2 text-sm text-white"
              data-testid="new-user"
            >
              <UserPlus size={16} /> New User
            </button>
          )}
        </div>
      </header>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
             role="alert" data-testid="user-error">
          <ShieldAlert size={16} className="mr-2 inline" />{error}
        </div>
      )}
      {notice && (
        <div className="rounded border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
             role="status" data-testid="user-notice">
          {notice}
        </div>
      )}

      {users === null && (
        <p className="text-sm text-slate-500" data-testid="users-loading">Loading Usersâ€¦</p>
      )}
      {users !== null && users.length === 0 && !error && (
        <p className="text-sm text-slate-500" data-testid="users-empty">
          There are no HQ Users to show.
        </p>
      )}

      {users !== null && users.length > 0 && (
        <table className="w-full text-sm" data-testid="users-table">
          <thead className="text-left text-slate-500">
            <tr>
              <th className="py-2">Name</th>
              <th>Username</th>
              <th>Role</th>
              <th>Status</th>
              <th className="text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((row) => {
              const mine = me && row.id === me.id;
              const allowed = canManage(row.role) && !mine;
              return (
                <tr key={row.id} className="border-t" data-testid={`user-row-${row.username}`}>
                  <td className="py-2">{row.display_name}</td>
                  <td className="text-slate-600">{row.username}</td>
                  <td><Badge text={row.role} styles={ROLE_STYLE[row.role] || ROLE_STYLE.VIEWER} /></td>
                  <td>
                    <Badge text={row.lifecycle_state}
                           styles={STATE_STYLE[row.lifecycle_state] || STATE_STYLE.disabled} />
                  </td>
                  <td className="space-x-2 text-right">
                    {/* Editing a name is not a lock-out, so it is offered for
                        your own account too. */}
                    {(allowed || mine) && (
                      <button type="button" disabled={busy} onClick={() => setEditing(row)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`edit-${row.username}`}>
                        <Pencil size={14} /> Edit
                      </button>
                    )}
                    {allowed && row.lifecycle_state === "active" && (
                      <button type="button" disabled={busy}
                              onClick={() => act(() => api.post(`/users/${row.id}/disable`),
                                                 `${row.display_name} was disabled.`)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`disable-${row.username}`}>
                        <Power size={14} /> Disable
                      </button>
                    )}
                    {allowed && row.lifecycle_state === "disabled" && (
                      <button type="button" disabled={busy}
                              onClick={() => act(() => api.post(`/users/${row.id}/enable`),
                                                 `${row.display_name} was enabled.`)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`enable-${row.username}`}>
                        <Power size={14} /> Enable
                      </button>
                    )}
                    {allowed && row.lifecycle_state !== "archived" && (
                      <button type="button" disabled={busy}
                              onClick={() => act(() => api.post(`/users/${row.id}/archive`),
                                                 `${row.display_name} was archived.`)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`archive-${row.username}`}>
                        <Archive size={14} /> Archive
                      </button>
                    )}
                    {allowed && row.lifecycle_state === "archived" && (
                      <button type="button" disabled={busy}
                              onClick={() => act(() => api.post(`/users/${row.id}/restore`),
                                                 `${row.display_name} was restored, and is disabled until somebody enables it.`)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`restore-${row.username}`}>
                        <ArchiveRestore size={14} /> Restore
                      </button>
                    )}
                    {isSuperAdmin && !mine && (
                      <button type="button" disabled={busy} onClick={() => setResetting(row)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`reset-${row.username}`}>
                        <KeyRound size={14} /> Reset password
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {creating && (
        <CreateUserForm
          myRole={myRole}
          onCancel={() => setCreating(false)}
          onSubmit={async (payload) => {
            const ok = await act(() => api.post("/users", payload),
                                 `${payload.display_name} was created.`);
            if (ok) setCreating(false);
          }}
        />
      )}

      {editing && (
        <EditUserForm
          user={editing}
          onCancel={() => setEditing(null)}
          onSubmit={async (payload, role) => {
            let ok = await act(() => api.patch(`/users/${editing.id}`, payload),
                               `${payload.display_name} was updated.`);
            if (ok && role && role !== editing.role) {
              ok = await act(() => api.post(`/users/${editing.id}/role`, { role }),
                             `The role was changed to ${role}. That account has been signed out.`);
            }
            if (ok) setEditing(null);
          }}
          canChangeRole={canManage(editing.role) && editing.id !== me?.id}
          assignableRoles={ROLES.filter((role) => canManage(role))}
        />
      )}

      {resetting && (
        <ResetPasswordForm
          user={resetting}
          onCancel={() => setResetting(null)}
          onSubmit={async (newPassword) => {
            const ok = await act(
              () => api.post(`/users/${resetting.id}/reset-password`, { new_password: newPassword }),
              `The password for ${resetting.display_name} was set. Every session for that account has ended.`);
            if (ok) setResetting(null);
          }}
        />
      )}
    </div>
  );
}

function Panel({ title, children, onCancel, submitLabel, onSubmit, testid }) {
  return (
    <form
      className="rounded border bg-white p-4 space-y-3" data-testid={testid}
      onSubmit={(event) => { event.preventDefault(); onSubmit(); }}
    >
      <h2 className="font-medium">{title}</h2>
      {children}
      <div className="flex gap-2">
        <button type="submit" className="rounded bg-slate-900 px-3 py-2 text-sm text-white"
                data-testid={`${testid}-submit`}>
          {submitLabel}
        </button>
        <button type="button" onClick={onCancel} className="rounded border px-3 py-2 text-sm"
                data-testid={`${testid}-cancel`}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function Field({ label, ...rest }) {
  return (
    <label className="block text-sm">
      <span className="mb-1 block text-slate-600">{label}</span>
      <input className="w-full rounded border px-2 py-1" {...rest} />
    </label>
  );
}

function CreateUserForm({ myRole, onCancel, onSubmit }) {
  const assignable = ROLES.filter((role) => (MANAGEABLE[myRole] || new Set()).has(role));
  const [form, setForm] = React.useState({
    username: "", display_name: "", role: assignable[0] || "VIEWER", password: "",
  });
  const change = (key) => (event) => setForm({ ...form, [key]: event.target.value });

  return (
    <Panel title="New User" testid="create-user-form" submitLabel="Create"
           onCancel={onCancel} onSubmit={() => onSubmit(form)}>
      <Field label="Display name" value={form.display_name} onChange={change("display_name")}
             required data-testid="create-display-name" />
      <Field label="Username" value={form.username} onChange={change("username")}
             required data-testid="create-username" />
      <label className="block text-sm">
        <span className="mb-1 block text-slate-600">Role</span>
        <select className="w-full rounded border px-2 py-1" value={form.role}
                onChange={change("role")} data-testid="create-role">
          {assignable.map((role) => <option key={role} value={role}>{role}</option>)}
        </select>
      </label>
      {/* type="password" so it is not read over a shoulder, and never written
          anywhere but this field and the request that follows. */}
      <Field label="Temporary password (at least 8 characters)" type="password"
             value={form.password} onChange={change("password")} minLength={8}
             required data-testid="create-password" autoComplete="new-password" />
      <p className="text-xs text-slate-500">
        Tell them this password in person or by phone, and ask them to change it. It is not
        e-mailed, not shown again, and not stored anywhere you can read it back.
      </p>
    </Panel>
  );
}

function EditUserForm({ user, onCancel, onSubmit, canChangeRole, assignableRoles }) {
  const [form, setForm] = React.useState({
    display_name: user.display_name, username: user.username,
  });
  const [role, setRole] = React.useState(user.role);
  const change = (key) => (event) => setForm({ ...form, [key]: event.target.value });

  return (
    <Panel title={`Edit ${user.username}`} testid="edit-user-form" submitLabel="Save"
           onCancel={onCancel} onSubmit={() => onSubmit(form, canChangeRole ? role : null)}>
      <Field label="Display name" value={form.display_name} onChange={change("display_name")}
             required data-testid="edit-display-name" />
      <Field label="Username" value={form.username} onChange={change("username")}
             required data-testid="edit-username" />
      {canChangeRole && (
        <label className="block text-sm">
          <span className="mb-1 block text-slate-600">Role</span>
          <select className="w-full rounded border px-2 py-1" value={role}
                  onChange={(event) => setRole(event.target.value)} data-testid="edit-role">
            {[...new Set([user.role, ...assignableRoles])].map((option) => (
              <option key={option} value={option}>{option}</option>
            ))}
          </select>
          <span className="mt-1 block text-xs text-slate-500">
            Changing a role signs that account out immediately.
          </span>
        </label>
      )}
    </Panel>
  );
}

function ResetPasswordForm({ user, onCancel, onSubmit }) {
  const [password, setPassword] = React.useState("");
  const [confirmed, setConfirmed] = React.useState(false);

  return (
    <Panel title={`Reset the password for ${user.username}`} testid="reset-password-form"
           submitLabel="Set password" onCancel={onCancel}
           onSubmit={() => confirmed && onSubmit(password)}>
      <p className="text-sm text-slate-600">
        This ends every session that account currently has, everywhere.
      </p>
      <Field label="New password (at least 8 characters)" type="password" value={password}
             onChange={(event) => setPassword(event.target.value)} minLength={8}
             required data-testid="reset-password-value" autoComplete="new-password" />
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={confirmed} data-testid="reset-confirm"
               onChange={(event) => setConfirmed(event.target.checked)} />
        I will give this password to {user.display_name} in person or by phone, not in writing.
      </label>
    </Panel>
  );
}
