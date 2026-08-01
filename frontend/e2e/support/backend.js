// A mocked EchoCast backend, shaped exactly like the real one.
//
// Every response here mirrors what backend/server.py actually returns, so a
// test that passes against these mocks is testing the real contract and not a
// convenient fiction. Where the shapes differ, the tests say so.

const UN = {
  id: 1,
  store_code: 'UN',
  store_name: 'Uttam Nagar Old',
  city: 'UN ZONE',
  region: 'UN ZONE',
  is_online_store: true,
  is_active: true,
  lifecycle_state: 'active',
  status: 'online',
};

const ASR = {
  id: 2,
  store_code: 'ASR',
  store_name: 'Uttam Nagar ASR',
  city: 'UN ZONE',
  region: 'UN ZONE',
  is_online_store: false,
  is_active: true,
  lifecycle_state: 'active',
  status: 'offline',
};

const DM = {
  id: 5,
  store_code: 'DM',
  store_name: 'Dwarka Mor',
  city: 'UN ZONE',
  region: 'UN ZONE',
  is_online_store: false,
  is_active: true,
  lifecycle_state: 'active',
  status: 'offline',
};

const STORES = [UN, ASR, DM];

// What GET /stores/{id}/receiver-devices/roles actually returns: no credential,
// no verifier, no key version. Three Devices, which is the approved per-Store
// limit - one legacy backfilled Device, one primary, one standby.
const PRIMARY_DEVICE = {
  public_id: '482f9e9b-3371-4c06-845f-202c34e661d0',
  display_name: 'UN till 1 (primary)',
  status: 'active',
  role: 'PRIMARY',
  enrolled_at: '2026-07-27T09:12:00+00:00',
  disabled_at: null,
  promoted_at: '2026-07-27T09:13:00+00:00',
};

const STANDBY_DEVICE = {
  public_id: '00875774-d573-4486-8fbf-473ea4d972fd',
  display_name: 'UN till 2 (standby)',
  status: 'active',
  role: 'STANDBY',
  enrolled_at: '2026-07-27T09:20:00+00:00',
  disabled_at: null,
  promoted_at: null,
};

const DEVICES = [PRIMARY_DEVICE, STANDBY_DEVICE];

// Never a real credential: an obviously fake, structureless string.
const FAKE_TOKEN = 'test-token-not-a-real-jwt';
const OPERATOR = { id: 1, username: 'pilot-operator', role: 'admin' };

// Mirrors backend/permission_catalog.py's DEFAULT_ROLE_PERMISSIONS exactly -
// GET /auth/permissions is what AuthContext.can() is built from, and every
// action button in the app is now gated by it, so a mock that returns nothing
// here would silently hide every button in every existing spec.
const ALL_PERMISSION_CODES = [
  'menu.broadcast.view', 'broadcast.start', 'broadcast.stop', 'broadcast.emergency_stop',
  'menu.stores.view', 'stores.create', 'stores.update', 'stores.archive',
  'menu.receivers.view', 'devices.enrollment.create', 'devices.primary.assign',
  'devices.rotate', 'devices.disable', 'devices.revoke', 'devices.archive',
  'devices.delete_permanently',
  'menu.history.view',
  'menu.logs.view',
  'menu.users.view', 'users.create', 'users.update', 'users.disable', 'users.permissions.manage',
];
const DEFAULT_ROLE_PERMISSIONS = {
  OWNER: ALL_PERMISSION_CODES,
  ADMIN: ALL_PERMISSION_CODES.filter(
    (c) => c !== 'users.permissions.manage' && c !== 'devices.delete_permanently'),
  BROADCASTER: ['menu.broadcast.view', 'broadcast.start', 'broadcast.stop',
                'broadcast.emergency_stop', 'menu.history.view', 'menu.receivers.view'],
  VIEWER: ['menu.broadcast.view', 'menu.stores.view', 'menu.receivers.view',
           'menu.history.view', 'menu.logs.view'],
};
function defaultPermissionsFor(role) {
  return DEFAULT_ROLE_PERMISSIONS[String(role || '').toUpperCase()] || [];
}

