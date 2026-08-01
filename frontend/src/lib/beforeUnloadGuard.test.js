/**
 * Defect: pressing F5/Refresh during a live broadcast reloaded the page
 * immediately and the broadcast stopped, with no warning at all.
 *
 * These tests use a fake event-target rather than jsdom's real `window` so
 * "exactly one handler" can be asserted directly instead of inferred from
 * side effects.
 */
import { createBeforeUnloadGuard } from "./beforeUnloadGuard";

function fakeTarget() {
  const listeners = [];
  return {
    listeners,
    addEventListener: (type, fn) => { if (type === "beforeunload") listeners.push(fn); },
    removeEventListener: (type, fn) => {
      if (type !== "beforeunload") return;
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    },
  };
}

test("IDLE: no protection is installed", () => {
  const target = fakeTarget();
  const guard = createBeforeUnloadGuard(target);

  guard.sync(false);

  expect(target.listeners).toHaveLength(0);
  expect(guard.isInstalled()).toBe(false);
});

test("ACTIVE broadcast: protection is installed", () => {
  const target = fakeTarget();
  const guard = createBeforeUnloadGuard(target);

  guard.sync(true);

  expect(target.listeners).toHaveLength(1);
  expect(guard.isInstalled()).toBe(true);
});

test("stopping removes the handler immediately", () => {
  const target = fakeTarget();
  const guard = createBeforeUnloadGuard(target);

  guard.sync(true);
  guard.sync(false);

  expect(target.listeners).toHaveLength(0);
});

test("repeated start/stop cycles never leave more than one handler installed", () => {
  const target = fakeTarget();
  const guard = createBeforeUnloadGuard(target);

  for (let i = 0; i < 5; i += 1) {
    guard.sync(true);
    guard.sync(true); // duplicate "still live" call, e.g. a re-render
    expect(target.listeners).toHaveLength(1);
    guard.sync(false);
    guard.sync(false); // duplicate "still stopped" call
    expect(target.listeners).toHaveLength(0);
  }
});

test("emergency stop (sync(false)) removes the handler the same as a normal stop", () => {
  const target = fakeTarget();
  const guard = createBeforeUnloadGuard(target);

  guard.sync(true);
  guard.sync(false); // models both Stop Broadcast and Emergency Stop paths
  expect(target.listeners).toHaveLength(0);
});

test("teardown removes an installed handler unconditionally", () => {
  const target = fakeTarget();
  const guard = createBeforeUnloadGuard(target);

  guard.sync(true);
  guard.teardown();

  expect(target.listeners).toHaveLength(0);
  expect(guard.isInstalled()).toBe(false);
});

test("the installed handler calls preventDefault and sets returnValue, without a custom sentence", () => {
  // Browsers ignore any custom string here and show their own fixed text, so
  // this only proves the handler participates in the native prompt - it does
  // not and cannot assert a specific message.
  const target = fakeTarget();
  const guard = createBeforeUnloadGuard(target);
  guard.sync(true);

  const event = { preventDefault: jest.fn(), returnValue: undefined };
  const handler = target.listeners[0];
  const result = handler(event);

  expect(event.preventDefault).toHaveBeenCalled();
  expect(event.returnValue).toBe("");
  expect(result).toBe("");
});
