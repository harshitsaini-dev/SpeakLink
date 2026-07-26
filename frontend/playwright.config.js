// Playwright browser regression coverage for the SpeakLink HQ console.
//
// These tests run the real React application in a real Chromium, but they mock
// the backend HTTP API with page.route. That is deliberate:
//
//   * the safety property that matters most - no microphone is opened until a
//     Receiver has actually acknowledged READY - lives entirely in frontend
//     JavaScript, so it can be proven here without any Store hardware;
//   * failure paths that are hard to produce against a live backend (a Receiver
//     that never reports READY, a denied microphone, a browser with no
//     WebM/Opus support) become ordinary, deterministic test cases.
//
// What these tests CANNOT prove: real microphone quality, Makook adapter
// connectivity, Bluetooth amplifier playback, or physical speaker audibility.
// Those require the hardware pilot and an operator's ears.

const { defineConfig, devices } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 7_000 },
  // One CRA dev server, so keep it serial and predictable.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [['list']],

  use: {
    baseURL: 'http://localhost:3000',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: {
          args: [
            // A synthetic microphone: no real audio device is ever opened, and
            // nothing can reach a Windows output endpoint or an amplifier.
            '--use-fake-ui-for-media-stream',
            '--use-fake-device-for-media-stream',
          ],
        },
      },
    },
  ],

  webServer: {
    command: 'yarn start',
    url: 'http://localhost:3000',
    // Reuse the operator's dev server if one is already up, so running these
    // tests never disturbs a pilot in progress.
    reuseExistingServer: true,
    timeout: 240_000,
    stdout: 'ignore',
    stderr: 'pipe',
    env: {
      BROWSER: 'none',
      // Every request is intercepted by page.route, so this value only has to
      // be a well-formed URL - no backend is contacted.
      REACT_APP_BACKEND_URL: 'http://localhost:8000',
    },
  },
});