//: HQ accounts, in all three lifecycle states. Deliberately carries no
//: password_hash and no session_version: if the real API ever started sending
//: either, a test written against this fixture would not notice, so the fixture
//: must not model something the API must never do.
const HQ_USERS = [
  { id: 1, username: 'founder', display_name: 'The Founder', role: 'OWNER',
    is_active: true, lifecycle_state: 'active' },
  { id: 2, username: 'priya', display_name: 'Priya Sharma', role: 'ADMIN',
    is_active: true, lifecycle_state: 'active' },
  { id: 3, username: 'rahul', display_name: 'Rahul Verma', role: 'BROADCASTER',
    is_active: false, lifecycle_state: 'disabled' },
  { id: 4, username: 'anita', display_name: 'Anita Rao', role: 'VIEWER',
    is_active: false, lifecycle_state: 'archived' },
];

const json = (body, status = 200) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify(body),
});

/**
 * Install the backend mocks.
 *
 * `state` is live: a test can mutate state.current between assertions to model
 * a Receiver that acknowledges READY, then AUDIO_RECEIVING, then
 * PLAYBACK_CONFIRMED - which is the only honest way to drive those states,
 * because each one requires a real Receiver acknowledgement.
 */
async function mockBackend(page, options = {}) {
  const state = {
    stores: options.stores || STORES,
    loginStatus: options.loginStatus || 200,
    current: options.current || { live: false, session: null, targets: [], ready_receivers: [] },
    sessionId: 8,
    startCalls: [],
    stopCalls: [],
    ticketsIssued: 0,
    devices: options.devices || DEVICES.map((device) => ({ ...device })),
    deviceRolesStatus: options.deviceRolesStatus || 200,
    codesIssued: 0,
    rotations: [],
    promotions: [],
    disables: [],
    revokes: [],
    edits: [],
    transitions: [],
    regenerations: [],
    liveStoreIds: options.liveStoreIds || [],
    // Who is signed in. Defaults to the existing operator so every spec written
    // before User management keeps the behaviour it was written against.
    operator: options.operator || OPERATOR,
    // Explicit override for a test proving a per-user DENY/ALLOW - defaults
    // to the role's real default matrix, exactly like the backend resolver
    // does when there is no override row.
    permissions: options.permissions || defaultPermissionsFor((options.operator || OPERATOR).role),
    // Simulates the backend's independent enforcement (permission or Store
    // scope) refusing a request the frontend UI would otherwise have allowed
    // through - e.g. a hand-crafted fetch bypassing a hidden button. 200
    // means "let the mock's normal logic decide", matching every other
    // *Status option in this file.
    storeUpdateStatus: options.storeUpdateStatus || 200,
    sessionCreateStatus: options.sessionCreateStatus || 200,
    users: options.users || HQ_USERS.map((row) => ({ ...row })),
    usersStatus: options.usersStatus || 200,
    //: How many broadcast sessions each account is recorded as having started.
    //: Anything above zero must block a permanent delete.
    userHistory: options.userHistory || { 2: 3 },
    //: How many things still refer to each Store, by id. Store 1 (UN) has
    //: Devices; the others are untouched and therefore deletable.
    storeDependencies: options.storeDependencies || { 1: 2, 2: 0, 5: 0 },
    //: How long an issued enrolment code lives. Overridable so a spec can
    //: watch the real expiry transition instead of mocking the clock.
    enrolmentCodeSeconds: options.enrolmentCodeSeconds || 900,
    //: The authenticated enrollment records the page polls for. Empty by
    //: default: a page that has not yet learned anything must fall back to the
    //: clock, never the other way round.
    enrolmentRecords: options.enrolmentRecords || [],
    enrolmentCodeStatus: options.enrolmentCodeStatus || 200,
    userActions: [],
    passwordResets: [],
    passwordChanges: [],
  };

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^.*\/api/, '');
    const method = request.method();

    if (method === 'POST' && path === '/auth/login') {
      if (state.loginStatus === 429) {
        // What the backend actually sends when a burst is throttled or an
        // account is temporarily locked - the same response for both.
        return route.fulfill({
          status: 429,
          contentType: 'application/json',
          headers: { 'Retry-After': '900' },
          body: JSON.stringify({ detail: 'Too many sign-in attempts. Please try again later.' }),
        });
      }
      if (state.loginStatus !== 200) {
        return route.fulfill(json({ detail: 'Invalid username or password' }, state.loginStatus));
      }
      return route.fulfill(json({ access_token: FAKE_TOKEN, token_type: 'bearer', user: state.operator }));
    }

    if (method === 'GET' && path === '/auth/me') {
      return route.fulfill(json(state.operator));
    }

    if (method === 'GET' && path === '/auth/permissions') {
      return route.fulfill(json({ role: String(state.operator.role || '').toUpperCase(),
                                  permissions: state.permissions }));
    }

    // ---- HQ Users ---------------------------------------------------------
    if (method === 'POST' && path === '/auth/change-password') {
      const body = JSON.parse(request.postData() || '{}');
      state.passwordChanges.push(body);
      if (body.current_password !== 'correct-current-password') {
        return route.fulfill(json({ detail: 'That is not your current password.' }, 403));
      }
      return route.fulfill(json({ ok: true, sessions_ended: true }));
    }

    if (path.startsWith('/users')) {
      if (state.usersStatus !== 200) {
        return route.fulfill(json({ detail: 'You do not have permission to perform this action.' },
                                  state.usersStatus));
      }
      if (method === 'GET' && path === '/users') {
        return route.fulfill(json(state.users));
      }
      if (method === 'POST' && path === '/users') {
        const body = JSON.parse(request.postData() || '{}');
        if (state.users.some((row) => row.username.toLowerCase() === (body.username || '').toLowerCase())) {
          return route.fulfill(json({ detail: `The username '${body.username}' is already in use.` }, 409));
        }
        const created = {
          id: state.users.length + 1, username: body.username,
          display_name: body.display_name, role: body.role,
          is_active: true, lifecycle_state: 'active',
        };
        state.users.push(created);
        state.userActions.push({ action: 'create', body });
        return route.fulfill(json(created, 201));
      }

      // Dependency summary and the dependency-guarded permanent delete.
      // Mirrors backend/deletion_safety.py: an OWNER is never deletable, an
      // account with recorded history is refused with 409, and the username
      // must be typed exactly.
      const dependencyMatch = path.match(/^\/users\/(\d+)\/dependencies$/);
      if (method === 'GET' && dependencyMatch) {
        const row = state.users.find((c) => c.id === Number(dependencyMatch[1]));
        if (!row) return route.fulfill(json({ detail: 'No such HQ User' }, 404));
        const sessions = state.userHistory[row.id] || 0;
        return route.fulfill(json({
          counts: { broadcast_sessions: sessions },
          unchecked: [],
          total: sessions,
          deletable: sessions === 0 && row.role !== 'OWNER',
          explanation: sessions === 0
            ? 'Nothing refers to this record, so it can be removed.'
            : `This record still has ${sessions} broadcast sessions. Deleting it would `
              + 'destroy operational history. Archive it instead.',
        }));
      }

      const permanentMatch = path.match(/^\/users\/(\d+)\/permanently$/);
      if (method === 'DELETE' && permanentMatch) {
        const id = Number(permanentMatch[1]);
        const row = state.users.find((c) => c.id === id);
        if (!row) return route.fulfill(json({ detail: 'No such HQ User' }, 404));
        const confirm = url.searchParams.get('confirm');
        if (row.role === 'OWNER') {
          return route.fulfill(json({
            detail: 'An OWNER account is never hard-deleted. Archive it instead.' }, 409));
        }
        if (id === state.operator.id) {
          return route.fulfill(json({ detail: 'You cannot delete your own account.' }, 409));
        }
        if (confirm !== row.username) {
          return route.fulfill(json({
            detail: `The typed confirmation did not match. Type the username exactly: ${row.username}` }, 409));
        }
        if ((state.userHistory[id] || 0) > 0) {
          return route.fulfill(json({
            detail: 'This account is recorded as the actor in operational history. '
                  + 'Archive it instead.' }, 409));
        }
        state.users = state.users.filter((c) => c.id !== id);
        state.userActions.push({ action: 'delete', id });
        return route.fulfill(json({ ok: true, deleted: { id, username: row.username } }));
      }

      // Rights editor: GET/PUT /users/{id}/permissions. Minimal shape - just
      // enough for the SUPER ADMIN-only visibility and round-trip to be
      // provable in a real browser; the resolver logic itself is proven in
      // the backend suite, not re-implemented here.
      const rightsMatch = path.match(/^\/users\/(\d+)\/permissions$/);
      if (rightsMatch) {
        const id = Number(rightsMatch[1]);
        const row = state.users.find((candidate) => candidate.id === id);
        if (!row) return route.fulfill(json({ detail: 'No such HQ User' }, 404));
        if (row.role === 'OWNER') {
          return route.fulfill(json({ detail: 'OWNER permissions cannot be overridden.' }, 409));
        }
        state.userRightsOverrides = state.userRightsOverrides || {};
        const overrides = state.userRightsOverrides[id] || {};
        if (method === 'PUT') {
          const body = JSON.parse(request.postData() || '{}');
          for (const change of body.changes || []) overrides[change.code] = change.effect;
          state.userRightsOverrides[id] = overrides;
        }
        const roleDefaults = defaultPermissionsFor(row.role);
        const catalogLabels = {
          'stores.update': ['Stores', 'Edit Store'],
          'stores.create': ['Stores', 'Create Store'],
          'broadcast.start': ['Broadcast', 'Start Broadcast'],
        };
        const permissions = ALL_PERMISSION_CODES.map((code) => {
          const roleAllowed = roleDefaults.includes(code);
          const override = overrides[code] || 'INHERIT';
          const effective = override === 'ALLOW' ? true : override === 'DENY' ? false : roleAllowed;
          const [group, label] = catalogLabels[code] || [code.split('.')[0], code];
          return { code, group, label, role_allowed: roleAllowed, override, effective };
        });
        return route.fulfill(json({ user_id: id, role: row.role, permissions }));
      }

      // Scope editor: GET/PUT /users/{id}/store-scope.
      const scopeMatch = path.match(/^\/users\/(\d+)\/store-scope$/);
      if (scopeMatch) {
        const id = Number(scopeMatch[1]);
        const row = state.users.find((candidate) => candidate.id === id);
        if (!row) return route.fulfill(json({ detail: 'No such HQ User' }, 404));
        state.userScope = state.userScope || {};
        if (method === 'PUT') {
          const body = JSON.parse(request.postData() || '{}');
          state.userScope[id] = body.entries || [];
        }
        return route.fulfill(json({ user_id: id, entries: state.userScope[id] || [] }));
      }

      const match = path.match(/^\/users\/(\d+)(?:\/([a-z-]+))?$/);
      if (match) {
        const id = Number(match[1]);
        const action = match[2];
        const row = state.users.find((candidate) => candidate.id === id);
        if (!row) return route.fulfill(json({ detail: 'No such HQ User' }, 404));

        if (action === 'reset-password') {
          state.passwordResets.push({ id });
          return route.fulfill(json({
            user_id: id, sessions_ended: true,
            detail: 'The password was set and every existing session for that account ended.',
          }));
        }
        if (action === 'role') {
          const body = JSON.parse(request.postData() || '{}');
          row.role = body.role;
          state.userActions.push({ action: 'role', id, role: body.role });
          return route.fulfill(json(row));
        }
        if (method === 'PATCH') {
          const body = JSON.parse(request.postData() || '{}');
          Object.assign(row, body);
          state.userActions.push({ action: 'edit', id, body });
          return route.fulfill(json(row));
        }
        // The refusals the real backend enforces, mirrored so a test cannot
        // pass here and fail against the server.
        const activeSuperAdmins = state.users.filter(
          (candidate) => candidate.role === 'OWNER' && candidate.lifecycle_state === 'active');
        if ((action === 'disable' || action === 'archive')
            && row.role === 'OWNER' && activeSuperAdmins.length <= 1) {
          return route.fulfill(json({
            detail: 'This is the only active SUPER_ADMIN. That would leave nobody able to administer EchoCast.',
          }, 409));
        }
        if ((action === 'disable' || action === 'archive') && id === state.operator.id) {
          return route.fulfill(json({ detail: 'You cannot do that to your own account.' }, 409));
        }
        if (action === 'enable' && row.lifecycle_state === 'archived') {
          return route.fulfill(json({
            detail: 'An account that is archived cannot become active.' }, 409));
        }
        const next = { disable: 'disabled', enable: 'active',
                       archive: 'archived', restore: 'disabled' }[action];
        if (next) {
          row.lifecycle_state = next;
          row.is_active = next === 'active';
          state.userActions.push({ action, id });
          return route.fulfill(json(row));
        }
      }
      return route.fulfill(json({ detail: 'Not found' }, 404));
    }

    if (method === 'POST' && path === '/auth/ws-ticket') {
      // A fresh opaque value each time, as the real endpoint does. Never a JWT.
      state.ticketsIssued += 1;
      return route.fulfill(json({ ticket: `test-ticket-${state.ticketsIssued}`, expires_in: 20 }));
    }

    // Store dependency summary and permanent delete. Mirrors
    // backend/deletion_safety.py: anything with Devices, targets, events or
    // enrolment codes is refused with 409, and the short code must be typed.
    const storeDependencies = path.match(/^\/stores\/(\d+)\/dependencies$/);
    if (method === 'GET' && storeDependencies) {
      const store = state.stores.find((s) => s.id === Number(storeDependencies[1]));
      if (!store) return route.fulfill(json({ detail: 'Store not found' }, 404));
      const devices = state.devices.filter(() => store.id === 1).length;
      const total = state.storeDependencies[store.id] ?? devices;
      return route.fulfill(json({
        counts: { receiver_devices: total, broadcast_targets: 0,
                  receiver_events: 0, receiver_enrollment_codes: 0 },
        unchecked: [],
        total,
        deletable: total === 0,
        explanation: total === 0
          ? 'Nothing refers to this record, so it can be removed.'
          : `This record still has ${total} receiver devices. Deleting it would `
            + 'destroy operational history. Archive it instead.',
      }));
    }

    const storePermanent = path.match(/^\/stores\/(\d+)\/permanently$/);
    if (method === 'DELETE' && storePermanent) {
      const id = Number(storePermanent[1]);
      const store = state.stores.find((s) => s.id === id);
      if (!store) return route.fulfill(json({ detail: 'Store not found' }, 404));
      const total = state.storeDependencies[id] ?? (id === 1 ? state.devices.length : 0);
      if (url.searchParams.get('confirm') !== store.store_code) {
        return route.fulfill(json({
          detail: `The typed confirmation did not match. Type the Store code exactly: ${store.store_code}` }, 409));
      }
      if (total > 0) {
        return route.fulfill(json({
          detail: 'This Store contains operational history or Receiver Devices. '
                + 'Archive it instead.' }, 409));
      }
      state.stores = state.stores.filter((s) => s.id !== id);
      state.transitions.push({ action: 'delete', id });
      return route.fulfill(json({ ok: true, deleted: { id, store_code: store.store_code } }));
    }

    if (method === 'GET' && path === '/stores') {
      // The real backend hides archived Stores unless asked. Mirror that, so a
      // test cannot pass here and fail against the server.
      const includeArchived = url.searchParams.get('include_archived') === 'true';
      const visible = includeArchived
        ? state.stores
        : state.stores.filter((s) => (s.lifecycle_state || 'active') !== 'archived');
      return route.fulfill(json(visible));
    }

    if (method === 'PUT' && /^\/stores\/\d+$/.test(path)) {
      if (state.storeUpdateStatus !== 200) {
        return route.fulfill(json(
          { detail: 'You do not have permission to perform this action.' },
          state.storeUpdateStatus,
        ));
      }
      const id = Number(path.split('/')[2]);
      const payload = request.postDataJSON();
      if ('receiver_token' in payload || 'is_active' in payload) {
        return route.fulfill(json({ detail: 'unexpected field' }, 422));
      }
      const clash = state.stores.find(
        (s) => s.id !== id && s.store_code === payload.store_code,
      );
      if (clash) return route.fulfill(json({ detail: 'store_code already exists' }, 409));
      state.edits.push({ id, payload });
      state.stores = state.stores.map((s) => (s.id === id ? { ...s, ...payload } : s));
      return route.fulfill(json(state.stores.find((s) => s.id === id)));
    }

    // Lifecycle. Note what each transition does to is_active: the real backend
    // keeps the two in lockstep, and the page reads both.
    const lifecycle = path.match(/^\/stores\/(\d+)\/(disable|enable|archive|restore)$/);
    if (method === 'POST' && lifecycle) {
      const id = Number(lifecycle[1]);
      const action = lifecycle[2];
      const store = state.stores.find((s) => s.id === id);
      if (!store) return route.fulfill(json({ detail: 'Store not found' }, 404));
      const current = store.lifecycle_state || (store.is_active ? 'active' : 'disabled');

      if (state.liveStoreIds.includes(id) && (action === 'disable' || action === 'archive')) {
        return route.fulfill(
          json({ detail: 'this Store is part of a live broadcast; stop the broadcast first' }, 409),
        );
      }
      if (action === 'enable' && current === 'archived') {
        return route.fulfill(json({ detail: 'restore it first' }, 409));
      }
      if (action === 'restore' && current !== 'archived') {
        return route.fulfill(json({ detail: 'only an archived Store can be restored' }, 409));
      }
      // restore returns a Store to DISABLED, never straight to ACTIVE.
      const next = { disable: 'disabled', enable: 'active', archive: 'archived', restore: 'disabled' }[action];
      state.transitions.push({ id, action, to: next });
      state.stores = state.stores.map((s) =>
        s.id === id ? { ...s, lifecycle_state: next, is_active: next === 'active' } : s,
      );
      return route.fulfill(json(state.stores.find((s) => s.id === id)));
    }

    if (method === 'POST' && /^\/stores\/\d+\/regenerate-token$/.test(path)) {
      const id = Number(path.split('/')[2]);
      state.regenerations.push(id);
      // Secret-free, exactly like the real StoreOut.
      return route.fulfill(json(state.stores.find((s) => s.id === id)));
    }

    if (method === 'GET' && path === '/stores/meta/regions-cities') {
      return route.fulfill(json({ regions: ['UN ZONE'], cities: ['UN ZONE'] }));
    }

    if (method === 'GET' && path === '/broadcast/current') {
      return route.fulfill(json(state.current));
    }

    if (method === 'POST' && path === '/broadcast/sessions') {
      if (state.sessionCreateStatus !== 200) {
        return route.fulfill(json(
          { detail: 'You do not have permission to perform this action.' },
          state.sessionCreateStatus,
        ));
      }
      const payload = request.postDataJSON();
      state.startCalls.push(payload);
      return route.fulfill(json({ id: state.sessionId, campaign_name: payload.campaign_name, status: 'pending' }));
    }

    if (method === 'POST' && /^\/broadcast\/sessions\/\d+\/start$/.test(path)) {
      return route.fulfill(json({ ok: true }));
    }

    if (method === 'POST' && /^\/broadcast\/sessions\/\d+\/stop$/.test(path)) {
      state.stopCalls.push(path);
      state.current = { live: false, session: null, targets: [], ready_receivers: [] };
      return route.fulfill(json({ ok: true }));
    }

    // ---- Receiver Devices -------------------------------------------------
    // Shaped exactly like receiver_primary_device.describe_store_devices and the
    // two one-time-delivery responses. Neither the code nor the credential is
    // ever returned by a GET here, because neither is by the real backend.
    if (method === 'GET' && /^\/stores\/\d+\/receiver-devices\/roles$/.test(path)) {
      if (state.deviceRolesStatus !== 200) {
        return route.fulfill(json({ detail: 'unavailable' }, state.deviceRolesStatus));
      }
      return route.fulfill(json(state.devices));
    }

    if (method === 'POST' && path === '/receiver-devices/enrollment-codes') {
      state.codesIssued += 1;
      return route.fulfill(
        json({ code: `ECHO-CODE-${state.codesIssued}`, store_id: 1, expires_in_seconds: state.enrolmentCodeSeconds }),
      );
    }

    // The authenticated enrollment record. Shaped exactly like
    // schemas.EnrollmentCodeStatusOut: state, timestamps, the Device the code
    // produced, and evidence-backed stages - and deliberately no code and no
    // hash, because the real endpoint returns neither.
    if (method === 'GET' && /^\/stores\/\d+\/enrollment-codes$/.test(path)) {
      if (state.enrolmentCodeStatus !== 200) {
        return route.fulfill(json({ detail: 'unavailable' }, state.enrolmentCodeStatus));
      }
      return route.fulfill(json(state.enrolmentRecords));
    }

    if (method === 'POST' && /^\/receiver-devices\/[^/]+\/rotate-credential$/.test(path)) {
      const publicId = path.split('/')[2];
      state.rotations.push(publicId);
      return route.fulfill(
        json({
          device_public_id: publicId,
          credential: `echocast_rcv_v2.${publicId}.rotated-secret-shown-once`,
          credential_version: 2,
          store_id: 1,
          previous_credential_retired: true,
        }),
      );
    }

    if (method === 'POST' && /^\/receiver-devices\/[^/]+\/promote$/.test(path)) {
      const publicId = path.split('/')[2];
      state.promotions.push(publicId);
      state.devices = state.devices.map((device) => ({
        ...device,
        role: device.public_id === publicId ? 'PRIMARY' : 'STANDBY',
      }));
      return route.fulfill(json(state.devices));
    }

    if (method === 'POST' && /^\/receiver-devices\/[^/]+\/(disable|revoke)$/.test(path)) {
      const [, , publicId, action] = path.split('/');
      state[action === 'disable' ? 'disables' : 'revokes'].push(publicId);
      state.devices = state.devices.map((device) =>
        device.public_id === publicId
          ? {
              ...device,
              status: action === 'disable' ? 'disabled' : 'retired',
              // Losing a primary never promotes anything: the backend clears the
              // role and leaves the Store without one until an admin chooses.
              role: 'STANDBY',
            }
          : device,
      );
      return route.fulfill(json(state.devices));
    }

    if (method === 'POST' && /^\/receiver-devices\/[^/]+\/archive$/.test(path)) {
      const publicId = path.split('/')[2];
      const now = '2026-08-01T12:00:00+00:00';
      state.devices = state.devices.map((device) =>
        device.public_id === publicId
          ? { ...device, status: 'disabled', archived_at: now, role: 'STANDBY' }
          : device,
      );
      return route.fulfill(json(state.devices.find((d) => d.public_id === publicId)));
    }

    if (method === 'POST' && /^\/receiver-devices\/[^/]+\/restore$/.test(path)) {
      const publicId = path.split('/')[2];
      state.devices = state.devices.map((device) =>
        device.public_id === publicId ? { ...device, archived_at: null } : device,
      );
      return route.fulfill(json(state.devices.find((d) => d.public_id === publicId)));
    }

    if (method === 'GET' && /^\/receiver-devices\/[^/]+\/dependencies$/.test(path)) {
      const publicId = path.split('/')[2];
      return route.fulfill(json({
        counts: {}, unchecked: [], total: 0, deletable: true,
        explanation: 'Nothing refers to this record, so it can be removed.',
      }));
    }

    if (method === 'DELETE' && /^\/receiver-devices\/[^/]+\/permanently$/.test(path)) {
      const publicId = path.split('/')[2];
      const confirm = url.searchParams.get('confirm');
      if (confirm !== publicId) {
        return route.fulfill(json({ detail: 'The typed confirmation did not match.' }, 409));
      }
      state.devices = state.devices.filter((device) => device.public_id !== publicId);
      return route.fulfill(json({ ok: true, deleted: { public_id: publicId } }));
    }

    // Anything unmocked is a bug in the test, not something to paper over.
    return route.fulfill(json({ detail: `unmocked ${method} ${path}` }, 501));
  });

  return state;
}

