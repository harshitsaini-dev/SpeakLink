import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { Radio, LayoutDashboard, Store as StoreIcon, History, Radar, HardDrive, ScrollText, Users, KeyRound, LogOut, Menu, X, Signal } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { MENU_PERMISSION_BY_PATH } from "@/lib/menuPermissions";
import RecordingPlayer, { PLAYER_BAR_HEIGHT } from "@/components/RecordingPlayer";
import { useRecordingPlayback } from "@/contexts/RecordingPlaybackContext";

const NAV = [
  { to: "/console", label: "Broadcast Console", icon: LayoutDashboard, testid: "nav-console" },
  // Supervision, not broadcasting. Hidden unless broadcast.active_view is
  // held - and ProtectedRoute blocks the URL independently, because a hidden
  // link is presentation and never a boundary.
  { to: "/active-broadcasts", label: "Active Broadcasts", icon: Signal, testid: "nav-active-broadcasts" },
  { to: "/stores", label: "Store Management", icon: StoreIcon, testid: "nav-stores" },
  { to: "/history", label: "Broadcast History", icon: History, testid: "nav-history" },
  { to: "/receivers", label: "Receiver Status", icon: Radar, testid: "nav-receivers" },
  { to: "/devices", label: "Receiver Devices", icon: HardDrive, testid: "nav-devices" },
  { to: "/logs", label: "System Logs", icon: ScrollText, testid: "nav-logs" },
  // Shown only to accounts holding menu.users.view (OWNER/ADMIN by default,
  // and per-user overrides can change that). This is presentation, not
  // protection - the backend answers 403 to the request either way, and a
  // navigation link is not a permission check. ProtectedRoute enforces the
  // same MENU_PERMISSION_BY_PATH map against a direct URL visit.
  { to: "/users", label: "User Management", icon: Users, testid: "nav-users" },
  // Everybody, deliberately: read-only does not mean unable to secure your own
  // account.
  { to: "/account/password", label: "Change Password", icon: KeyRound, testid: "nav-password" },
];

export default function Layout() {
  const { user, logout, can } = useAuth();
  const { active, playToken, pauseToken, stopPlayback } =
    useRecordingPlayback();
  const navigate = useNavigate();
  const [open, setOpen] = React.useState(false);

  const handleLogout = () => { logout(); navigate("/login"); };
  const visibleNav = NAV.filter((n) => {
    const permission = MENU_PERMISSION_BY_PATH[n.to];
    return !permission || can(permission);
  });

  return (
    // The shell is exactly one viewport tall and never scrolls itself, so the
    // document is never the vertical scroll owner. Main owns scrolling.
    <div className="h-screen bg-slate-50 overflow-hidden">
      {/* Sidebar */}
      {/* FIXED to the viewport, on every screen size and in every state.
          It was md:sticky, which keeps a sticky element in normal flow and
          leaves its position dependent on which ancestor happens to scroll.
          Fixed takes it out of flow entirely and anchors it to the viewport, so
          no page, modal or live state can move it. Because it is out of flow,
          the main shell carries the matching md:ml-64 offset below. */}
      <aside
        data-testid="app-sidebar"
        className={`${open ? "translate-x-0" : "-translate-x-full"} md:translate-x-0
                    fixed inset-y-0 left-0 z-40 w-64 h-screen
                    bg-slate-900 text-slate-100 flex flex-col transition-transform`}>
        {/* Brand and account never scroll away: only the middle list does. */}
        <div className="h-16 shrink-0 px-5 flex items-center gap-2 border-b border-slate-800">
          <Radio className="text-red-500" size={22} />
          <div>
            <div className="font-bold tracking-tight text-white text-lg leading-none">SpeakLink</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-slate-400 mt-0.5">Live Broadcast</div>
          </div>
        </div>
        <nav className="flex-1 min-h-0 overflow-y-auto px-3 py-4 space-y-1">
          {visibleNav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              data-testid={n.testid}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-colors ${
                  isActive ? "bg-blue-700 text-white" : "text-slate-300 hover:bg-slate-800 hover:text-white"
                }`
              }
            >
              <n.icon size={18} />
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="shrink-0 p-3 border-t border-slate-800">
          <div className="px-3 py-2 mb-2">
            <div className="text-xs text-slate-400">Signed in as</div>
            <div className="text-sm font-medium text-white">{user?.username}</div>
          </div>
          <button
            data-testid="logout-btn"
            onClick={handleLogout}
            className="w-full flex items-center gap-2 px-3 py-2 text-sm rounded-md text-slate-300 hover:bg-slate-800 hover:text-white transition-colors"
          >
            <LogOut size={16} /> Log out
          </button>
        </div>
      </aside>

      {/* Overlay for mobile */}
      {open && <div className="fixed inset-0 bg-black/40 z-30 md:hidden" onClick={() => setOpen(false)} />}

      {/* Main */}
      {/* md:ml-64 matches the w-64 sidebar exactly. A fixed sidebar is out of
          flow, so without this offset the page would sit underneath it. */}
      <div className="md:ml-64 flex flex-col min-w-0 h-screen">
        <header className="h-16 shrink-0 bg-white border-b border-slate-200 flex items-center justify-between px-4 md:px-6">
          <button
            data-testid="sidebar-toggle-btn"
            className="md:hidden p-2 rounded-md hover:bg-slate-100"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? <X size={20} /> : <Menu size={20} />}
          </button>
          <div className="text-xs uppercase tracking-[0.15em] text-slate-500">HQ Broadcast Console · v1.0</div>
        </header>
        {/* Room reserved across EVERY page while the player is up, so no
            table, pagination control or form action ends up underneath it. */}
        <main className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden p-4 md:p-6"
              style={active ? { paddingBottom: PLAYER_BAR_HEIGHT + 24 } : undefined}>
          <Outlet />
        </main>
      </div>

      {/* ONE player for the application. It lives here rather than inside
          Broadcast History because a recording an operator is listening to is
          not a property of the page they happen to be on - navigating to
          Receiver Status used to stop the audio and lose their place. */}
      <RecordingPlayer
        session={active}
        playToken={playToken}
        pauseToken={pauseToken}
        onClose={stopPlayback} />
    </div>
  );
}
