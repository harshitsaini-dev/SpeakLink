/**
 * Light, dark, and following the machine.
 *
 * The interesting properties are all about what happens WITHOUT a click:
 * somebody who never opens the control should get their computer's setting
 * and keep getting it, and somebody who does open it should never have their
 * choice quietly overruled at sunset.
 */
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";

import { ThemeProvider, useTheme, resolveTheme } from "./ThemeContext";
import ThemeToggle from "@/components/ThemeToggle";

let systemQuery;

function stubMatchMedia({ dark }) {
  const listeners = new Set();
  systemQuery = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_event, fn) => listeners.add(fn),
    removeEventListener: (_event, fn) => listeners.delete(fn),
    addListener: (fn) => listeners.add(fn),
    removeListener: (fn) => listeners.delete(fn),
    // What the operating system does at sunset.
    change(nowDark) {
      systemQuery.matches = nowDark;
      listeners.forEach((fn) => fn({ matches: nowDark }));
    },
  };
  window.matchMedia = () => systemQuery;
}

function Probe() {
  const { choice, resolved } = useTheme();
  return <span data-testid="probe">{`${choice}:${resolved}`}</span>;
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
  stubMatchMedia({ dark: false });
});

test("with no choice made, it follows the machine", () => {
  stubMatchMedia({ dark: true });
  render(<ThemeProvider><Probe /></ThemeProvider>);
  expect(screen.getByTestId("probe").textContent).toBe("system:dark");
  expect(document.documentElement.classList.contains("dark")).toBe(true);
});

test("following is a standing instruction, not a reading taken at load", () => {
  // The machine flips at sunset. Somebody who never expressed a preference
  // asked for "whatever my computer does", and this is that.
  render(<ThemeProvider><Probe /></ThemeProvider>);
  expect(screen.getByTestId("probe").textContent).toBe("system:light");

  act(() => systemQuery.change(true));
  expect(screen.getByTestId("probe").textContent).toBe("system:dark");
  expect(document.documentElement.classList.contains("dark")).toBe(true);
});

test("a chosen theme is not overruled when the machine changes", () => {
  // A preference quietly undone by an operating system setting is
  // indistinguishable from a bug - and an HQ screen that flips to dark in the
  // middle of a broadcast is worse than either theme.
  render(
    <ThemeProvider>
      <ThemeToggle />
      <Probe />
    </ThemeProvider>);

  fireEvent.click(screen.getByTestId("theme-light"));
  expect(screen.getByTestId("probe").textContent).toBe("light:light");

  act(() => systemQuery.change(true));
  expect(screen.getByTestId("probe").textContent).toBe("light:light");
  expect(document.documentElement.classList.contains("dark")).toBe(false);
});

test("a choice survives a reload, and can be given back to the machine", () => {
  const { unmount } = render(
    <ThemeProvider><ThemeToggle /><Probe /></ThemeProvider>);
  fireEvent.click(screen.getByTestId("theme-dark"));
  unmount();

  stubMatchMedia({ dark: false });
  render(<ThemeProvider><ThemeToggle /><Probe /></ThemeProvider>);
  expect(screen.getByTestId("probe").textContent).toBe("dark:dark");

  // And System is on the control, so there is a way back. A two-way switch
  // would have made this state unreachable without clearing site data.
  fireEvent.click(screen.getByTestId("theme-system"));
  expect(screen.getByTestId("probe").textContent).toBe("system:light");
});

test("storage being unavailable does not break the application", () => {
  const getItem = jest.spyOn(Storage.prototype, "getItem")
    .mockImplementation(() => { throw new Error("private browsing"); });
  const setItem = jest.spyOn(Storage.prototype, "setItem")
    .mockImplementation(() => { throw new Error("private browsing"); });

  render(<ThemeProvider><ThemeToggle /><Probe /></ThemeProvider>);
  expect(screen.getByTestId("probe").textContent).toBe("system:light");
  fireEvent.click(screen.getByTestId("theme-dark"));
  expect(screen.getByTestId("probe").textContent).toBe("dark:dark");

  getItem.mockRestore();
  setItem.mockRestore();
});

test("resolving is pure, and system means system", () => {
  expect(resolveTheme("system", true)).toBe("dark");
  expect(resolveTheme("system", false)).toBe("light");
  expect(resolveTheme("dark", false)).toBe("dark");
  expect(resolveTheme("light", true)).toBe("light");
});