/** Put a signed-in operator in place without going through the login form. */
async function signIn(page) {
  await page.addInitScript((token) => {
    window.localStorage.setItem('echocast_token', token);
  }, FAKE_TOKEN);
}

/**
 * Count getUserMedia calls and optionally deny them.
 *
 * This is the instrument for the single most important assertion in the suite:
 * the microphone must not be opened before a Receiver acknowledges READY.
 */
async function instrumentMicrophone(page, { deny = false } = {}) {
  await page.addInitScript((shouldDeny) => {
    window.__micCalls = 0;
    window.__trackStops = 0;
    window.__recorderStops = 0;

    const media = navigator.mediaDevices;
    const original = media.getUserMedia.bind(media);
    media.getUserMedia = (constraints) => {
      window.__micCalls += 1;
      if (shouldDeny) {
        const error = new Error('Permission denied');
        error.name = 'NotAllowedError';
        return Promise.reject(error);
      }
      return original(constraints);
    };

    // Releasing the microphone track is what actually turns off the browser's
    // recording indicator. Stopping the recorder alone does not.
    const trackStop = MediaStreamTrack.prototype.stop;
    MediaStreamTrack.prototype.stop = function patchedStop(...args) {
      window.__trackStops += 1;
      return trackStop.apply(this, args);
    };

    const recorderStop = MediaRecorder.prototype.stop;
    MediaRecorder.prototype.stop = function patchedStop(...args) {
      window.__recorderStops += 1;
      return recorderStop.apply(this, args);
    };
  }, deny);
}

