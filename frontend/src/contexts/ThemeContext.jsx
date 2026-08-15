import React from "react";

/**
 * Light, dark, or whatever the machine is set to.
 *
 * SYSTEM IS THE DEFAULT, AND IT KEEPS FOLLOWING
 *
 * Somebody who has never opened this menu gets the theme their computer
 * already uses, and if they change that at sunset this follows - because
 * "system" is a standing instruction, not a one-off reading taken when the
 * page loaded.
 *
 * A CHOICE IS A CHOICE
 *
 * Once somebody picks light or dark, the system stops being consulted. A
 * preference that gets quietly overruled by an operating system setting is
 * indistinguishable from a bug, and an HQ machine that flips to dark at six
 * o'clock in the middle of a broadcast is worse than either theme.
 *
 * WHY THE CLASS IS ON <html>
 *
 * Tailwind's dark variant looks there, and so do the listener pages, which
 * render outside the signed-in shell. One element, set in one place, means
 * the public pages cannot end up with a different answer from the admin ones.
 */

const STORAGE_KEY = "speaklink.theme";

//: "system" is a mode, not a colour. Storing the RESOLVED colour would freeze
//: today's setting forever and quietly break the following behaviour.
export const THEMES = ["system", "light", "dark"];

const ThemeContext = React.createContext(null);

function prefersDark() {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export function resolveTheme(choice, systemIsDark) {
  if (choice === "light" || choice === "dark") return choice;
  return systemIsDark ? "dark" : "light";
}

export function applyTheme(resolved) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  // A short transition, and only while switching. Leaving it on would put a
  // fade behind every hover on every row in the product.
  root.classList.add("theme-switching");
  root.classList.toggle("dark", resolved === "dark");
  root.style.colorScheme = resolved;
  window.setTimeout(() => root.classList.remove("theme-switching"), 220);
}

export function ThemeProvider({ children }) {
  const [choice, setChoice] = React.useState(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      return THEMES.includes(saved) ? saved : "system";
    } catch {
      // Private browsing, or storage disabled. A theme is not worth failing
      // the application over.
      return "system";
    }
  });
  const [systemIsDark, setSystemIsDark] = React.useState(prefersDark);

  // Keep following the machine while the choice is "system".
  React.useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return undefined;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const listen = (event) => setSystemIsDark(event.matches);
    if (query.addEventListener) {
      query.addEventListener("change", listen);
      return () => query.removeEventListener("change", listen);
    }
    // Safari before 14 and jsdom.
    query.addListener(listen);
    return () => query.removeListener(listen);
  }, []);

  const resolved = resolveTheme(choice, systemIsDark);

  React.useEffect(() => { applyTheme(resolved); }, [resolved]);

  const choose = React.useCallback((next) => {
    const value = THEMES.includes(next) ? next : "system";
    setChoice(value);
    try { window.localStorage.setItem(STORAGE_KEY, value); } catch { /* ignore */ }
  }, []);

  const value = React.useMemo(
    () => ({ choice, resolved, choose, systemIsDark }),
    [choice, resolved, choose, systemIsDark]);

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

export function useTheme() {
  const value = React.useContext(ThemeContext);
  if (value) return value;
  // A component rendered outside the provider - a test mounting one page, or
  // the listener shell before it is wrapped. It still has to render, and
  // "light unless the machine says otherwise" is the honest fallback.
  return {
    choice: "system",
    resolved: prefersDark() ? "dark" : "light",
    choose: () => {},
    systemIsDark: prefersDark(),
  };
}
