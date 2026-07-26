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
  status: 'offline',
};

const STORES = [UN, ASR, DM];

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
  };

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace(/^.*\/api/, '');
    const method = request.method();

    if (method === 'POST' && path === '/auth/login') {
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
      return route.fulfill(json(state.stores));
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
  FAKE_TOKEN,
  OPERATOR,
};