/** Replace WebSocket with one that opens immediately and swallows sends. */
async function stubWebSocket(page) {
  await page.addInitScript(() => {
    window.__wsUrls = [];
    class FakeWebSocket {
      constructor(url) {
        window.__wsUrls.push(String(url));
        this.url = String(url);
        this.readyState = 0;
        this.binaryType = 'blob';
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        this.onclose = null;
        setTimeout(() => {
          this.readyState = 1;
          if (this.onopen) this.onopen({ type: 'open' });
        }, 0);
      }
      send() { /* the byte path is proven by the hardware pilot, not here */ }
      close() {
        this.readyState = 3;
        if (this.onclose) this.onclose({ type: 'close' });
      }
    }
    FakeWebSocket.CONNECTING = 0;
    FakeWebSocket.OPEN = 1;
    FakeWebSocket.CLOSING = 2;
    FakeWebSocket.CLOSED = 3;
    window.WebSocket = FakeWebSocket;
  });
}

/** Make MediaRecorder report that no WebM/Opus variant is supported. */
async function removeOpusSupport(page) {
  await page.addInitScript(() => {
    window.MediaRecorder.isTypeSupported = () => false;
  });
}

module.exports = {
  mockBackend,
  signIn,
  instrumentMicrophone,
  stubWebSocket,
  removeOpusSupport,
  STORES,
  UN,
  ASR,
  DM,
  DEVICES,
  HQ_USERS,
  OPERATOR,
  PRIMARY_DEVICE,
  STANDBY_DEVICE,
  FAKE_TOKEN,
  OPERATOR,
};
