/**
 * Who can sign in to SpeakLink, and what they are allowed to do.
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
  UserPlus, RefreshCw, KeyRound, Pencil, Power, Archive, ArchiveRestore, ShieldAlert, Trash2,
  ShieldCheck, MapPin,
} from "lucide-react";

const ROLES = ["OWNER", "ADMIN", "BROADCASTER", "VIEWER"];

/**
 * Display-only rename: OWNER -> SUPER ADMIN.
 *
 * The stored role value, the API, and every backend comparison stay
 * "OWNER" - rbac.py's own history explains why that string is the one
 * thing every existing account, migration and test already agrees on
 * (`LEGACY_ROLE_ALIASES`, the SUPER_ADMIN -> OWNER rename). Renaming the
 * stored value again would need a second migration and touch every test
 * that asserts role == "OWNER". This renames only what a person reads.
 */
const ROLE_LABEL = { OWNER: "SUPER ADMIN", ADMIN: "ADMIN", BROADCASTER: "BROADCASTER", VIEWER: "VIEWER" };
const roleLabel = (role) => ROLE_LABEL[role] || role;

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
  // OWNER, not SUPER_ADMIN. The rename that introduced OWNER rewrote the
  // quoted string "SUPER_ADMIN" everywhere but not this unquoted object KEY, so
  // MANAGEABLE had no entry for the role an owner actually has - canManage()
  // returned false for every row and an OWNER saw no management buttons at all.
  // A rename tool cannot tell an identifier from a string; a person can.
  OWNER: new Set(ROLES),
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
  const [deleting, setDeleting] = React.useState(null);
  const [editingRights, setEditingRights] = React.useState(null);
  const [editingScope, setEditingScope] = React.useState(null);

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
            <strong>SUPER ADMIN</strong> and <strong>ADMIN</strong> both have full operational
            access. The one difference: only a SUPER ADMIN can change another SUPER ADMIN, and the
            last enabled SUPER ADMIN cannot be disabled, archived or demoted — otherwise nobody
            would be able to administer SpeakLink again.
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
                  <td><Badge text={roleLabel(row.role)} styles={ROLE_STYLE[row.role] || ROLE_STYLE.VIEWER} /></td>
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
                    {/* Offered only where the backend could conceivably allow
                        it. Whether it is actually permitted is decided by the
                        dependency summary the dialog fetches, and again by the
                        server inside the deleting transaction. */}
                    {allowed && row.role !== "OWNER" && (
                      <button type="button" disabled={busy} onClick={() => setDeleting(row)}
                              className="inline-flex items-center gap-1 rounded border border-red-300 px-2 py-1 text-red-700"
                              data-testid={`delete-${row.username}`}>
                        <Trash2 size={14} /> Delete
                      </button>
                    )}
                    {/* Only OWNER may reach the endpoint this opens; the button
                        is hidden for the same reason the endpoint refuses an
                        OWNER target - OWNER's rights are never overridden. */}
                    {isSuperAdmin && row.role !== "OWNER" && (
                      <button type="button" disabled={busy} onClick={() => setEditingRights(row)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`rights-${row.username}`}>
                        <ShieldCheck size={14} /> Rights
                      </button>
                    )}
                    {/* Store/City/Zone scope. Same OWNER-only reservation as
                        Rights, for the same reason. */}
                    {isSuperAdmin && row.role !== "OWNER" && (row.role === "ADMIN" || row.role === "BROADCASTER") && (
                      <button type="button" disabled={busy} onClick={() => setEditingScope(row)}
                              className="inline-flex items-center gap-1 rounded border px-2 py-1"
                              data-testid={`scope-${row.username}`}>
                        <MapPin size={14} /> Scope
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

      {deleting && (
        <DeleteUserDialog
          user={deleting}
          onCancel={() => setDeleting(null)}
          onConfirm={async (typed) => {
            const ok = await act(
              () => api.delete(`/users/${deleting.id}/permanently`,
                               { params: { confirm: typed } }),
              `${deleting.username} was permanently deleted.`);
            if (ok) setDeleting(null);
          }}
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

      {editingRights && (
        <RightsEditor
          user={editingRights}
          onCancel={() => setEditingRights(null)}
          onSaved={() => setEditingRights(null)}
        />
      )}

      {editingScope && (
        <ScopeEditor
          user={editingScope}
          onCancel={() => setEditingScope(null)}
          onSaved={() => setEditingScope(null)}
        />
      )}
    </div>
  );
}

const GROUP_ORDER = ["Broadcast", "Stores", "Receivers", "History", "Logs", "Users"];

/**
 * Beginner-friendly rights editor: Role / Override / Effective, per
 * permission, grouped the way an operator thinks about the product rather
 * than by table name. Changing rights here never touches a password or role -
 * it calls exactly one endpoint, PUT /users/{id}/permissions, which does
 * nothing else.
 */
function RightsEditor({ user, onCancel, onSaved }) {
  const [rows, setRows] = React.useState(null);
  const [role, setRole] = React.useState(user.role);
  const [pending, setPending] = React.useState({});
  const [loadError, setLoadError] = React.useState("");
  const [saveError, setSaveError] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoadError("");
    try {
      const { data } = await api.get(`/users/${user.id}/permissions`);
      setRole(data.role);
      setRows(data.permissions);
      setPending({});
    } catch (failure) {
      setLoadError(
        failure?.response?.status === 403
          ? "You do not have permission to view or change this account's rights."
          : "This account's rights could not be loaded."
      );
    }
  }, [user.id]);

  React.useEffect(() => { load(); }, [load]);

  const effectiveOverride = (row) => pending[row.code] ?? row.override;

  const effectiveNow = (row) => {
    const override = effectiveOverride(row);
    if (override === "ALLOW") return true;
    if (override === "DENY") return false;
    return row.role_allowed;
  };

  const changedCount = Object.keys(pending).length;

  const save = async () => {
    setSaving(true);
    setSaveError("");
    setSaved(false);
    try {
      const changes = Object.entries(pending).map(([code, effect]) => ({ code, effect }));
      await api.put(`/users/${user.id}/permissions`, { changes });
      setSaved(true);
      await load();
    } catch (failure) {
      const detail = failure?.response?.data?.detail;
      setSaveError(detail || "These rights could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  const grouped = React.useMemo(() => {
    if (!rows) return [];
    const byGroup = new Map();
    for (const row of rows) {
      if (!byGroup.has(row.group)) byGroup.set(row.group, []);
      byGroup.get(row.group).push(row);
    }
    return GROUP_ORDER.filter((g) => byGroup.has(g)).map((g) => [g, byGroup.get(g)]);
  }, [rows]);

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
         data-testid="rights-editor">
      <div className="bg-white rounded-md shadow-xl max-w-2xl w-full max-h-[85vh] flex flex-col">
        <div className="p-5 border-b flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-widest text-slate-500">User</div>
            <div className="text-lg font-semibold" data-testid="rights-username">{user.display_name}</div>
            <div className="text-xs text-slate-500">
              Base Role: <span className="font-medium" data-testid="rights-base-role">{roleLabel(role)}</span>
            </div>
          </div>
          <button type="button" onClick={onCancel} className="text-slate-500 hover:text-slate-900"
                  data-testid="rights-close">✕</button>
        </div>

        <div className="p-5 overflow-y-auto space-y-5">
          {loadError && (
            <p className="text-sm text-red-700" data-testid="rights-load-error">{loadError}</p>
          )}
          {!rows && !loadError && (
            <p className="text-sm text-slate-500" data-testid="rights-loading">Loading rights…</p>
          )}
          {grouped.map(([group, groupRows]) => (
            <div key={group}>
              <h3 className="text-xs font-bold uppercase tracking-widest text-slate-500 mb-2">{group}</h3>
              <div className="space-y-2">
                {groupRows.map((row) => {
                  const override = effectiveOverride(row);
                  const effective = effectiveNow(row);
                  return (
                    <div key={row.code}
                         className="flex items-center justify-between gap-3 border rounded px-3 py-2"
                         data-testid={`right-row-${row.code}`}>
                      <div className="min-w-0">
                        <div className="text-sm font-medium">{row.label}</div>
                        <div className="text-xs text-slate-500">
                          Role: {row.role_allowed ? "Allowed" : "Not allowed"}
                          {override !== "INHERIT" && <> · Override: {override}</>}
                          {" · "}
                          Effective:{" "}
                          <span className={effective ? "text-emerald-700 font-medium" : "text-red-700 font-medium"}>
                            {effective ? "Allowed" : "Denied"}
                          </span>
                        </div>
                      </div>
                      <select
                        className="border rounded px-2 py-1 text-sm bg-white shrink-0"
                        value={override}
                        data-testid={`right-select-${row.code}`}
                        onChange={(event) => {
                          const value = event.target.value;
                          setPending((prev) => {
                            const next = { ...prev };
                            if (value === row.override) delete next[row.code];
                            else next[row.code] = value;
                            return next;
                          });
                        }}
                      >
                        <option value="INHERIT">Inherit</option>
                        <option value="ALLOW">Allow</option>
                        <option value="DENY">Deny</option>
                      </select>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        <div className="p-5 border-t space-y-2">
          {saveError && <p className="text-sm text-red-700" data-testid="rights-save-error">{saveError}</p>}
          {saved && !saveError && (
            <p className="text-sm text-emerald-700" data-testid="rights-save-success">Rights saved.</p>
          )}
          <div className="flex gap-2">
            <button
              type="button" onClick={save} disabled={saving || changedCount === 0}
              className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-40"
              data-testid="rights-save"
            >
              {saving ? "Saving…" : `Save Changes${changedCount ? ` (${changedCount})` : ""}`}
            </button>
            <button type="button" onClick={onCancel}
                    className="rounded border px-3 py-2 text-sm" data-testid="rights-cancel">
              Cancel
            </button>
            {saved && (
              <button type="button" onClick={onSaved}
                      className="ml-auto rounded border px-3 py-2 text-sm" data-testid="rights-done">
                Done
              </button>
            )}
          </div>
        </div>
      </div>
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
          {assignable.map((role) => <option key={role} value={role}>{roleLabel(role)}</option>)}
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
              <option key={option} value={option}>{roleLabel(option)}</option>
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

/**
 * Permanent deletion, which is refused far more often than it succeeds.
 *
 * The dialog asks the backend what still refers to this account BEFORE
 * offering the button, so somebody is not invited to press a thing that will
 * come back 409. The typed username is the one confirmation that cannot be
 * clicked through by muscle memory - and it is checked again on the server,
 * inside the transaction that deletes, because a dialog is not a control.
 */
function DeleteUserDialog({ user, onCancel, onConfirm }) {
  const [summary, setSummary] = React.useState(null);
  const [failed, setFailed] = React.useState("");
  const [typed, setTyped] = React.useState("");

  React.useEffect(() => {
    let cancelled = false;
    api.get(`/users/${user.id}/dependencies`)
      .then((response) => { if (!cancelled) setSummary(response.data); })
      .catch(() => { if (!cancelled) setFailed("The dependency check could not be run."); });
    return () => { cancelled = true; };
  }, [user.id]);

  const matches = typed === user.username;

  return (
    <div className="rounded border border-red-200 bg-red-50 p-4 space-y-3"
         data-testid="delete-user-dialog">
      <h2 className="font-medium text-red-900">Permanently delete {user.username}</h2>

      {summary === null && !failed && (
        <p className="text-sm text-slate-600" data-testid="delete-checking">
          Checking what still refers to this account…
        </p>
      )}

      {failed && (
        <p className="text-sm text-red-800" data-testid="delete-check-failed">
          {failed} Nothing has been deleted. Archive this account instead.
        </p>
      )}

      {summary && (
        <>
          <p className="text-sm text-slate-700" data-testid="delete-summary">
            {summary.explanation}
          </p>
          {!summary.deletable && (
            <p className="text-sm text-red-800" data-testid="delete-blocked">
              This account cannot be deleted. Archive it instead — the history stays
              readable and the account can be restored.
            </p>
          )}
          {summary.deletable && (
            <>
              <p className="text-sm text-red-800">
                This cannot be undone. Type <strong>{user.username}</strong> to confirm.
              </p>
              <input
                className="w-full max-w-xs rounded border px-2 py-1" value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off" data-testid="delete-confirm-input"
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
          data-testid="delete-confirm"
        >
          Delete permanently
        </button>
        <button type="button" onClick={onCancel} className="rounded border px-3 py-2 text-sm"
                data-testid="delete-cancel">
          Cancel
        </button>
      </div>
    </div>
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

/**
 * Store/City/Zone scope: which Stores this account may see and manage.
 *
 * An empty list means unrestricted - the same "no rows means no
 * restriction" rule the backend resolver follows, so clearing every row here
 * and saving genuinely returns the account to seeing every Store, not to an
 * error state.
 */
function ScopeEditor({ user, onCancel, onSaved }) {
  const [entries, setEntries] = React.useState(null);
  const [stores, setStores] = React.useState([]);
  const [draftType, setDraftType] = React.useState("STORE");
  const [draftStoreId, setDraftStoreId] = React.useState("");
  const [draftValue, setDraftValue] = React.useState("");
  const [loadError, setLoadError] = React.useState("");
  const [saveError, setSaveError] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoadError("");
    try {
      const [{ data: scope }, { data: storeList }] = await Promise.all([
        api.get(`/users/${user.id}/store-scope`),
        api.get("/stores", { params: { include_inactive: true, include_archived: true } }),
      ]);
      setEntries(scope.entries);
      setStores(storeList);
      if (storeList.length) setDraftStoreId(String(storeList[0].id));
    } catch (failure) {
      setLoadError(
        failure?.response?.status === 403
          ? "You do not have permission to view or change this account's scope."
          : "This account's scope could not be loaded."
      );
    }
  }, [user.id]);

  React.useEffect(() => { load(); }, [load]);

  const addEntry = () => {
    if (draftType === "STORE") {
      if (!draftStoreId) return;
      const store = stores.find((s) => String(s.id) === draftStoreId);
      if (!store) return;
      if (entries.some((e) => e.scope_type === "STORE" && String(e.store_id) === draftStoreId)) return;
      setEntries([...entries, { scope_type: "STORE", store_id: store.id, scope_value: null,
                                 _label: `${store.store_name} (${store.store_code})` }]);
    } else {
      const value = draftValue.trim();
      if (!value) return;
      if (entries.some((e) => e.scope_type === draftType && e.scope_value === value)) return;
      setEntries([...entries, { scope_type: draftType, store_id: null, scope_value: value }]);
      setDraftValue("");
    }
  };

  const removeEntry = (index) => setEntries(entries.filter((_, i) => i !== index));

  const save = async () => {
    setSaving(true);
    setSaveError("");
    setSaved(false);
    try {
      const payload = {
        entries: entries.map((e) => ({
          scope_type: e.scope_type, store_id: e.store_id, scope_value: e.scope_value,
        })),
      };
      await api.put(`/users/${user.id}/store-scope`, payload);
      setSaved(true);
      await load();
    } catch (failure) {
      setSaveError(failure?.response?.data?.detail || "This account's scope could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  const describeEntry = (entry) => {
    if (entry.scope_type === "STORE") {
      if (entry._label) return `Store: ${entry._label}`;
      const store = stores.find((s) => s.id === entry.store_id);
      return `Store: ${store ? `${store.store_name} (${store.store_code})` : entry.store_id}`;
    }
    return entry.scope_type === "CITY" ? `City: ${entry.scope_value}` : `Zone: ${entry.scope_value}`;
  };

  return (
    <div className="fixed inset-0 z-50 bg-black/40 flex items-center justify-center p-4"
         data-testid="scope-editor">
      <div className="bg-white rounded-md shadow-xl max-w-lg w-full max-h-[85vh] flex flex-col">
        <div className="p-5 border-b flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-widest text-slate-500">User</div>
            <div className="text-lg font-semibold">{user.display_name}</div>
            <p className="mt-1 text-xs text-slate-500">
              No entries means unrestricted — this account sees every Store. Adding an entry
              limits it to only what is listed here.
            </p>
          </div>
          <button type="button" onClick={onCancel} className="text-slate-500 hover:text-slate-900"
                  data-testid="scope-close">✕</button>
        </div>

        <div className="p-5 overflow-y-auto space-y-4">
          {loadError && <p className="text-sm text-red-700" data-testid="scope-load-error">{loadError}</p>}
          {entries === null && !loadError && (
            <p className="text-sm text-slate-500" data-testid="scope-loading">Loading scope…</p>
          )}

          {entries && (
            <>
              <div className="space-y-1">
                {entries.length === 0 && (
                  <p className="text-sm text-slate-500" data-testid="scope-empty">
                    Unrestricted — no Store/City/Zone limits.
                  </p>
                )}
                {entries.map((entry, index) => (
                  <div key={index} className="flex items-center justify-between border rounded px-3 py-2"
                       data-testid={`scope-entry-${index}`}>
                    <span className="text-sm">{describeEntry(entry)}</span>
                    <button type="button" onClick={() => removeEntry(index)}
                            className="text-xs text-red-700 hover:underline"
                            data-testid={`scope-remove-${index}`}>
                      Remove
                    </button>
                  </div>
                ))}
              </div>

              <div className="border-t pt-3 space-y-2">
                <div className="text-xs font-bold uppercase tracking-widest text-slate-500">
                  Add assignment
                </div>
                <div className="flex gap-2">
                  <select className="border rounded px-2 py-1 text-sm bg-white" value={draftType}
                          data-testid="scope-add-type" onChange={(e) => setDraftType(e.target.value)}>
                    <option value="STORE">Store</option>
                    <option value="CITY">City</option>
                    <option value="REGION">Zone</option>
                  </select>
                  {draftType === "STORE" ? (
                    <select className="flex-1 border rounded px-2 py-1 text-sm bg-white"
                            value={draftStoreId} data-testid="scope-add-store"
                            onChange={(e) => setDraftStoreId(e.target.value)}>
                      {stores.map((s) => (
                        <option key={s.id} value={s.id}>{s.store_name} ({s.store_code})</option>
                      ))}
                    </select>
                  ) : (
                    <input className="flex-1 border rounded px-2 py-1 text-sm" value={draftValue}
                           data-testid="scope-add-value"
                           placeholder={draftType === "CITY" ? "City name" : "Zone name"}
                           onChange={(e) => setDraftValue(e.target.value)} />
                  )}
                  <button type="button" onClick={addEntry}
                          className="rounded border px-3 py-1 text-sm" data-testid="scope-add-btn">
                    Add
                  </button>
                </div>
              </div>
            </>
          )}
        </div>

        <div className="p-5 border-t space-y-2">
          {saveError && <p className="text-sm text-red-700" data-testid="scope-save-error">{saveError}</p>}
          {saved && !saveError && (
            <p className="text-sm text-emerald-700" data-testid="scope-save-success">Scope saved.</p>
          )}
          <div className="flex gap-2">
            <button type="button" onClick={save} disabled={saving || entries === null}
                    className="rounded bg-slate-900 px-3 py-2 text-sm text-white disabled:opacity-40"
                    data-testid="scope-save">
              {saving ? "Saving…" : "Save Changes"}
            </button>
            <button type="button" onClick={onCancel} className="rounded border px-3 py-2 text-sm"
                    data-testid="scope-cancel">
              Cancel
            </button>
            {saved && (
              <button type="button" onClick={onSaved} className="ml-auto rounded border px-3 py-2 text-sm"
                      data-testid="scope-done">
                Done
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
