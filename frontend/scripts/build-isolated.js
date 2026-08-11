#!/usr/bin/env node
/**
 * A production build that CANNOT touch the live HQ frontend.
 *
 * Port 8000 serves frontend/build straight from disk, so an ordinary
 * `craco build` - run purely to check that the code still compiles - silently
 * replaced the bundle the live HQ was serving, with no restart and no deploy
 * step to notice. Verification must not be able to do that.
 *
 * This writes to frontend/build-dev instead, via CRA's BUILD_PATH. It is a
 * Node wrapper rather than an inline `BUILD_PATH=... craco build` in
 * package.json because npm runs scripts through cmd.exe on Windows, where that
 * prefix syntax is not a variable assignment but a command that does not exist.
 *
 * Pass --out=<dir> to build somewhere else again; anything resolving to the
 * live build directory is refused rather than quietly redirected, because a
 * flag that silently means something other than what it says is worse than an
 * error.
 */
const { spawnSync } = require('child_process');
const path = require('path');

const FRONTEND = path.resolve(__dirname, '..');
const LIVE_BUILD = path.join(FRONTEND, 'build');
const DEFAULT_OUT = path.join(FRONTEND, 'build-dev');

const requested = process.argv.slice(2)
  .find((argument) => argument.startsWith('--out='));
const out = path.resolve(FRONTEND,
  requested ? requested.slice('--out='.length) : DEFAULT_OUT);

if (path.relative(LIVE_BUILD, out) === '') {
  console.error(
    'Refusing to build into frontend/build: that directory is served live by\n'
    + 'port 8000, and this command exists precisely so verification cannot\n'
    + 'change it. Use the real deployment path if you mean to deploy.');
  process.exit(2);
}

console.log(`Building into ${out}`);
const result = spawnSync('npx', ['craco', 'build'], {
  cwd: FRONTEND,
  env: { ...process.env, CI: process.env.CI ?? 'true', BUILD_PATH: out },
  stdio: 'inherit',
  shell: true,
});
process.exit(result.status === null ? 1 : result.status);
