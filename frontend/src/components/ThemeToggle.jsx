import React from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "@/contexts/ThemeContext";

/**
 * Three states, all three visible.
 *
 * Not a two-way switch. A switch can only ever say light or dark, so the
 * moment somebody touches it they have silently opted out of following their
 * machine - and there is then no way back to "whatever my computer does"
 * except clearing site data. System is a real choice and it is on the
 * control.
 */
export default function ThemeToggle({ compact = false }) {
  const { choice, choose } = useTheme();

  const options = [
    { value: "system", label: "System", icon: Monitor },
    { value: "light", label: "Light", icon: Sun },
    { value: "dark", label: "Dark", icon: Moon },
  ];

  return (
    <div role="group" aria-label="Theme" data-testid="theme-toggle"
         // The corners have to agree.
         //
         // A 10px shell holding 6px buttons with a 2px gap leaves the chosen
         // pill's corner sitting inside a differently-curved corner, and the
         // eye reads that as a misprint. The rule is: inner radius = outer
         // radius minus the padding, so the two curves are concentric.
         className="glass inline-flex items-center gap-1 overflow-hidden p-1"
         style={{ borderRadius: 12 }}>
      {options.map(({ value, label, icon: Icon }) => {
        const chosen = choice === value;
        return (
          <button key={value} type="button"
                  onClick={() => choose(value)}
                  data-testid={`theme-${value}`}
                  aria-pressed={chosen}
                  title={value === "system"
                    ? "Follow this computer's setting"
                    : `Always ${label.toLowerCase()}`}
                  style={{ borderRadius: 8 }}
                  className={`inline-flex h-7 items-center justify-center gap-1
                              px-2 text-xs leading-none transition-colors ${
                    chosen
                      // Enough contrast to be unmistakably the chosen one on
                      // BOTH a light and a dark sidebar, which is the one
                      // place this control ever sits.
                      ? "bg-blue-500 text-white shadow-sm"
                      // On a dark rail in both themes, so the unchosen ones
                      // are a step brighter than a normal muted label: grey on
                      // grey was unreadable in light mode.
                      : "text-slate-300 hover:bg-surface/10 hover:text-white"}`}>
            <Icon className="h-3.5 w-3.5 shrink-0" />
            {!compact && <span>{label}</span>}
            {/* The name is still there for a screen reader when the control
                is showing icons only - an unlabelled icon triplet is three
                shapes nobody has to guess at. */}
            {compact && <span className="sr-only">{label}</span>}
          </button>
        );
      })}
    </div>
  );
}
