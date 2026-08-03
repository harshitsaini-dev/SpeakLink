# Archive vs Permanent Delete — Stores

The same distinction as [USER_ACCOUNT_DELETION.md](USER_ACCOUNT_DELETION.md),
applied to Stores, and enforced the same way: in the database, not the
interface.

## Archive — reversible

* the row stays in `stores`, `lifecycle_state = 'archived'`
* it cannot be a broadcast target and its Receivers stop connecting
* it can be restored (returning it as **disabled**; re-enabling is a second,
  deliberate step)
* it keeps its Store ID and every historical relationship
* **its Store Code stays reserved** — creating another Store with that code is
  refused, because this one may come back

## Permanent Delete — irreversible

* the `stores` row is **deleted**
* it is absent from Store Management and every search, under every lifecycle
  selection, with no filter that can reveal it
* it cannot be restored, and offers no Edit, Enable, Disable, Archive or
  Receiver Device action, because there is no Store to act on
* its Receiver Devices are **retired**, their credentials **revoked**, its
  pending enrolment codes backdated, and its `receiver_token` dies with the row
* its live operational state — broadcast leases, Store Scope assignments,
  primary-device assignment — is deleted with it
* **its Store Code becomes available** for a new Store

Historical records survive: Broadcast Targets, Receiver events, Receiver
Devices and the deletion audit all remain readable.

## Same Store Code is NOT the same Store

This is the security property the whole design turns on. A new Store created
with a deleted Store's code:

* gets a **new Store ID**
* inherits **no** Receiver Device, credential, primary assignment, enrolment
  code, Store Scope or lease
* gets its **own** `receiver_token`

A Receiver still holding the old Store's credential **cannot authenticate as
the new Store**. The credential is revoked by *status*, not merely stamped with
a time — the status is what the authentication path filters on.

## What this replaced, and why

Permanent deletion used to be a *tombstone*: the row stayed, marked deleted.
`store_deletion.py` said so plainly — the Store Code was "never handed out to a
new Store afterward". So an operator saw this:

```
Store Management  ->  permanently delete AYUSHK  ->  it disappears
Add Store         ->  store_code AYUSHK          ->  "store_code already exists"
```

A Store that still occupies the code namespace has not been deleted; it has
been hidden.

## How history stays readable without the Store

Each Broadcast Target, Receiver event and Receiver Device carries an immutable
snapshot of the Store Code (and, for targets, the Store name), written in the
same transaction that deletes the Store. The foreign key is then set to NULL.

**Nulling the pointer is the safety property, not tidiness.** `stores.id` was
`INTEGER PRIMARY KEY` with no `AUTOINCREMENT`, so SQLite hands out
`max(id) + 1`. In the live database the tombstones were ids 58, 59 and 60 —
and 60 *was* the maximum, so deleting it and adding any Store would have given
the replacement id 60 along with every history row still pointing there.

Two independent defences now exist:

1. history names the Store by **snapshot**, not by a join, so a reused id
   proves nothing;
2. `stores` uses `AUTOINCREMENT`, so a released id is **never reissued**.

Broadcast History renders a deleted Store's target from the snapshot, never by
looking the code up — a *different* Store may now be using it.

## Safety rules

* **refused while a broadcast is on air** on that Store, checked against both
  the runtime and the lease table. Deleting a Store must never silence somebody
  else's announcement as a side effect — stop the broadcast first.
* the Store Code must be typed exactly, and the consequence acknowledged
* the permission required is `stores.delete_permanently` (SUPER ADMIN/OWNER)
* one transaction: any failure rolls back, so there is no half-deleted Store,
  no Devices left revoked beside a Store that still exists, and no history that
  has lost its Store

## The canonical Store catalog

Two behaviours that are easy to get backwards, and both are tested:

* a **fresh database** still receives all 44 canonical Stores on first boot;
* an **ordinary restart of an existing database** does **not** recreate a Store
  an operator permanently deleted. `seed_stores` is a first-run bootstrap — it
  inserts only when the Store table is empty — so the operator's decision
  stands across every restart.

## Existing tombstones

Databases created before this change may still contain Stores marked
`lifecycle_state = 'deleted'`. Startup completes that decision once: snapshots
their history, neutralises their Receiver identity, deletes the row and
releases the code.

It touches **only** `lifecycle_state = 'deleted'`. Active, disabled and
archived Stores are never purged. The migration is idempotent.
