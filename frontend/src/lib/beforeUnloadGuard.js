/**
 * Native browser "are you sure you want to leave" protection, installed only
 * while a broadcast is actively LIVE.
 *
 * Modern browsers ignore any custom string passed to `beforeunload` and show
 * their own fixed confirmation text - so this deliberately does not attempt
 * to promise a custom sentence. What it does guarantee: exactly one listener
 * exists while `sync(true)` has most recently been called, and exactly zero
 * while `sync(false)` has - so IDLE never warns, repeated start/stop never
 * stacks duplicate handlers, and a normal Stop/Emergency Stop removes it
 * immediately.
 */
export function createBeforeUnloadGuard(target = typeof window !== "undefined" ? window : null) {
  let installed = false;

  const handler = (event) => {
    event.preventDefault();
    // Both are required: Chrome reads the return value, older engines read
    // `returnValue`. Neither controls the text the browser actually shows.
    event.returnValue = "";
    return "";
  };

  return {
    /** Reconcile the installed state with whether a broadcast is active. */
    sync(isLive) {
      if (!target) return;
      if (isLive && !installed) {
        target.addEventListener("beforeunload", handler);
        installed = true;
      } else if (!isLive && installed) {
        target.removeEventListener("beforeunload", handler);
        installed = false;
      }
    },
    /** Unconditionally remove the listener, e.g. on provider teardown. */
    teardown() {
      if (installed && target) target.removeEventListener("beforeunload", handler);
      installed = false;
    },
    isInstalled: () => installed,
  };
}
