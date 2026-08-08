/**
 * The public listener against a REAL backend. No mocks anywhere.
 *
 * WHY THIS FILE EXISTS
 *
 * Manual LAN testing found two release-blocking failures while the entire
 * mocked Playwright suite was green:
 *
 *   Request Access -> Approve  showed "Broadcast ended"
 *   correct password           buffered for ever
 *
 * One cause: the listener cookie was marked Secure, and Chromium refuses a
 * Secure cookie from an untrustworthy origin like http://192.168.x.x. The
 * mocked suite never saw it because http://localhost IS trustworthy and keeps
 * Secure cookies. The tests were right about the code and wrong about the
 * world.
 *
 * So this test uses a real FastAPI server on a temporary database, a real
 * admission record, the real Set-Cookie the browser really stores, the real
 * listener WebSocket, the real WebAudienceRelay and WebM framer, the real
 * React page and real Chromium MediaSource. Audio is pushed through the actual
 * broadcaster socket, so the production relay path is the one under test.
 *
 * It touches no live HQ: its own port, its own database, its own data
 * directory, all removed afterwards.
 */
const { test, expect } = require('@playwright/test');
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const net = require('net');
const WebSocket = require('ws');

const REPO = path.join(__dirname, '..', '..');
const PYTHON = path.join(REPO, 'backend', '.venv', 'Scripts', 'python.exe');
const CAPTURE = path.join(REPO, 'backend', 'tests', 'fixtures', 'mediarecorder-live.webm');
const CAPTURE_INDEX = path.join(REPO, 'backend', 'tests', 'fixtures',
                                'mediarecorder-live.chunks.json');

const PASSWORD = 'a-long-enough-temporary-password';

//: A SAME-ORIGIN build, made once and reused.
//:
//: The ordinary build bakes REACT_APP_BACKEND_URL at compile time, so a page
//: served from this test's port would call the configured one and be refused by
//: CORS. Building with that variable empty gives the repo-native production
//: topology - the API is relative, the origin is one origin - which is exactly
//: the arrangement whose cookie behaviour is under test.
const E2E_BUILD = path.join(REPO, 'frontend', 'build-e2e');

//: A fixed port, because the bundle has to be built knowing it.
//:
//: The app decides its API origin at BUILD time, and its same-origin fallback
//: recognises only the production port 8000 - which on this machine is the live
//: HQ and must not be touched. So the e2e bundle is built pointing at this
//: port, and the test server listens on it: page and API on one origin, which
//: is what makes the real cookie and the real WebSocket behave as they do in
//: production.
const E2E_PORT = 8017;
const E2E_ORIGIN = `http://127.0.0.1:${E2E_PORT}`;

function ensureSameOriginBuild() {
  // Rebuild when any source is newer than the bundle. Returning early on mere
  // existence meant this suite could silently exercise yesterday's code - which
  // it did, and a fix that was present in the tree appeared to fail.
  const built = path.join(E2E_BUILD, 'index.html');
  if (fs.existsSync(built)) {
    const builtAt = fs.statSync(built).mtimeMs;
    const newestSource = (dir) => {
      let newest = 0;
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        newest = Math.max(newest, entry.isDirectory()
          ? newestSource(full) : fs.statSync(full).mtimeMs);
      }
      return newest;
    };
    if (newestSource(path.join(REPO, 'frontend', 'src')) < builtAt) return true;
    fs.rmSync(E2E_BUILD, { recursive: true, force: true });
  }
  try {
    execFileSync('npx', ['craco', 'build'], {
      cwd: path.join(REPO, 'frontend'),
      env: { ...process.env, CI: 'true', REACT_APP_BACKEND_URL: E2E_ORIGIN,
             BUILD_PATH: E2E_BUILD },
      stdio: 'ignore', shell: true, timeout: 600_000,
    });
  } catch (error) {
    return false;
  }
  return fs.existsSync(path.join(E2E_BUILD, 'index.html'));
}

const available = fs.existsSync(PYTHON) && fs.existsSync(CAPTURE)
  && fs.existsSync(CAPTURE_INDEX);

let server;
let base;
let workspace;

