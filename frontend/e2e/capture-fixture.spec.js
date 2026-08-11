// Not a regression test: a generator that produces the real MediaRecorder
// capture the backend Cluster-framer tests replay. Skipped unless asked for.
const { test } = require('@playwright/test');
const { captureLiveWebm } = require('./support/capture-webm-stream');

test('capture a real MediaRecorder WebM stream for backend tests', async ({ page }) => {
  test.skip(!process.env.SPEAKLINK_CAPTURE_WEBM, 'set SPEAKLINK_CAPTURE_WEBM=1 to regenerate');
  test.setTimeout(120_000);
  await page.goto('/');
  const result = await captureLiveWebm(page, { durationMs: 12_000 });
  console.log('captured', result.chunkSizes.length, 'chunks ->', result.file);
});
