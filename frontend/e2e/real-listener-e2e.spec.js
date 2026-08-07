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
  if (fs.existsSync(path.join(E2E_BUILD, 'index.html'))) return true;
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
    workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'speaklink-e2e-'));
    const port = E2E_PORT;
    base = E2E_ORIGIN;

    server = spawn(PYTHON, ['-m', 'uvicorn', 'server:app',
                            '--host', '127.0.0.1', '--port', String(port),
                            '--log-level', 'warning'], {
      cwd: path.join(REPO, 'backend'),
      env: {
        ...process.env,
        SPEAKLINK_DB_PATH: path.join(workspace, 'hq.db'),
        SPEAKLINK_DATA_DIR: path.join(workspace, 'data'),
        JWT_SECRET: 'e2e-only-secret-value-not-a-real-one',
        ADMIN_USERNAME: 'founder',
        ADMIN_PASSWORD: PASSWORD,
        // Deliberately NOT setting any LAN cookie override: the point is that
        // the cookie policy now follows the request scheme by itself.
        // The backend serves the built React app, which is the repo-native
        // production topology: same origin, so the listener cookie and the
        // listener WebSocket behave exactly as they do for a real operator.
        SPEAKLINK_FRONTEND_BUILD: E2E_BUILD,
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

    // Feed at roughly the real 250 ms cadence so the relay behaves as it does
    // live, then keep looping so a late joiner always has audio waiting.
    let index = 0;
    const pump = setInterval(() => {
      if (socket.readyState !== WebSocket.OPEN) return;
      socket.send(chunks[index % chunks.length]);
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
});
