/**
 * The public listener, end to end in a real Chromium.
 *
 * The audio is REAL: the Clusters pushed down the fake socket are the frames
 * the shipped backend framer produced from a real MediaRecorder capture (see
 * backend/tools/emit_relay_frames.py). So "Listening" here is proved by the
 * element's currentTime advancing, not by the word appearing on screen - which
 * is the whole point, since a page can print anything.
 *
 * The HTTP API is mocked and the WebSocket is replaced in the page, because
 * what is under test is the listener application: admission, playback truth,
 * kick, room end and reconnect. The backend halves of those are proved against
 * the real server in backend/tests/test_web_audience_api.py and
 * test_listener_socket_and_link_only.py.
 */
const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

const FRAMES_BIN = path.join(__dirname, 'fixtures', 'relay-frames.bin');
const FRAMES_INDEX = path.join(__dirname, 'fixtures', 'relay-frames.json');

const available = fs.existsSync(FRAMES_BIN) && fs.existsSync(FRAMES_INDEX);

const ROOM_CODE = 'EC-7K4P92';
const PASSWORD = 'Q7KM-92PX';

test.describe('the public SpeakLink listener', () => {
  test.skip(!available, 'generate first: python backend/tools/emit_relay_frames.py');

  let frames;

  test.beforeAll(() => {
    const index = JSON.parse(fs.readFileSync(FRAMES_INDEX, 'utf8'));
    const blob = fs.readFileSync(FRAMES_BIN);
    frames = index.frames.map((frame) => ({
      kind: frame.kind,
      bytes: Array.from(blob.subarray(frame.offset, frame.offset + frame.length)),
    }));
  });

  /**
   * Replace WebSocket in the page with one the test drives, and hand it the
   * real relay frames. Everything else about the page is untouched.
   */
  async function installFakeSocket(page) {
    await page.addInitScript(({ payload }) => {
      window.__listenerFrames = payload;
      window.__sockets = [];
      class DrivenSocket {
        constructor(url) {
          this.url = url;
          this.readyState = 1;
          this.sent = [];
          // ONLY the listener socket is tracked. The CRA dev server opens its
          // own hot-reload socket, and pushing audio into that one produced a
          // JSON parse error from webpack rather than anything about SpeakLink.
          if (String(url).includes('/api/listen/ws')) window.__sockets.push(this);
          setTimeout(() => this.onopen && this.onopen(), 0);
        }
        send(data) { this.sent.push(data); }
        close() { this.readyState = 3; if (this.onclose) this.onclose({ code: 1000 }); }
      }
      DrivenSocket.OPEN = 1;
      window.WebSocket = DrivenSocket;

      // The page opens its socket only after the session bootstrap round trip,
      // so a test that starts pushing the moment the join resolves can arrive
      // before the socket exists. Wait for it rather than assume it.
      window.__socket = async () => {
        const deadline = performance.now() + 15_000;
        while (window.__sockets.length === 0) {
          if (performance.now() > deadline) {
            throw new Error('the listener never opened a socket');
          }
          await new Promise((resume) => setTimeout(resume, 50));
        }
        return window.__sockets[window.__sockets.length - 1];
      };

      // Push the bootstrap and then every Cluster, as the relay does.
      window.__deliver = async (fromCluster) => {
        const socket = await window.__socket();
        const clusters = window.__listenerFrames.filter((f) => f.kind === 'cluster');
        const init = window.__listenerFrames.find((f) => f.kind === 'init');
        socket.onmessage({ data: JSON.stringify({
          type: 'bootstrap', mime: 'audio/webm;codecs=opus',
          clusters: 0, heartbeat_seconds: 10 }) });
        socket.onmessage({ data: new Uint8Array(init.bytes).buffer });
        for (let i = fromCluster; i < clusters.length; i += 1) {
          socket.onmessage({ data: new Uint8Array(clusters[i].bytes).buffer });
        }
      };
      window.__pushControl = async (message) => {
        const socket = await window.__socket();
        socket.onmessage({ data: JSON.stringify(message) });
      };
      window.__dropSocket = async (code) => {
        const socket = await window.__socket();
        socket.readyState = 3;
        socket.onclose({ code });
      };
    }, { payload: frames });
  }

  async function mockApi(page, { admitted = true, joinStatus = 200,
                                 admissionStatus = 'PASSWORD_ADMITTED' } = {}) {
    await page.route('**/api/listen/rooms/*/join', (route) => {
      if (joinStatus !== 200) {
        return route.fulfill({ status: joinStatus,
                               contentType: 'application/json',
                               body: JSON.stringify({ detail: 'Incorrect password.' }) });
      }
      return route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ public_code: ROOM_CODE, display_name: 'Harshit',
          admission_status: admissionStatus, admitted,
          broadcast_live: true, heartbeat_seconds: 10 }) });
    });
    await page.route('**/api/listen/rooms/*/request-access', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json',
        body: JSON.stringify({ public_code: ROOM_CODE, display_name: 'Aman',
          admission_status: 'REQUESTED', admitted: false,
          broadcast_live: true, heartbeat_seconds: 10 }) }));
  }

  async function join(page, { name = 'Harshit', password = PASSWORD } = {}) {
    await page.getByTestId('listen-name').fill(name);
    await page.getByTestId('listen-password').fill(password);
    await page.getByTestId('listen-join').click();
  }

  /** What the element is REALLY doing, not what the page says. */
  async function playbackTruth(page) {
    return page.locator('[data-testid="listener-audio"]').evaluate(async (node) => {
      const start = node.currentTime;
      await new Promise((resolve) => setTimeout(resolve, 900));
      return { paused: node.paused, advanced: node.currentTime > start,
               currentTime: node.currentTime, error: node.error ? node.error.code : null };
    });
  }

  // ---- FLOW A: password join -------------------------------------------
  test('FLOW A: a password join really plays', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);
    await page.goto(`/listen/${ROOM_CODE}`);

    await expect(page.getByTestId('listen-code')).toHaveValue(ROOM_CODE);
    await join(page);
    await expect(page.getByTestId('listen-live')).toBeVisible();

    await page.evaluate(() => window.__deliver(0));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');

    const truth = await playbackTruth(page);
    expect(truth.paused).toBe(false);
    expect(truth.advanced, 'the listener is not actually playing').toBe(true);
    expect(truth.error).toBeNull();
  });

  test('FLOW A2: a wrong password says so and does not join', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page, { joinStatus: 401 });
    await page.goto(`/listen/${ROOM_CODE}`);
    await join(page, { password: 'WRONG-PASS' });

    await expect(page.getByTestId('listen-error')).toContainText(/incorrect password/i);
    await expect(page.getByTestId('listen-live')).toHaveCount(0);
    await expect(page.getByTestId('listen-waiting')).toHaveCount(0);
  });

  // ---- FLOW B: request then approve ------------------------------------
  test('FLOW B: a request waits, then plays once approved', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);

    let admitted = false;
    // Nobody has asked yet, so there is no session. The real server answers 401
    // here, and the page must show the form rather than a waiting screen - it
    // now asks /listen/me on load, so a mock that claimed a session regardless
    // of cookie would put a first-time visitor straight into "waiting".
    let hasRequested = false;
    // The trailing wildcard matters: the page now asks about ONE Broadcast,
    // so this URL carries ?public_code=..., and a pattern without it stops
    // matching the moment the question becomes room-scoped.
    await page.route('**/api/listen/me*', (route) => {
      if (!hasRequested) {
        return route.fulfill({ status: 401, contentType: 'application/json',
                               body: JSON.stringify({ detail: 'Not admitted.' }) });
      }
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ public_code: ROOM_CODE, display_name: 'Aman',
          admission_status: admitted ? 'APPROVED' : 'REQUESTED',
          admitted, broadcast_live: true, heartbeat_seconds: 10 }) });
    });

    await page.goto(`/listen/${ROOM_CODE}`);
    await page.getByTestId('listen-name').fill('Aman');
    await page.getByTestId('listen-request').click();
    hasRequested = true;

    await expect(page.getByTestId('listen-waiting')).toBeVisible();
    // No audio socket exists before admission.
    expect(await page.evaluate(() => window.__sockets.length)).toBe(0);

    admitted = true;                       // the broadcaster clicks Approve
    await expect(page.getByTestId('listen-live')).toBeVisible({ timeout: 10_000 });

    await page.evaluate(() => window.__deliver(0));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');
    expect((await playbackTruth(page)).advanced).toBe(true);
  });

  // ---- FLOW G: joining late, and reconnecting ---------------------------
  test('FLOW G: a late joiner starts at the live edge and plays', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);
    await page.goto(`/listen/${ROOM_CODE}`);
    await join(page);

    // Bootstrapped from well into the stream, as a late arrival would be.
    await page.evaluate(() => window.__deliver(30));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');
    const truth = await playbackTruth(page);
    expect(truth.advanced).toBe(true);
    expect(truth.error).toBeNull();
  });

  test('FLOW G2: a dropped connection reconnects and plays again', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);
    await page.goto(`/listen/${ROOM_CODE}`);
    await join(page);
    await page.evaluate(() => window.__deliver(0));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');

    await page.evaluate(() => window.__dropSocket(1006));
    // A second socket is opened after the backoff, and gets a fresh bootstrap.
    await expect.poll(
      () => page.evaluate(() => window.__sockets.length),
      { timeout: 15_000 }).toBeGreaterThan(1);

    await page.evaluate(() => window.__deliver(20));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');
    expect((await playbackTruth(page)).advanced).toBe(true);
  });

  // ---- FLOW D: kick ----------------------------------------------------
  test('FLOW D: a kicked listener stops hearing the Broadcast', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);
    await page.goto(`/listen/${ROOM_CODE}`);
    await join(page);
    await page.evaluate(() => window.__deliver(0));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');

    await page.evaluate(() => window.__pushControl({ type: 'kicked' }));
    await expect(page.getByTestId('listen-kicked'))
      .toContainText(/removed from this Broadcast/i);

    // The audio really stopped, and no reconnect was attempted.
    const stopped = await page.locator('[data-testid="listener-audio"]')
      .evaluate((node) => node.paused);
    expect(stopped).toBe(true);
    const before = await page.evaluate(() => window.__sockets.length);
    await page.waitForTimeout(3000);
    expect(await page.evaluate(() => window.__sockets.length)).toBe(before);
  });

  // ---- FLOW H: the Broadcast ends --------------------------------------
  test('FLOW H: the Broadcast ending stops playback and does not retry', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);
    await page.goto(`/listen/${ROOM_CODE}`);
    await join(page);
    await page.evaluate(() => window.__deliver(0));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');

    await page.evaluate(() => window.__pushControl({ type: 'room_ended' }));
    await expect(page.getByTestId('listen-ended')).toContainText(/broadcast ended/i);

    const before = await page.evaluate(() => window.__sockets.length);
    await page.waitForTimeout(3000);
    expect(await page.evaluate(() => window.__sockets.length)).toBe(before);
  });

  // ---- the listener surface itself --------------------------------------
  test('the listener page exposes no HQ surface and no credential', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);
    await page.goto(`/listen/${ROOM_CODE}`);
    await join(page);

    // No token, jwt or password anywhere in the socket URL.
    const url = await page.evaluate(() => window.__sockets[0].url);
    expect(url).not.toMatch(/token|jwt|password|listener=/i);
    expect(page.url()).not.toMatch(/token|jwt|password/i);

    const body = await page.locator('body').innerText();
    for (const forbidden of ['Store', 'Zone', 'Receiver', 'System Logs',
                             'User Management', 'Broadcast History']) {
      expect(body).not.toContain(forbidden);
    }
    // And no HQ chrome at all.
    await expect(page.getByTestId('nav-stores')).toHaveCount(0);
    await expect(page.getByTestId('nav-history')).toHaveCount(0);
  });

  test('the heartbeat reports the browser state and nothing else', async ({ page }) => {
    await installFakeSocket(page);
    await mockApi(page);
    await page.goto(`/listen/${ROOM_CODE}`);
    await join(page);
    await page.evaluate(() => window.__deliver(0));
    await expect(page.getByTestId('listen-status')).toHaveText('Listening');

    await page.waitForTimeout(11_000);           // one heartbeat interval
    const sent = await page.evaluate(() =>
      window.__sockets[window.__sockets.length - 1].sent.map((s) => JSON.parse(s)));
    expect(sent.length).toBeGreaterThan(0);
    for (const message of sent) {
      expect(message.type).toBe('heartbeat');
      // The listener asserts only what its own browser is doing.
      expect(Object.keys(message).sort()).toEqual(['playback_state', 'type']);
    }
    expect(sent[sent.length - 1].playback_state).toBe('LISTENING');
  });
});
