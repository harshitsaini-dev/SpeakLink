# Archive vs Permanent Delete

Two different things an operator can do to an HQ account, and the guarantees
each one carries.

## Archive — reversible

* the row stays in `hq_users`, `lifecycle_state = 'archived'`
* the account cannot sign in
* it can be restored (which returns it as **disabled**; enabling is a second,
  deliberate step)
* it keeps its user ID and its historical relationships
* **its username stays reserved** — creating another account with that name is
  refused, because the account may come back

Archive is the right choice when somebody has left but might return, or when
an account is being retired and you want the option to undo it.

## Permanent Delete — irreversible

* the `hq_users` row is **deleted**
* the account is absent from User Management and from every search, with no
  filter that can reveal it, because there is nothing left to reveal
* it cannot be restored, and has no Rights, Scope or Reset Password actions,
  because there is no account to act on
* its permission overrides and Store Scope are **deleted with it**
* **its username becomes available** for a completely new account
* the new account is a **different identity**: a new ID, a new password, no
  inherited rights, no inherited scope

Historical records survive. Broadcast History, the administrative audit and
the deletion audit all remain readable.

## What this replaced, and why

Permanent deletion used to be a *tombstone*: the row stayed and was marked
deleted. That kept history readable, but it also kept the username reserved
for ever, so an operator saw this:

```
User Management
  admin          ADMIN          permanently deleted     [Rights] [Scope] [Reset Password]

New User -> username: admin
  "The username 'admin' is already in use."
```

An account that still occupies the namespace and still has actions has not
been deleted; it has been hidden. Filtering it out of the list in React would
have left exactly that bug in place, so the fix is in the database semantics.

## How history stays readable without the account

Each broadcast now carries an immutable snapshot of who ran it
(`started_by_username`, `started_by_display_name`), written in the same
transaction that deletes the account. The foreign key `started_by` is set to
NULL.

**Nulling the pointer is not tidiness, it is the whole safety property.**
`hq_users.id` was `INTEGER PRIMARY KEY` with no `AUTOINCREMENT`, so SQLite
handed out `max(id) + 1` — delete the highest-numbered account and the *next*
account created received that exact id, inheriting every history row still
pointing at it. In the live database, broadcast session #2 was started by user
id 3; deleting id 3 and creating anybody else would have transferred that
broadcast to a different human being with no error anywhere.

Two independent defences now exist:

1. ownership is a **snapshot**, not a join, so a reused id proves nothing;
2. `hq_users` uses `AUTOINCREMENT`, so a released id is **never reissued**.

Audit tables (`permission_audit_events`, `store_scope_audit_events`) keep their
rows with the actor set to NULL; the deleted account's identity is recoverable
from `user_deletion_events`, which records the old ID, username and role.

## Safety rules

* the **last active SUPER ADMIN / OWNER cannot be deleted** — there would be
  nobody able to administer EchoCast, and that cannot be undone from inside the
  product
* **you cannot delete the account you are signed in as** — another sufficiently
  authorised administrator must do it
* the permission required is `users.delete_permanently`, which ADMIN does not
  hold by default
* the username must be typed exactly, and the consequence acknowledged
* the whole thing is one transaction: any failure rolls back, so there is no
  half-deleted account, no orphaned rights, and no history that has lost its
  owner

## Existing tombstones

Databases created before this change may still contain accounts marked
`lifecycle_state = 'deleted'`. Startup completes that decision once: it
snapshots their history, removes their live security state, deletes the row and
releases the username.

It touches **only** `lifecycle_state = 'deleted'`. Active and archived accounts
are never purged — an archived account is restorable and deleting one would
destroy something an operator deliberately kept. The migration is idempotent:
once the rows are gone there is nothing left to match, and no second audit row
is written.
