// A mocked SpeakLink backend, shaped exactly like the real one.
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
  'broadcast.view_ownership', 'broadcast.active_view', 'broadcast.view_targets',
  'broadcast.stop_any',
  'menu.stores.view', 'stores.create', 'stores.update', 'stores.archive',
  'stores.delete_permanently',
  'menu.receivers.view', 'devices.enrollment.create', 'devices.primary.assign',
  'devices.rotate', 'devices.disable', 'devices.revoke', 'devices.archive',
  'devices.delete_permanently',
  'menu.history.view', 'broadcast_history.archive', 'broadcast_history.delete_permanently',
  'menu.logs.view', 'system_logs.archive', 'system_logs.delete_permanently',
  'menu.users.view', 'users.create', 'users.update', 'users.disable', 'users.permissions.manage',
  'users.delete_permanently',
];

/** The codes an ADMIN never holds by default. Mirrors permission_catalog.DESTRUCTIVE_CODES. */
const DESTRUCTIVE_CODES = [
  'stores.delete_permanently', 'devices.delete_permanently', 'users.delete_permanently',
  'broadcast_history.delete_permanently', 'system_logs.delete_permanently',
];
const DEFAULT_ROLE_PERMISSIONS = {
  OWNER: ALL_PERMISSION_CODES,
  ADMIN: ALL_PERMISSION_CODES.filter(
    (c) => c !== 'users.permissions.manage' && !DESTRUCTIVE_CODES.includes(c)),
  // broadcast.emergency_stop, broadcast.view_ownership and the three Active
  // Broadcast supervision codes are NOT here, and must not drift back: one
  // stops every other operator's broadcast, one reveals whose broadcast holds
  // a Store, and the others open, expose and interrupt other operators' work.
  // Mirrors permission_catalog.DEFAULT_ROLE_PERMISSIONS.
  BROADCASTER: ['menu.broadcast.view', 'broadcast.start', 'broadcast.stop',
                'menu.history.view', 'menu.receivers.view',
                'menu.stores.view'],
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

//: Structured System Log entries and Broadcast sessions, in the shape the
//: /search endpoints return - including archived_at, which the row itself
//: must carry so the UI can tell an archived row from a live one while
//: showing both.
const LOG_ENTRIES = [
  { id: 101, level: 'info', message: 'Broadcast started for UN', created_at: '2026-08-01T09:00:00+00:00',
    actor_user_id: 1, store_id: 1, device_public_id: null, archived_at: null },
  { id: 102, level: 'warn', message: 'Receiver ASR heartbeat late', created_at: '2026-08-01T09:05:00+00:00',
    actor_user_id: null, store_id: 2, device_public_id: null, archived_at: null },
  { id: 103, level: 'error', message: 'Playback failed at Dwarka Mor', created_at: '2026-08-01T09:10:00+00:00',
    actor_user_id: 2, store_id: 5, device_public_id: null, archived_at: null },
];

const HISTORY_SESSIONS = [
  { id: 8, campaign_name: 'Morning offer', started_by: 1, started_at: '2026-08-01T09:00:00+00:00',
    ended_at: '2026-08-01T09:02:00+00:00', status: 'completed', target_mode: 'selected',
    selected_store_count: 2, online_store_count: 1, offline_store_count: 1, notes: null,
    created_at: '2026-08-01T09:00:00+00:00', archived_at: null },
  { id: 9, campaign_name: 'Evening reminder', started_by: 2, started_at: '2026-08-01T18:00:00+00:00',
    ended_at: '2026-08-01T18:01:00+00:00', status: 'completed', target_mode: 'all',
    selected_store_count: 3, online_store_count: 1, offline_store_count: 2, notes: null,
    created_at: '2026-08-01T18:00:00+00:00', archived_at: null },
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
    // Live per-Store output state, keyed by store id. Seeded by the spec.
    audioControl: options.audioControl || {},
    audioCommandId: 0,
    // 'applied' (default), 'failed', 'unsupported', or 'none' for a Receiver
    // that never answers - the ACK-timeout case.
    audioControlAckResult: options.audioControlAckResult,
    audioControlSupported: options.audioControlSupported,
    audioControlOnline: options.audioControlOnline,
    audioControlSessionEnded: options.audioControlSessionEnded || false,
    storeUpdateStatus: options.storeUpdateStatus || 200,
    storesListStatus: options.storesListStatus || 200,
    // Per-user Store scope for the broadcast target catalog. null means
    // unrestricted, matching resolve_store_scope's own None-vs-empty rule:
    // an empty ARRAY is a real answer ("scoped to nothing"), never widened.
    scopedStoreIds: options.scopedStoreIds === undefined
      ? null : options.scopedStoreIds,
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
    logs: options.logs || LOG_ENTRIES.map((row) => ({ ...row })),
    sessions: options.sessions || HISTORY_SESSIONS.map((row) => ({ ...row })),
    //: Every bulk request the page sent, verbatim. This is how a spec proves
    //: Select All Filtered posted a FILTER rather than an enumerated id list -
    //: the distinction is invisible in the resulting list either way.
    bulkCalls: [],
    deletePermanentlyCalls: [],
    //: Live broadcasts, as GET /broadcast/active would report them.
    //: Each entry: {session_id, campaign_name, owner_username,
    //: owner_display_name, owner_user_id, started_at, target_store_ids}.
    activeSessions: options.activeSessions || [],
    //: Makes emergency stop answer EMERGENCY_STOP_INCOMPLETE.
    emergencyStopIncomplete: options.emergencyStopIncomplete || false,
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

    // ---- Server-side search, filtering and paging --------------------------
    // Shaped exactly like admin_search.Page.as_dict: items/total/page/
    // page_size/pages/has_more. The frontend reads has_more to enable Next,
    // so a mock that omitted it would make paging look broken here and work
    // against the real server, or the reverse.
    const paged = (rows) => {
      const page = Number(url.searchParams.get('page') || 1);
      const size = Number(url.searchParams.get('page_size') || 50);
      const start = (page - 1) * size;
      const items = rows.slice(start, start + size);
      return {
        items, total: rows.length, page, page_size: size,
        pages: Math.max(1, Math.ceil(rows.length / size)),
        has_more: start + size < rows.length,
      };
    };
    const q = (url.searchParams.get('q') || '').toLowerCase();
    const param = (name) => url.searchParams.get(name);
    const flag = (name) => param(name) === 'true';

    if (method === 'GET' && path === '/stores/filter-options') {
      const visible = state.stores.filter((x) => x.lifecycle_state !== 'deleted');
      return route.fulfill(json({
        regions: [...new Set(visible.map((x) => x.region))].sort(),
        cities: [...new Set(visible.map((x) => x.city))].sort(),
      }));
    }

    if (method === 'GET' && path === '/stores/search') {
      if (state.storesListStatus !== 200) {
        return route.fulfill(json({ detail: 'You do not have permission to view this.' },
                                  state.storesListStatus));
      }
      // A permanently deleted Store never appears, under any selection.
      let rows = state.stores.filter((x) => x.lifecycle_state !== 'deleted');
      // ONE exclusive lifecycle control, defaulting to active.
      const lifecycle = param('lifecycle') || 'active';
      if (lifecycle === 'active') {
        rows = rows.filter((x) => (x.lifecycle_state || (x.is_active ? 'active' : 'disabled')) === 'active');
      } else if (lifecycle === 'disabled') {
        rows = rows.filter((x) => (x.lifecycle_state || (x.is_active ? 'active' : 'disabled')) === 'disabled');
      } else if (lifecycle === 'archived') {
        rows = rows.filter((x) => x.lifecycle_state === 'archived');
      }
      if (q) rows = rows.filter((x) =>
        `${x.store_code} ${x.store_name}`.toLowerCase().includes(q));
      if (param('region')) rows = rows.filter((x) => x.region === param('region'));
      if (param('city')) rows = rows.filter((x) => x.city === param('city'));
      return route.fulfill(json(paged(rows.map((x) => ({
        id: x.id, store_code: x.store_code, store_name: x.store_name,
        city: x.city, region: x.region,
        is_online_store: !!x.is_online_store,
        is_active: x.is_active !== false,
        lifecycle_state: x.lifecycle_state || 'active',
      })))));
    }

    if (method === 'GET' && path === '/receivers/filter-options') {
      const visible = state.stores.filter((s) => s.lifecycle_state !== 'deleted');
      return route.fulfill(json({
        regions: [...new Set(visible.map((s) => s.region))].sort(),
        cities: [...new Set(visible.map((s) => s.city))].sort(),
        stores: visible.map((s) => ({ id: s.id, store_code: s.store_code, store_name: s.store_name })),
      }));
    }

    if (method === 'GET' && path === '/receivers/search') {
      if (state.storesListStatus !== 200) {
        return route.fulfill(json({ detail: 'Receiver Status could not be loaded.' },
                                  state.storesListStatus));
      }
      let rows = state.stores
        .filter((s) => s.lifecycle_state !== 'deleted')
        .map((s) => ({
          id: s.id, store_code: s.store_code, store_name: s.store_name,
          city: s.city, region: s.region, status: s.status,
          connected: s.status === 'online', ready: s.status === 'online',
          has_primary: s.id === 1, device_count: s.id === 1 ? state.devices.length : 0,
          speaker_verified: null,
        }));
      if (q) rows = rows.filter((r) => `${r.store_code} ${r.store_name}`.toLowerCase().includes(q));
      if (param('region')) rows = rows.filter((r) => r.region === param('region'));
      if (param('city')) rows = rows.filter((r) => r.city === param('city'));
      if (param('store_id')) rows = rows.filter((r) => String(r.id) === param('store_id'));
      if (param('status')) rows = rows.filter((r) => r.status === param('status'));
      if (param('has_primary')) rows = rows.filter((r) => String(r.has_primary) === param('has_primary'));
      return route.fulfill(json(paged(rows)));
    }

    if (method === 'GET' && path === '/receiver-devices/search') {
      const storeOf = (id) => state.stores.find((s) => s.id === id) || state.stores[0];
      let rows = state.devices.map((d) => {
        const store = storeOf(d.store_id || 1);
        return {
          public_id: d.public_id, display_name: d.display_name, status: d.status,
          lifecycle: d.deleted_at ? 'deleted' : d.archived_at ? 'archived' : 'active',
          archived_at: d.archived_at || null, deleted_at: d.deleted_at || null,
          is_primary: d.role === 'PRIMARY',
          store_id: store.id, store_code: store.store_code, store_name: store.store_name,
          city: store.city, region: store.region,
        };
      });
      if (q) rows = rows.filter((r) =>
        `${r.display_name} ${r.public_id} ${r.store_code} ${r.store_name}`.toLowerCase().includes(q));
      if (param('region')) rows = rows.filter((r) => r.region === param('region'));
      if (param('city')) rows = rows.filter((r) => r.city === param('city'));
      if (param('store_id')) rows = rows.filter((r) => String(r.store_id) === param('store_id'));
      if (param('status')) rows = rows.filter((r) => r.status === param('status'));
      if (param('is_primary')) rows = rows.filter((r) => String(r.is_primary) === param('is_primary'));
      const deviceLifecycle = param('lifecycle') || 'all_current';
      if (deviceLifecycle === 'all_current') {
        rows = rows.filter((r) => r.lifecycle !== 'deleted');
      } else {
        rows = rows.filter((r) => r.lifecycle === deviceLifecycle);
      }
      return route.fulfill(json(paged(rows)));
    }

    if (method === 'GET' && path === '/logs/search') {
      let rows = state.logs;
      if (q) rows = rows.filter((r) => r.message.toLowerCase().includes(q));
      if (param('level')) rows = rows.filter((r) => r.level === param('level'));
      if (param('actor_user_id')) rows = rows.filter((r) => String(r.actor_user_id) === param('actor_user_id'));
      if (param('store_id')) rows = rows.filter((r) => String(r.store_id) === param('store_id'));
      if (param('date_from')) rows = rows.filter((r) => r.created_at.slice(0, 10) >= param('date_from'));
      if (param('date_to')) rows = rows.filter((r) => r.created_at.slice(0, 10) <= param('date_to'));
      if (flag('archived_only')) rows = rows.filter((r) => r.archived_at);
      else if (!flag('include_archived')) rows = rows.filter((r) => !r.archived_at);
      const body = paged(rows);
      body.meta = { entity_filter_coverage: {
        rows_with_structured_entities: state.logs.filter((r) => r.store_id).length,
        total_rows: state.logs.length } };
      return route.fulfill(json(body));
    }

    if (method === 'GET' && path === '/broadcast/history/search') {
      let rows = state.sessions;
      if (q) rows = rows.filter((r) => r.campaign_name.toLowerCase().includes(q));
      if (param('status')) rows = rows.filter((r) => r.status === param('status'));
      if (param('started_by')) rows = rows.filter((r) => String(r.started_by) === param('started_by'));
      if (param('date_from')) rows = rows.filter((r) => r.created_at.slice(0, 10) >= param('date_from'));
      if (param('date_to')) rows = rows.filter((r) => r.created_at.slice(0, 10) <= param('date_to'));
      if (flag('archived_only')) rows = rows.filter((r) => r.archived_at);
      else if (!flag('include_archived')) rows = rows.filter((r) => !r.archived_at);
      return route.fulfill(json(paged(rows)));
    }

    // ---- Bulk archive / unarchive / permanent delete -----------------------
    // The request body is recorded before anything is done with it, because
    // WHAT WAS SENT is the property under test: mode "filtered" must carry the
    // filter and no ids, so the backend resolves the matched set inside the
    // caller's own scope rather than trusting a list React built.
    const bulk = path.match(/^\/(logs|broadcast\/history)\/(archive|unarchive|delete-permanently)$/);
    if (method === 'POST' && bulk) {
      const body = request.postDataJSON() || {};
      state.bulkCalls.push({ path, body });
      const collection = bulk[1] === 'logs' ? 'logs' : 'sessions';
      const action = bulk[2];
      const key = collection === 'logs' ? 'id' : 'id';

      if (action === 'delete-permanently') {
        if (body.confirm !== 'DELETE') {
          return route.fulfill(json({ detail: "Type DELETE exactly to confirm." }, 409));
        }
        if (!body.acknowledged) {
          return route.fulfill(json({ detail: 'The acknowledgement is required.' }, 400));
        }
      }

      const matches = (row) => (body.mode === 'filtered'
        ? true
        : (body.ids || []).includes(row[key]));
      const targeted = state[collection].filter(matches);

      if (action === 'delete-permanently') {
        state[collection] = state[collection].filter((row) => !matches(row));
      } else {
        const stamp = action === 'archive' ? '2026-08-02T00:00:00+00:00' : null;
        state[collection] = state[collection].map((row) =>
          (matches(row) ? { ...row, archived_at: stamp } : row));
      }
      return route.fulfill(json({
        requested: targeted.length, affected: targeted.length, matched: targeted.length,
        skipped: 0, failed: 0, ids: targeted.map((row) => row[key]),
      }));
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

      if (method === 'GET' && path === '/users/search') {
        // No include_deleted branch: a permanently deleted account has no row,
        // so there is nothing any parameter could reveal.
        let rows = state.users.filter((r) => (r.lifecycle_state || 'active') !== 'deleted');
        if (q) rows = rows.filter((r) =>
          `${r.username} ${r.display_name}`.toLowerCase().includes(q));
        if (param('role')) rows = rows.filter((r) => r.role === param('role'));
        if (param('state')) rows = rows.filter((r) => (r.lifecycle_state || 'active') === param('state'));
        state.userScope = state.userScope || {};
        const scoped = (type, value) => rows.filter((r) =>
          (state.userScope[r.id] || []).some((e) => e.scope_type === type
            && (type === 'STORE' ? String(e.store_id) === value : e.scope_value === value)));
        if (param('scope_store_id')) rows = scoped('STORE', param('scope_store_id'));
        if (param('scope_city')) rows = scoped('CITY', param('scope_city'));
        if (param('scope_region')) rows = scoped('REGION', param('scope_region'));
        return route.fulfill(json(paged(rows)));
      }

      // TRUE permanent deletion: the row really goes. Mirrors
      // backend/user_permanent_delete.py.
      //
      // This used to tombstone - the row stayed, marked deleted - which kept
      // the username reserved for ever and left the account on screen with
      // Rights, Scope and Reset Password beside it. The account is now
      // removed, its rights and Store Scope go with it, and its username
      // becomes available again.
      const userTombstone = path.match(/^\/users\/(\d+)\/delete-permanently$/);
      if (method === 'POST' && userTombstone) {
        const id = Number(userTombstone[1]);
        const row = state.users.find((c) => c.id === id);
        if (!row) return route.fulfill(json({ detail: 'No such HQ User' }, 404));
        const body = request.postDataJSON() || {};
        state.deletePermanentlyCalls.push({ kind: 'user', id, body });
        if (!body.acknowledged) {
          return route.fulfill(json({ detail: 'The acknowledgement is required.' }, 400));
        }
        if (body.confirm !== row.username) {
          return route.fulfill(json({
            detail: `The typed confirmation did not match. Type the username exactly: ${row.username}` }, 409));
        }
        if (id === state.operator.id) {
          return route.fulfill(json({ detail: 'You cannot delete your own account.' }, 409));
        }
        if (row.role === 'OWNER'
            && state.users.filter((c) => c.role === 'OWNER').length <= 1) {
          return route.fulfill(json({ detail: 'This is the last SUPER ADMIN.' }, 409));
        }
        // The row goes, and so does everything that belonged only to it - a
        // later account reusing the username must inherit nothing.
        state.users = state.users.filter((c) => c.id !== id);
        state.userScope = state.userScope || {};
        delete state.userScope[id];
        state.userRights = state.userRights || {};
        delete state.userRights[id];
        return route.fulfill(json({ ok: true, user_id: id, username: row.username,
                                    row_deleted: true, username_released: true }));
      }
      if (method === 'POST' && path === '/users') {
        const body = JSON.parse(request.postData() || '{}');
        if (state.users.some((row) => row.username.toLowerCase() === (body.username || '').toLowerCase())) {
          return route.fulfill(json({ detail: `The username '${body.username}' is already in use.` }, 409));
        }
        // A high-water mark, never `length + 1`: reusing a deleted id is
        // exactly how an old broadcast rebinds to a new person. Mirrors the
        // AUTOINCREMENT the real hq_users table now uses.
        state.nextUserId = Math.max(
          state.nextUserId || 0,
          ...state.users.map((r) => r.id), 0) + 1;
        const created = {
          id: state.nextUserId, username: body.username,
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
            detail: 'This is the only active SUPER_ADMIN. That would leave nobody able to administer SpeakLink.',
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

    // TRUE permanent delete: the row really goes and the Store Code is freed.
    // Mirrors backend/store_permanent_delete.py.
    //
    // This used to tombstone - lifecycle_state became 'deleted' and the row
    // stayed - which kept the Store Code reserved for ever. That is exactly
    // the defect the operator hit with AYUSHK.
    // Creating a Store. Mirrors the real uniqueness rule (case-sensitive on
    // store_code, and only against Stores that still EXIST) and the real id
    // rule: a high-water mark, never `length + 1`, because reusing a deleted
    // Store's id is how old history rebinds to a new shop.
    if (method === 'POST' && path === '/stores') {
      const body = request.postDataJSON() || {};
      if (state.stores.some((s) => s.store_code === body.store_code)) {
        return route.fulfill(json({ detail: 'store_code already exists' }, 409));
      }
      state.nextStoreId = Math.max(
        state.nextStoreId || 0, ...state.stores.map((s) => s.id), 0) + 1;
      const created = {
        id: state.nextStoreId,
        store_code: body.store_code,
        store_name: body.store_name,
        city: body.city || 'TESTVILLE',
        region: body.region || 'TEST ZONE',
        is_online_store: false,
        is_active: true,
        lifecycle_state: 'active',
        status: 'offline',
      };
      state.stores = [...state.stores, created];
      return route.fulfill(json(created, 201));
    }

    const storeTombstone = path.match(/^\/stores\/(\d+)\/delete-permanently$/);
    if (method === 'POST' && storeTombstone) {
      const id = Number(storeTombstone[1]);
      const store = state.stores.find((s) => s.id === id);
      if (!store) return route.fulfill(json({ detail: 'Store not found' }, 404));
      const payload = request.postDataJSON();
      if (!payload.acknowledged) {
        return route.fulfill(json({
          detail: "The 'this Store cannot be restored' acknowledgement is required." }, 400));
      }
      if (payload.confirm !== store.store_code) {
        return route.fulfill(json({
          detail: `The typed confirmation did not match. Type the Store code exactly: ${store.store_code}` }, 409));
      }
      // A new object, never a mutation of the shared fixture - state.stores
      // defaults to the module-level STORES array by reference, and
      // mutating one of its elements in place would leak into every other
      // test that reuses that fixture.
      // The row goes, and everything that belonged only to it goes with it -
      // a later Store reusing the code must inherit nothing.
      state.stores = state.stores.filter((s) => s.id !== id);
      state.storeDevices = state.storeDevices || {};
      delete state.storeDevices[id];
      state.transitions.push({ action: 'permanently-deleted', id });
      return route.fulfill(json({
        ok: true, store_id: id, store_code: store.store_code, store_name: store.store_name,
        row_deleted: true, store_code_released: true,
        devices_detached: 0, credentials_revoked: 0,
        live_removed: {}, history_detached: {},
      }));
    }

    if (method === 'GET' && path === '/stores') {
      if (state.storesListStatus !== 200) {
        return route.fulfill(json({ detail: 'Receiver Status could not be loaded.' }, state.storesListStatus));
      }
      // The real backend hides archived Stores unless asked. Mirror that, so a
      // test cannot pass here and fail against the server.
      const includeArchived = url.searchParams.get('include_archived') === 'true';
      // A tombstoned (permanently deleted) Store never comes back here,
      // unconditionally - unlike archived, no flag reveals it operationally.
      const notDeleted = state.stores.filter((s) => s.lifecycle_state !== 'deleted');
      const visible = includeArchived
        ? notDeleted
        : notDeleted.filter((s) => (s.lifecycle_state || 'active') !== 'archived');
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

    // The broadcast TARGET catalog. Gated on menu.broadcast.view here, exactly
    // as the real endpoint is, and deliberately NOT on menu.stores.view - that
    // coupling is the defect these routes exist to prove is gone. Store Scope
    // is applied server-side there; `state.scopedStoreIds` stands in for it.
    if (method === 'GET' && path === '/broadcast/target-stores') {
      if (!state.permissions.includes('menu.broadcast.view')) {
        return route.fulfill(json(
          { detail: 'You do not have permission to perform this action.' }, 403,
        ));
      }
      const targetable = state.stores.filter(
        (s) => (s.lifecycle_state || 'active') === 'active'
          && (state.scopedStoreIds === null
              || state.scopedStoreIds.includes(s.id)),
      );
      // Only the fields the Console draws - the same seven the real
      // BroadcastTargetStoreOut carries, so a leak here would fail there too.
      const stores = targetable.map((s) => ({
        id: s.id, store_code: s.store_code, store_name: s.store_name,
        city: s.city, region: s.region,
        is_online_store: Boolean(s.is_online_store), status: s.status,
      }));
      return route.fulfill(json({
        stores,
        regions: [...new Set(stores.map((s) => s.region).filter(Boolean))].sort(),
        cities: [...new Set(stores.map((s) => s.city).filter(Boolean))].sort(),
      }));
    }

    // ---- per-Store output volume ------------------------------------------
    //
    // Models the parts a browser test can actually observe: the requested
    // value comes back immediately (pending), and the APPLIED value only
    // appears once a Receiver acknowledgement is simulated. A mock that
    // reported "applied" straight away would let a UI claiming an unproven
    // result pass.
    const audioControl = path.match(/^\/broadcast\/sessions\/(\d+)\/audio-control$/);
    if (audioControl) {
      const sessionId = Number(audioControl[1]);
      if (!state.permissions.includes('store_audio.control')) {
        return route.fulfill(json(
          { detail: 'You do not have permission to perform this action.' }, 403));
      }
      if (state.audioControlSessionEnded) {
        return route.fulfill(json({ detail: 'That broadcast is no longer active.' }, 409));
      }
      if (method === 'POST') {
        const body = request.postDataJSON() || {};
        const existing = state.audioControl[body.store_id] || {
          requested_volume_percent: 100, requested_muted: false,
          applied_volume_percent: null, applied_muted: null,
          last_command_id: 0, last_acknowledged_command_id: 0, result: null,
        };
        state.audioCommandId += 1;
        state.audioControl[body.store_id] = {
          ...existing,
          requested_volume_percent: 'volume_percent' in body
            ? body.volume_percent : existing.requested_volume_percent,
          requested_muted: 'muted' in body ? body.muted : existing.requested_muted,
          last_command_id: state.audioCommandId,
        };
        // The simulated Receiver answers unless a test asks it not to.
        if (state.audioControlAckResult !== 'none') {
          const row = state.audioControl[body.store_id];
          const result = state.audioControlAckResult || 'applied';
          state.audioControl[body.store_id] = {
            ...row,
            last_acknowledged_command_id: row.last_command_id,
            result,
            applied_volume_percent: result === 'applied'
              ? row.requested_volume_percent : null,
            applied_muted: result === 'applied' ? row.requested_muted : null,
            error_message: result === 'failed' ? 'Could not apply output volume' : null,
          };
        }
      }
      const stores = Object.entries(state.audioControl).map(([storeId, row]) => ({
        store_id: Number(storeId),
        requested_volume_percent: row.requested_volume_percent,
        requested_muted: row.requested_muted,
        applied_volume_percent: row.applied_volume_percent ?? null,
        applied_muted: row.applied_muted ?? null,
        last_command_id: row.last_command_id,
        last_acknowledged_command_id: row.last_acknowledged_command_id,
        result: row.result ?? null,
        error_code: null,
        error_message: row.error_message ?? null,
        output_device: 'index:1',
        pending: row.last_command_id > row.last_acknowledged_command_id,
        supported: state.audioControlSupported !== false,
        online: state.audioControlOnline !== false,
      }));
      return route.fulfill(json({ session_id: sessionId, stores }));
    }

    if (method === 'GET' && path === '/broadcast/current') {
      return route.fulfill(json(state.current));
    }

    // Shaped exactly like GET /api/broadcast/active. The REDACTION happens
    // here, as it does on the real server: without broadcast.view_ownership
    // the sessions list is EMPTY rather than filled with anonymised stubs,
    // because a stub still discloses how many other broadcasts exist.
    if (method === 'GET' && path === '/broadcast/active') {
      const mayViewOwnership = state.permissions.includes('broadcast.view_ownership');
      const me = state.operator;
      const mine = state.activeSessions.find((s) => s.owner_username === me.username) || null;
      const others = state.activeSessions.filter((s) => s.owner_username !== me.username);
      const busy = [];
      state.activeSessions.forEach((s) => busy.push(...s.target_store_ids));
      return route.fulfill(json({
        mine: mine ? {
          session_id: mine.session_id,
          campaign_name: mine.campaign_name,
          started_at: mine.started_at,
          target_store_ids: mine.target_store_ids,
          target_store_count: mine.target_store_ids.length,
        } : null,
        sessions: mayViewOwnership ? others.map((s) => ({
          session_id: s.session_id,
          campaign_name: s.campaign_name,
          owner_user_id: s.owner_user_id,
          owner_username: s.owner_username,
          owner_display_name: s.owner_display_name,
          started_at: s.started_at,
          // Exact targets need broadcast.view_targets of their own. Ownership
          // visibility used to carry them, which made it a back door to
          // target visibility.
          ...(state.permissions.includes('broadcast.view_targets')
            ? { target_store_ids: s.target_store_ids } : {}),
          target_store_count: s.target_store_ids.length,
        })) : [],
        busy_store_ids: busy,
        may_view_ownership: mayViewOwnership,
        may_view_targets: state.permissions.includes('broadcast.view_targets'),
        // Withheld entirely from accounts that may not open the supervision
        // page - the number itself is a disclosure.
        may_manage_active: state.permissions.includes('broadcast.active_view'),
        active_count: state.permissions.includes('broadcast.active_view')
          ? state.activeSessions.length : null,
      }));
    }

    // ---- Active Broadcasts supervision -------------------------------------
    // Shaped exactly like GET /api/broadcast/active-management. Redaction
    // happens HERE, as on the real server: a field the caller may not see is
    // never built, so a spec that finds it on screen has found a real leak.
    if (method === 'GET' && path === '/broadcast/active-management') {
      if (!state.permissions.includes('broadcast.active_view')) {
        return route.fulfill(json(
          { detail: 'You do not have permission to view this.' }, 403));
      }
      const mayOwn = state.permissions.includes('broadcast.view_ownership');
      const mayTargets = state.permissions.includes('broadcast.view_targets');
      const params = new URL(request.url()).searchParams;
      const term = (params.get('q') || '').trim().toLowerCase();
      const ownerFilter = params.get('owner') || 'all';
      const sort = params.get('sort') || 'newest';
      const page = Number(params.get('page') || 1);
      const pageSize = Number(params.get('page_size') || 20);

      let rows = state.activeSessions.map((s) => ({
        session_id: s.session_id,
        campaign_name: s.campaign_name,
        started_at: s.started_at,
        status: 'live',
        target_store_count: s.target_store_ids.length,
        is_mine: s.owner_username === state.operator.username,
        _owner: s,
      }));

      if (ownerFilter === 'mine') rows = rows.filter((r) => r.is_mine);
      if (ownerFilter === 'others') rows = rows.filter((r) => !r.is_mine);

      if (term) {
        rows = rows.filter((r) => {
          const hay = [r.campaign_name];
          if (mayOwn || r.is_mine) {
            hay.push(r._owner.owner_username, r._owner.owner_display_name);
          }
          if (mayTargets) {
            (r._owner.target_store_names || []).forEach((n) => hay.push(n));
          }
          return hay.filter(Boolean).some((v) => v.toLowerCase().includes(term));
        });
      }

      rows.sort((a, b) => (sort === 'newest'
        ? String(b.started_at).localeCompare(String(a.started_at))
        : String(a.started_at).localeCompare(String(b.started_at))));

      const total = rows.length;
      const window = rows.slice((page - 1) * pageSize, page * pageSize);
      return route.fulfill(json({
        items: window.map((r) => {
          const row = {
            session_id: r.session_id,
            campaign_name: r.campaign_name,
            started_at: r.started_at,
            status: r.status,
            target_store_count: r.target_store_count,
            is_mine: r.is_mine,
          };
          if (mayOwn || r.is_mine) {
            row.owner_user_id = r._owner.owner_user_id;
            row.owner_username = r._owner.owner_username;
            row.owner_display_name = r._owner.owner_display_name;
          }
          return row;
        }),
        total,
        page,
        page_size: pageSize,
        pages: Math.ceil(total / pageSize),
        has_more: page * pageSize < total,
        meta: {
          may_view_ownership: mayOwn,
          may_view_targets: mayTargets,
          may_stop_any: state.permissions.includes('broadcast.stop_any'),
        },
      }));
    }

    const storesMatch = path.match(/^\/broadcast\/active-management\/(\d+)\/stores$/);
    if (method === 'GET' && storesMatch) {
      if (!state.permissions.includes('broadcast.active_view')
          || !state.permissions.includes('broadcast.view_targets')) {
        return route.fulfill(json(
          { detail: 'You do not have permission to view the Stores of a broadcast.' }, 403));
      }
      const found = state.activeSessions.find(
        (s) => s.session_id === Number(storesMatch[1]));
      if (!found) return route.fulfill(json({ detail: 'No such active broadcast' }, 404));
      return route.fulfill(json({
        session_id: found.session_id,
        campaign_name: found.campaign_name,
        started_at: found.started_at,
        target_store_count: found.target_store_ids.length,
        stores: found.target_store_ids.map((id) => {
          const store = STORES.find((s) => s.id === id) || {};
          return { store_id: id, store_code: store.store_code,
                   store_name: store.store_name };
        }),
        ...(state.permissions.includes('broadcast.view_ownership')
          ? { owner_user_id: found.owner_user_id,
              owner_username: found.owner_username,
              owner_display_name: found.owner_display_name } : {}),
      }));
    }

    const stopMatch = path.match(/^\/broadcast\/active-management\/(\d+)\/stop$/);
    if (method === 'POST' && stopMatch) {
      const id = Number(stopMatch[1]);
      const found = state.activeSessions.find((s) => s.session_id === id);
      if (!found) return route.fulfill(json({ detail: 'No such active broadcast' }, 404));
      const isMine = found.owner_username === state.operator.username;
      if (!state.permissions.includes('broadcast.active_view')
          || (!isMine && !state.permissions.includes('broadcast.stop_any'))) {
        return route.fulfill(json(
          { detail: "You do not have permission to stop another operator's broadcast." },
          403));
      }
      // ONLY the named session. Every other broadcast stays live - the
      // property that separates this from Emergency Stop All.
      state.activeSessions = state.activeSessions.filter((s) => s.session_id !== id);
      return route.fulfill(json({ ok: true, session_id: id, status: 'ended' }));
    }

    if (method === 'POST' && path === '/broadcast/emergency-stop') {
      if (!state.permissions.includes('broadcast.emergency_stop')) {
        return route.fulfill(json(
          { detail: 'You do not have permission to perform this action.' }, 403));
      }
      if (state.emergencyStopIncomplete) {
        return route.fulfill(json({ detail: {
          code: 'EMERGENCY_STOP_INCOMPLETE',
          message: 'Some broadcasts could not be stopped and may still be live.',
          stopped_session_ids: [state.activeSessions[0]?.session_id].filter(Boolean),
          failed_session_ids: [state.activeSessions[1]?.session_id].filter(Boolean),
        } }, 500));
      }
      const stopped = state.activeSessions.map((s) => s.session_id);
      state.activeSessions = [];
      state.current = { live: false, session: null, targets: [], ready_receivers: [] };
      return route.fulfill(json({ ok: true, session_ids: stopped }));
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
      // A permanently deleted Device is operationally gone. deleted_at is the
      // marker: its status is 'retired', and so is an ordinarily retired
      // Device's, so status cannot tell them apart.
      return route.fulfill(json(state.devices.filter((d) => !d.deleted_at)));
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
          credential: `speaklink_rcv_v2.${publicId}.rotated-secret-shown-once`,
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

    // The Device tombstone. Mirrors backend/device_deletion.py: the row stays
    // so credential and enrolment history remain readable, its status becomes
    // 'retired', its credentials are revoked, and no restore exists.
    const deviceTombstone = path.match(/^\/receiver-devices\/([^/]+)\/delete-permanently$/);
    if (method === 'POST' && deviceTombstone) {
      const publicId = deviceTombstone[1];
      const device = state.devices.find((d) => d.public_id === publicId);
      if (!device) return route.fulfill(json({ detail: 'No such Device' }, 404));
      const body = request.postDataJSON() || {};
      state.deletePermanentlyCalls.push({ kind: 'device', public_id: publicId, body });
      if (!body.acknowledged) {
        return route.fulfill(json({ detail: 'The acknowledgement is required.' }, 400));
      }
      if (body.confirm !== publicId) {
        return route.fulfill(json({
          detail: `The typed confirmation did not match. Type the Device id exactly: ${publicId}` }, 409));
      }
      state.devices = state.devices.map((d) => (d.public_id === publicId
        ? { ...d, status: 'retired', role: 'STANDBY',
            deleted_at: '2026-08-02T00:00:00+00:00',
            disabled_at: '2026-08-02T00:00:00+00:00' }
        : d));
      return route.fulfill(json({ ok: true, public_id: publicId, credentials_revoked: 1 }));
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
    window.localStorage.setItem('speaklink_token', token);
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
  LOG_ENTRIES,
  HISTORY_SESSIONS,
  PRIMARY_DEVICE,
  STANDBY_DEVICE,
  FAKE_TOKEN,
  OPERATOR,
};