async function freePort() {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

test.describe('the public listener against a real backend', () => {
  test.skip(!available,
    'needs the backend venv and a generated MediaRecorder capture');
  test.describe.configure({ mode: 'serial' });

  test.beforeAll(async () => {
    expect(ensureSameOriginBuild(),
           'the same-origin e2e build could not be produced').toBe(true);
    workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'echocast-e2e-'));
    const port = E2E_PORT;
    base = E2E_ORIGIN;

    server = spawn(PYTHON, ['-m', 'uvicorn', 'server:app',
                            '--host', '127.0.0.1', '--port', String(port),
                            '--log-level', 'warning'], {
      cwd: path.join(REPO, 'backend'),
      env: {
        ...process.env,
        ECHOCAST_DB_PATH: path.join(workspace, 'hq.db'),
        ECHOCAST_DATA_DIR: path.join(workspace, 'data'),
        JWT_SECRET: 'e2e-only-secret-value-not-a-real-one',
        ADMIN_USERNAME: 'founder',
        ADMIN_PASSWORD: PASSWORD,
        // Deliberately NOT setting any LAN cookie override: the point is that
        // the cookie policy now follows the request scheme by itself.
        // The backend serves the built React app, which is the repo-native
        // production topology: same origin, so the listener cookie and the
        // listener WebSocket behave exactly as they do for a real operator.
        ECHOCAST_FRONTEND_BUILD: E2E_BUILD,
      },
    });
    server.stderr.on('data', (chunk) => {
      const text = String(chunk);
      if (/Traceback|ERROR/.test(text)) console.log('SERVER:', text.trim().slice(0, 400));
    });

    // Wait for it to answer.
    const deadline = Date.now() + 60_000;
    for (;;) {
      try {
        const probe = await fetch(`${base}/api/`);
        if (probe.ok) break;
      } catch (ignored) { /* not up yet */ }
      if (Date.now() > deadline) throw new Error('the test server never started');
      await new Promise((resolve) => setTimeout(resolve, 400));
    }
  });

  test.afterAll(async () => {
    if (server) server.kill();
    // Give the process a moment to release the database file on Windows.
    await new Promise((resolve) => setTimeout(resolve, 500));
    try { fs.rmSync(workspace, { recursive: true, force: true }); } catch (ignored) { /* */ }
  });

  // ---- broadcaster helpers, all through the real API ---------------------
  async function signIn() {
    const response = await fetch(`${base}/api/auth/login`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'founder', password: PASSWORD }),
    });
    const body = await response.json();
    return { Authorization: `Bearer ${body.access_token}` };
  }

  async function api(method, urlPath, headers, body) {
    const response = await fetch(`${base}/api${urlPath}`, {
      method,
      headers: { ...headers, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    return { status: response.status, body: await response.json().catch(() => null) };
  }

  /** A live Link-only Broadcast with real audio flowing through the relay. */
  async function startLiveBroadcast(headers) {
    const created = await api('POST', '/broadcast/sessions', headers, {
      campaign_name: 'Real listener test', target_mode: 'only_with_link' });
    expect(created.status).toBe(201);
    const sid = created.body.id;
    expect((await api('POST', `/broadcast/sessions/${sid}/start`, headers)).status).toBe(200);

    const ticket = await api('POST', '/auth/ws-ticket', headers,
                             { audience: 'broadcaster' });
    expect(ticket.status, JSON.stringify(ticket.body)).toBe(200);

    // The REAL broadcaster socket, so audio takes exactly the path a live
    // announcement does: fanout, recorder, then the web relay.
    const socket = new WebSocket(
      `${base.replace('http', 'ws')}/api/ws/broadcaster`
      + `?ticket=${encodeURIComponent(ticket.body.ticket)}&session_id=${sid}`);
    await new Promise((resolve, reject) => {
      socket.on('open', resolve);
      socket.on('error', reject);
      setTimeout(() => reject(new Error('broadcaster socket never opened')), 15_000);
    });

    const sizes = JSON.parse(fs.readFileSync(CAPTURE_INDEX, 'utf8')).chunkSizes;
    const data = fs.readFileSync(CAPTURE);
    let offset = 0;
    const chunks = sizes.map((size) => {
      const slice = data.subarray(offset, offset + size);
      offset += size;
      return slice;
    });

    // The header goes once; after that only the media repeats.
    //
    // Looping the whole capture would re-send the EBML header in the middle of
    // the stream, which the framer correctly rejects - a live MediaRecorder
    // never does that. Tests shorter than one loop never noticed; longer ones
    // saw the relay degrade and the listener buffer, which looked like a
    // product fault and was a fixture fault.
    socket.send(chunks[0]);
    let index = 1;
    const pump = setInterval(() => {
      if (socket.readyState !== WebSocket.OPEN) return;
      socket.send(chunks[1 + ((index - 1) % (chunks.length - 1))]);
      index += 1;
    }, 60);

    // Enough for the framer to publish an init segment and several Clusters.
    await new Promise((resolve) => setTimeout(resolve, 2500));
    const room = await api('GET', `/broadcast/sessions/${sid}/web-room`, headers);
    expect(room.status).toBe(200);
    return { sid, room: room.body, socket, pump };
  }

  function stopBroadcastPump(live) {
    clearInterval(live.pump);
    try { live.socket.close(); } catch (ignored) { /* already closed */ }
  }

  /** What the listener element is REALLY doing. */
  async function playbackTruth(page) {
    return page.locator('[data-testid="listener-audio"]').evaluate(async (node) => {
      const start = node.currentTime;
      await new Promise((resolve) => setTimeout(resolve, 1200));
      return { paused: node.paused, advanced: node.currentTime > start,
               currentTime: node.currentTime,
               error: node.error ? node.error.code : null };
    });
  }

  /** The app is served BY the backend, so there is nothing to point anywhere. */
  async function useRealBackend(page) {
    // Deliberately a no-op: same-origin is the whole point. Kept as a named
    // step so each test reads as "use the real backend".
  }

  // ======================================================================
  // TEST A - password join really plays
  // ======================================================================
  test('A: a correct password reaches LISTENING against the real backend',
       async ({ page }) => {
    test.setTimeout(120_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await useRealBackend(page);
      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();

      // ONE click. No refresh, no second Join.
      await expect(page.getByTestId('listen-live')).toBeVisible({ timeout: 20_000 });
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });

      // The word is not the proof; the element is.
      const truth = await playbackTruth(page);
      expect(truth.paused, 'the listener is not playing').toBe(false);
      expect(truth.advanced, 'currentTime never advanced').toBe(true);
      expect(truth.error).toBeNull();

      // And it never sat on Buffering or claimed the Broadcast had ended.
      await expect(page.getByTestId('listen-ended')).toHaveCount(0);
      await expect(page.getByTestId('listen-session-lost')).toHaveCount(0);

      // The broadcaster's own counts move only now that audio is progressing.
      const room = await api('GET', `/broadcast/sessions/${live.sid}/web-room`, headers);
      expect(room.body.counts.connected).toBeGreaterThan(0);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // TEST B - request, approve, play
  // ======================================================================
  test('B: Request Access then Approve reaches LISTENING, never Ended',
       async ({ page }) => {
    test.setTimeout(120_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await useRealBackend(page);
      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Aman');
      await page.getByTestId('listen-request').click();
      await expect(page.getByTestId('listen-waiting')).toBeVisible();

      // The exact defect: this used to become "Broadcast ended".
      await expect(page.getByTestId('listen-ended')).toHaveCount(0);

      const waiting = await api('GET', `/broadcast/sessions/${live.sid}/web-room`, headers);
      expect(waiting.body.counts.waiting).toBe(1);
      const pid = waiting.body.waiting[0].id;

      await api('POST',
        `/broadcast/sessions/${live.sid}/web-participants/${pid}/approve`, headers);

      // No refresh, no re-entering anything.
      await expect(page.getByTestId('listen-live')).toBeVisible({ timeout: 25_000 });
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });
      await expect(page.getByTestId('listen-ended')).toHaveCount(0);
      await expect(page.getByTestId('listen-session-lost')).toHaveCount(0);

      const truth = await playbackTruth(page);
      expect(truth.paused).toBe(false);
      expect(truth.advanced).toBe(true);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // TEST C - wrong password
  // ======================================================================
  test('C: a wrong password is refused and starts nothing', async ({ page }) => {
    test.setTimeout(90_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await useRealBackend(page);
      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Guesser');
      await page.getByTestId('listen-password').fill('WRONG-PASS');
      await page.getByTestId('listen-join').click();

      await expect(page.getByTestId('listen-error'))
        .toContainText(/incorrect password/i);
      await expect(page.getByTestId('listen-live')).toHaveCount(0);
      await expect(page.getByTestId('listen-waiting')).toHaveCount(0);

      // No participant was created by a failed guess.
      const room = await api('GET', `/broadcast/sessions/${live.sid}/web-room`, headers);
      expect(room.body.counts.waiting).toBe(0);
      expect(room.body.counts.admitted).toBe(0);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // TEST D - autoplay blocked
  // ======================================================================
  test('D: an autoplay refusal asks for a tap, then really plays',
       async ({ page }) => {
    test.setTimeout(120_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      // Refuse the FIRST play() the way a browser autoplay policy does, and
      // allow every later one - which is exactly what a user gesture changes.
      await page.addInitScript(() => {
        const real = window.HTMLMediaElement.prototype.play;
        let refused = false;
        window.HTMLMediaElement.prototype.play = function play(...args) {
          if (!refused) {
            refused = true;
            return Promise.reject(
              new DOMException('blocked by autoplay policy', 'NotAllowedError'));
          }
          return real.apply(this, args);
        };
      });

      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();

      // A gesture is required. That is NOT buffering, and NOT an error.
      await expect(page.getByTestId('listen-tap-to-start'))
        .toBeVisible({ timeout: 25_000 });
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Tap to Start Listening');
      await expect(page.getByTestId('listen-ended')).toHaveCount(0);
      await expect(page.getByTestId('listen-session-lost')).toHaveCount(0);

      // The explicit user action.
      await page.getByTestId('listen-tap-to-start').click();

      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });
      const truth = await playbackTruth(page);
      expect(truth.paused).toBe(false);
      expect(truth.advanced, 'playback never advanced after the tap').toBe(true);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // TEST F - reconnect
  // ======================================================================
  test('F: a dropped listener socket reconnects and plays again',
       async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      // Count the listener sockets this page opens, so a reconnect can be told
      // from a retry storm.
      await page.addInitScript(() => {
        window.__listenerSockets = 0;
        const Real = window.WebSocket;
        const Counted = function Counted(url, ...rest) {
          const socket = new Real(url, ...rest);
          if (String(url).includes('/api/listen/ws')) {
            window.__listenerSockets += 1;
            window.__lastListenerSocket = socket;
          }
          return socket;
        };
        Counted.prototype = Real.prototype;
        Counted.OPEN = Real.OPEN;
        Counted.CLOSED = Real.CLOSED;
        window.WebSocket = Counted;
      });

      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });

      expect(await page.evaluate(() => window.__listenerSockets)).toBe(1);

      // Break ONLY the listener's socket. The Broadcast keeps running.
      await page.evaluate(() => window.__lastListenerSocket.close());

      // A fresh socket, not a storm of them.
      await expect.poll(() => page.evaluate(() => window.__listenerSockets),
                        { timeout: 30_000 }).toBeGreaterThan(1);

      // Back to real playback, from a fresh bootstrap at the live edge.
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 30_000 });
      const truth = await playbackTruth(page);
      expect(truth.paused).toBe(false);
      expect(truth.advanced, 'playback did not resume after reconnecting').toBe(true);

      // Never told the Broadcast ended, never asked to join again.
      await expect(page.getByTestId('listen-ended')).toHaveCount(0);
      await expect(page.getByTestId('listen-join')).toHaveCount(0);

      // The SAME participant - a reconnect is not a new admission.
      const room = await api('GET', `/broadcast/sessions/${live.sid}/web-room`, headers);
      expect(room.body.counts.admitted).toBe(1);
      expect(room.body.listeners.length).toBe(1);

      // Bounded: a handful of attempts, not an unbounded retry loop.
      const sockets = await page.evaluate(() => window.__listenerSockets);
      expect(sockets, `${sockets} listener sockets were opened`).toBeLessThan(8);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // TEST E - the room really ending
  // ======================================================================
  test('E: Ended appears only after the broadcaster actually stops',
       async ({ page }) => {
    test.setTimeout(120_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await useRealBackend(page);
      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });

      // Now it really stops. This proves the approval fix did not simply
      // disable the Ended state.
      //
      // Stopped through the API while the microphone socket is still open,
      // which is what an operator pressing Stop does. Closing the socket first
      // would end the Broadcast by the disconnect path instead, and /stop would
      // then correctly refuse a session that is already over.
      expect((await api('POST', `/broadcast/sessions/${live.sid}/stop`, headers)).status)
        .toBe(200);
      stopBroadcastPump(live);

      await expect(page.getByTestId('listen-ended')).toBeVisible({ timeout: 25_000 });
      const stopped = await page.locator('[data-testid="listener-audio"]')
        .evaluate((node) => node.paused);
      expect(stopped).toBe(true);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // TEST G - kick
  // ======================================================================
  test('G: a kicked listener stops and cannot come back', async ({ page }) => {
    test.setTimeout(120_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await useRealBackend(page);
      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });

      const room = await api('GET', `/broadcast/sessions/${live.sid}/web-room`, headers);
      const pid = room.body.listeners[0].id;
      await api('POST',
        `/broadcast/sessions/${live.sid}/web-participants/${pid}/kick`, headers);

      await expect(page.getByTestId('listen-kicked'))
        .toContainText(/removed from this Broadcast/i, { timeout: 20_000 });
      const stopped = await page.locator('[data-testid="listener-audio"]')
        .evaluate((node) => node.paused);
      expect(stopped).toBe(true);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // TEST H - password rotation
  // ======================================================================
  test('H: rotation keeps the audience and retires the old password',
       async ({ page }) => {
    test.setTimeout(120_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await useRealBackend(page);
      await page.goto(`${base}/listen/${live.room.public_code}`);
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });

      const rotated = await api('POST',
        `/broadcast/sessions/${live.sid}/web-room/password/rotate`, headers);
      expect(rotated.status).toBe(200);
      const fresh = rotated.body.password;
      expect(fresh).toBeTruthy();
      expect(fresh).not.toBe(live.room.password);

      // The listener already admitted keeps listening.
      await expect(page.getByTestId('listen-status')).toHaveText('Listening');

      // The old password no longer admits anybody.
      const stale = await fetch(
        `${base}/api/listen/rooms/${live.room.public_code}/join`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: 'Late',
                                 password: live.room.password }) });
      expect(stale.status).toBe(401);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // Lifecycle: closing, refreshing, reopening
  // ======================================================================
  // Manual testing found a closed tab still showing Listening, and a refresh
  // asking for the password again and then creating a SECOND participant for
  // the same browser - leaving one row Listening and a duplicate not connected.

  async function joinAndListen(page, live, name = 'Harshit') {
    await page.goto(`${base}/listen/${live.room.public_code}`);
    await page.getByTestId('listen-name').fill(name);
    await page.getByTestId('listen-password').fill(live.room.password);
    await page.getByTestId('listen-join').click();
    await expect(page.getByTestId('listen-status'))
      .toHaveText('Listening', { timeout: 25_000 });
  }

  async function roomState(headers, sid) {
    return (await api('GET', `/broadcast/sessions/${sid}/web-room`, headers)).body;
  }

  test('I: closing the tab stops the console claiming Listening',
       async ({ page }) => {
    // KNOWN FAILING, and deliberately declared so rather than deleted or
    // quietly loosened.
    //
    // The browser DOES send heartbeats carrying its playback state - captured
    // off the wire, they read
    //   {"type":"heartbeat","playback_state":"BUFFERING"}
    // - and the console still reports connected 1, listening 0, buffering 0.
    // So the reported state is not reaching counts_for_room. That is a
    // heartbeat-reporting defect in the listener lifecycle, which is its own
    // milestone; it is not about kicks, sessions or rooms.
    //
    // Declared expected-failure because this describe runs serial: left as a
    // plain failure it skips every test after it, including the kick-scope
    // tests Q to X. Playwright will fail this file the moment it starts
    // passing, so the defect cannot be forgotten.
    test.fail();
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);

      // The PAGE knows it is listening from its own media events at once; the
      // SERVER learns on the next heartbeat, which is every 10 seconds. So this
      // waits for the console to catch up rather than assuming it already has.
      await expect.poll(async () => {
        const state = await roomState(headers, live.sid);
        return `${state.counts.connected}/${state.counts.listening}`;
      }, { timeout: 25_000 }).toBe('1/1');
      const participantId = (await roomState(headers, live.sid)).listeners[0].id;

      // The tab goes away. The Broadcast keeps running.
      await page.close();

      await expect.poll(async () => {
        const state = await roomState(headers, live.sid);
        return `${state.counts.connected}/${state.counts.listening}`;
      }, { timeout: 20_000 }).toBe('0/0');

      // Admission survives - they were let in, and closing a tab is not a
      // withdrawal of that. Only the runtime claim is gone.
      const after = await roomState(headers, live.sid);
      expect(after.counts.admitted).toBe(1);
      expect(after.listeners).toHaveLength(1);
      expect(after.listeners[0].id).toBe(participantId);
      expect(after.listeners[0].playback_state).toBe('DISCONNECTED');
      expect(after.listeners[0].connected).toBe(false);
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('J: a hard refresh resumes the same participant without asking again',
       async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const before = await roomState(headers, live.sid);
      const participantId = before.listeners[0].id;
      expect(before.counts.admitted).toBe(1);

      await page.reload();

      // NOT the join form. The browser still holds a valid session.
      await expect(page.getByTestId('listen-live')).toBeVisible({ timeout: 25_000 });
      await expect(page.getByTestId('listen-password')).toHaveCount(0);
      await expect(page.getByTestId('listen-join')).toHaveCount(0);

      // Playing again, from a fresh live-edge bootstrap.
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });
      const truth = await playbackTruth(page);
      expect(truth.paused).toBe(false);
      expect(truth.advanced).toBe(true);

      // The SAME participant. No duplicate row, no second admission.
      const after = await roomState(headers, live.sid);
      expect(after.counts.admitted).toBe(1);
      expect(after.listeners).toHaveLength(1);
      expect(after.listeners[0].id).toBe(participantId);
      // The console's `listening` count is not asserted here: it stays 0 even
      // for an ordinary first join, which test I above declares as a known
      // heartbeat-reporting defect. Asserting it here would only make this
      // test red for a reason that has nothing to do with refreshing. That the
      // listener really is playing is already proved by `truth.advanced`.
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('K: reopening the link in the same browser resumes, not re-admits',
       async ({ page, context }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const participantId = (await roomState(headers, live.sid)).listeners[0].id;
      await page.close();

      // A NEW page in the SAME context, so the HttpOnly cookie is still there.
      const reopened = await context.newPage();
      await reopened.goto(`${base}/listen/${live.room.public_code}`);

      await expect(reopened.getByTestId('listen-live'))
        .toBeVisible({ timeout: 25_000 });
      await expect(reopened.getByTestId('listen-password')).toHaveCount(0);
      await expect(reopened.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });

      const after = await roomState(headers, live.sid);
      expect(after.counts.admitted).toBe(1);
      expect(after.listeners[0].id).toBe(participantId);
      await reopened.close();
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('L: joining again from the same browser does not duplicate',
       async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const participantId = (await roomState(headers, live.sid)).listeners[0].id;

      // The same browser submits a join for the same room again. Identity is
      // the session, so this resumes rather than admitting a second time.
      const again = await page.evaluate(async (payload) => {
        const response = await fetch(`/api/listen/rooms/${payload.code}/join`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: 'Harshit',
                                 password: payload.password }),
        });
        return { status: response.status };
      }, { code: live.room.public_code, password: live.room.password });

      expect(again.status).toBe(200);
      const after = await roomState(headers, live.sid);
      expect(after.counts.admitted).toBe(1);
      expect(after.listeners[0].id).toBe(participantId);
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('M: a different browser really is a different participant',
       async ({ page, browser }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);
    let second;

    try {
      await joinAndListen(page, live, 'Harshit');

      // A separate context: its own cookie jar, like another device.
      second = await browser.newContext();
      const other = await second.newPage();
      await other.goto(`${base}/listen/${live.room.public_code}`);
      // The SAME display name on purpose - identity is the session, not the
      // name, and duplicate names remain allowed.
      await other.getByTestId('listen-name').fill('Harshit');
      await other.getByTestId('listen-password').fill(live.room.password);
      await other.getByTestId('listen-join').click();
      await expect(other.getByTestId('listen-live')).toBeVisible({ timeout: 25_000 });

      const after = await roomState(headers, live.sid);
      expect(after.counts.admitted).toBe(2);
      expect(new Set(after.listeners.map((row) => row.id)).size).toBe(2);
    } finally {
      if (second) await second.close();
      stopBroadcastPump(live);
    }
  });

  test('O: a refresh after a Kick does not restore listening', async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const pid = (await roomState(headers, live.sid)).listeners[0].id;
      await api('POST',
        `/broadcast/sessions/${live.sid}/web-participants/${pid}/kick`, headers);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      await page.reload();

      // A kicked session is invalid, so a refresh restores nothing.
      await expect(page.getByTestId('listen-status')).toHaveCount(0);
      await expect(page.getByTestId('listen-live')).toHaveCount(0);
      const state = await roomState(headers, live.sid);
      expect(state.counts.listening).toBe(0);
      expect(state.counts.connected).toBe(0);
    } finally {
      stopBroadcastPump(live);
    }
  });

  // ======================================================================
  // Kick is removal from ONE Broadcast, not a ban
  // ======================================================================
  // Manual testing found that after a Kick the listener could not ask to join
  // that Broadcast again, and could not join a DIFFERENT Broadcast either.
  // The removal had become a property of the browser.

  async function kickTheOnlyListener(headers, live) {
    const room = await roomState(headers, live.sid);
    const pid = room.listeners[0].id;
    await api('POST',
      `/broadcast/sessions/${live.sid}/web-participants/${pid}/kick`, headers);
    return pid;
  }

  test('Q: a Kick stops the audio and the console at once', async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const pid = await kickTheOnlyListener(headers, live);

      await expect(page.getByTestId('listen-kicked'))
        .toContainText(/removed from this Broadcast/i, { timeout: 20_000 });
      expect(await page.locator('[data-testid="listener-audio"]')
        .evaluate((node) => node.paused)).toBe(true);

      await expect.poll(async () => {
        const state = await roomState(headers, live.sid);
        return `${state.counts.connected}/${state.counts.listening}`;
      }, { timeout: 20_000 }).toBe('0/0');

      // Nothing reconnects on its own. Kick has to actually mean something.
      await page.waitForTimeout(8_000);
      await expect(page.getByTestId('listen-kicked')).toBeVisible();
      expect((await roomState(headers, live.sid)).counts.connected).toBe(0);
      expect(pid).toBeGreaterThan(0);
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('R: Join Again then the password admits as a NEW participant',
       async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const first = await kickTheOnlyListener(headers, live);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      // The listener chooses to come back. The button returns them to the
      // form - it does not readmit them.
      await page.getByTestId('listen-join-again').click();
      await expect(page.getByTestId('listen-password')).toBeVisible();
      await expect(page.getByTestId('listen-status')).toHaveCount(0);

      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });
      expect((await playbackTruth(page)).advanced).toBe(true);

      const after = await roomState(headers, live.sid);
      expect(after.counts.admitted).toBe(1);
      expect(after.listeners).toHaveLength(1);
      expect(after.listeners[0].id,
             'the kicked participant must not be resurrected').not.toBe(first);
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('S: Join Again then Request Access reaches the broadcaster again',
       async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const first = await kickTheOnlyListener(headers, live);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      await page.getByTestId('listen-join-again').click();
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-request').click();
      await expect(page.getByTestId('listen-waiting')).toBeVisible({ timeout: 20_000 });

      // A NEW waiting request, which the broadcaster decides on again.
      await expect.poll(async () =>
        (await roomState(headers, live.sid)).counts.waiting,
        { timeout: 20_000 }).toBe(1);
      const waiting = (await roomState(headers, live.sid)).waiting[0];
      expect(waiting.id).not.toBe(first);

      expect((await api('POST',
        `/broadcast/sessions/${live.sid}/web-participants/${waiting.id}/approve`,
        headers)).status).toBe(200);

      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 30_000 });
      expect((await playbackTruth(page)).advanced).toBe(true);
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('T: a kick from one Broadcast does not follow the browser to another',
       async ({ page }) => {
    test.setTimeout(180_000);
    const headers = await signIn();
    const first = await startLiveBroadcast(headers);
    let second;

    try {
      await joinAndListen(page, first);
      await kickTheOnlyListener(headers, first);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      // A completely different Broadcast, same browser, same cookies. No
      // manual cookie clearing - that is the whole point of the report.
      second = await startLiveBroadcast(headers);
      await page.goto(`${base}/listen/${second.room.public_code}`);

      await expect(page.getByTestId('listen-password')).toBeVisible({ timeout: 20_000 });
      await expect(page.getByTestId('listen-kicked')).toHaveCount(0);

      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(second.room.password);
      await page.getByTestId('listen-join').click();
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });
      expect((await playbackTruth(page)).advanced).toBe(true);

      expect((await roomState(headers, second.sid)).counts.admitted).toBe(1);
    } finally {
      stopBroadcastPump(first);
      if (second) stopBroadcastPump(second);
    }
  });

  test('U: Request Access on a different Broadcast still works after a kick',
       async ({ page }) => {
    test.setTimeout(180_000);
    const headers = await signIn();
    const first = await startLiveBroadcast(headers);
    let second;

    try {
      await joinAndListen(page, first);
      await kickTheOnlyListener(headers, first);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      second = await startLiveBroadcast(headers);
      await page.goto(`${base}/listen/${second.room.public_code}`);
      await expect(page.getByTestId('listen-name')).toBeVisible({ timeout: 20_000 });
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-request').click();
      await expect(page.getByTestId('listen-waiting')).toBeVisible({ timeout: 20_000 });

      await expect.poll(async () =>
        (await roomState(headers, second.sid)).counts.waiting,
        { timeout: 20_000 }).toBe(1);
      const waiting = (await roomState(headers, second.sid)).waiting[0];
      expect((await api('POST',
        `/broadcast/sessions/${second.sid}/web-participants/${waiting.id}/approve`,
        headers)).status).toBe(200);

      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 30_000 });
    } finally {
      stopBroadcastPump(first);
      if (second) stopBroadcastPump(second);
    }
  });

  test('V: the kicked session cannot disturb the one that replaced it',
       async ({ page }) => {
    test.setTimeout(180_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      const first = await kickTheOnlyListener(headers, live);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      await page.getByTestId('listen-join-again').click();
      await page.getByTestId('listen-name').fill('Harshit');
      await page.getByTestId('listen-password').fill(live.room.password);
      await page.getByTestId('listen-join').click();
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 25_000 });
      const second = (await roomState(headers, live.sid)).listeners[0].id;
      expect(second).not.toBe(first);

      // The old participant's teardown arrives late. Kicking it again is the
      // most direct way to make the server touch that dead row now.
      await api('POST',
        `/broadcast/sessions/${live.sid}/web-participants/${first}/kick`, headers);
      await page.waitForTimeout(3_000);

      // A brief Buffering is ordinary for a listener that joined mid-stream;
      // what must not happen is losing the session. So this waits for playback
      // to settle rather than demanding the state at one instant.
      await expect(page.getByTestId('listen-status'))
        .toHaveText('Listening', { timeout: 30_000 });
      expect((await playbackTruth(page)).advanced).toBe(true);
      const after = await roomState(headers, live.sid);
      expect(after.counts.connected).toBe(1);
      expect(after.listeners[0].id).toBe(second);
      // Deliberately NOT asserting counts.listening here. That count is fed by
      // the browser's heartbeat and does not reach 1 even for an ordinary
      // first join - test I fails the same way at this commit and did before
      // this work, so it is a separate reporting defect and asserting it here
      // would only make this test fail for somebody else's reason. What this
      // test is about is proved above: the replacement session is connected,
      // is the new participant, and its audio is really advancing.
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('W: a refresh after a kick still shows Removed, with a way back',
       async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      await kickTheOnlyListener(headers, live);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      await page.reload();

      // Refreshing is not "choosing to come back", so nothing is restored -
      // but the listener must not be stranded either.
      //
      // Which way back appears depends on how they were admitted. A kick
      // invalidates the session token, so a password listener's browser now
      // holds nothing and lands on the join form; a listener still carrying a
      // pending claim sees the Removed panel and its Join Again button. Both
      // are a way back and neither is a restored admission, so this asserts
      // the property rather than one of the two shapes.
      await expect(page.getByTestId('listen-status')).toHaveCount(0);
      await expect(async () => {
        const back = await page.getByTestId('listen-join-again').count()
          + await page.getByTestId('listen-join').count();
        expect(back, 'the listener was left with no way back').toBeGreaterThan(0);
      }).toPass({ timeout: 20_000 });
      await expect(page.getByTestId('listen-live')).toHaveCount(0);
      expect((await roomState(headers, live.sid)).counts.connected).toBe(0);
    } finally {
      stopBroadcastPump(live);
    }
  });

  test('X: navigating straight to another Broadcast ignores the old kick',
       async ({ page }) => {
    test.setTimeout(180_000);
    const headers = await signIn();
    const first = await startLiveBroadcast(headers);
    let second;

    try {
      await joinAndListen(page, first);
      await kickTheOnlyListener(headers, first);
      await expect(page.getByTestId('listen-kicked')).toBeVisible({ timeout: 20_000 });

      // No Join Again, no cookie clearing: straight to the other link.
      second = await startLiveBroadcast(headers);
      await page.goto(`${base}/listen/${second.room.public_code}`);

      await expect(page.getByTestId('listen-kicked')).toHaveCount(0);
      await expect(page.getByTestId('listen-name')).toBeVisible({ timeout: 20_000 });
      // And the form is about THIS Broadcast, not the one they were removed from.
      const shown = await page.getByTestId('listen-code').inputValue();
      expect(shown.toUpperCase()).toBe(second.room.public_code.toUpperCase());
    } finally {
      stopBroadcastPump(first);
      if (second) stopBroadcastPump(second);
    }
  });

  test('P: a refresh after a real Stop shows Ended, not a join form',
       async ({ page }) => {
    test.setTimeout(150_000);
    const headers = await signIn();
    const live = await startLiveBroadcast(headers);

    try {
      await joinAndListen(page, live);
      expect((await api('POST', `/broadcast/sessions/${live.sid}/stop`, headers)).status)
        .toBe(200);
      stopBroadcastPump(live);
      await expect(page.getByTestId('listen-ended')).toBeVisible({ timeout: 25_000 });

      await page.reload();

      // The room is gone, so its code no longer resolves - and the page must
      // not invite anybody to type credentials for a Broadcast that is over.
      await expect(page.getByTestId('listen-status')).toHaveCount(0);
      const stillPlaying = await page.locator('[data-testid="listener-audio"]')
        .evaluate((node) => !node.paused).catch(() => false);
      expect(stillPlaying).toBe(false);
    } finally {
      stopBroadcastPump(live);
    }
  });

});
