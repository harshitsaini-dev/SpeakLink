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
      return route.fulfill(json({ access_token: FAKE_TOKEN, token_type: 'bearer', user: OPERATOR }));
    }

    if (method === 'GET' && path === '/auth/me') {
      return route.fulfill(json(OPERATOR));
    }

    if (method === 'POST' && path === '/auth/ws-ticket') {
      // A fresh opaque value each time, as the real endpoint does. Never a JWT.
      state.ticketsIssued += 1;
      return route.fulfill(json({ ticket: `test-ticket-${state.ticketsIssued}`, expires_in: 20 }));
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
        json({ code: `ECHO-CODE-${state.codesIssued}`, store_id: 1, expires_in_seconds: 900 }),
      );
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
  PRIMARY_DEVICE,
  STANDBY_DEVICE,
  FAKE_TOKEN,
  OPERATOR,
};
