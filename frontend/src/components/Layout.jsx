import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { LayoutDashboard, Store as StoreIcon, History, Radar, HardDrive, ScrollText, Users, KeyRound, LogOut, Menu, X, Signal, Megaphone, ClipboardList, Music, Gauge } from "lucide-react";
import SpeakLinkMark from "@/components/SpeakLinkMark";
import { useAuth } from "@/contexts/AuthContext";
import { MENU_PERMISSION_BY_PATH } from "@/lib/menuPermissions";
import RecordingPlayer, { PLAYER_BAR_HEIGHT } from "@/components/RecordingPlayer";
import EmergencyStopControl from "@/components/EmergencyStopControl";
import { useRecordingPlayback } from "@/contexts/RecordingPlaybackContext";
import { formatIstClock } from "@/lib/time";

//: The sidebar, grouped by what a person is trying to DO.
//:
//: A flat list of nine links made the reader scan all nine every time, and it
//: put "Broadcast Console" - opened many times a day - next to "System Logs",
//: opened when something has already gone wrong. Grouping is not decoration:
//: it lets somebody find the thing they came for without reading the things
//: they did not.
//:
//: The order of the groups is the order of the day: what is on air now, then
//: the estate it plays to, then the records, then the settings. Groups whose
//: every link is hidden by permission do not render at all - a heading over
//: nothing tells a reader they are missing something without saying what.
const NAV_GROUPS = [
  {
    title: "Live",
    items: [
      { to: "/console", label: "Broadcast Console", icon: LayoutDashboard, testid: "nav-console" },
      // Supervision, not broadcasting. Hidden unless broadcast.active_view is
      // held - and ProtectedRoute blocks the URL independently, because a
      // hidden link is presentation and never a boundary.
      { to: "/active-broadcasts", label: "Active Broadcasts", icon: Signal, testid: "nav-active-broadcasts" },
      // Its own menu, not a tab inside the Console. A recorded announcement
      // runs for days with nobody present; a broadcast is somebody holding a
      // microphone.
      { to: "/announcements", label: "Announcements", icon: Megaphone, testid: "nav-announcements" },
    ],
  },
  {
    // "Master" rather than "Estate": these are the records everything else
    // refers to - the shops, the machines in them, the recordings and the
    // plans that use both. Live is what is happening; this is what it is
    // happening TO.
    title: "Master",
    items: [
      { to: "/stores", label: "Store Management", icon: StoreIcon, testid: "nav-stores" },
      { to: "/receivers", label: "Receiver Status", icon: Radar, testid: "nav-receivers" },
      { to: "/devices", label: "Receiver Devices", icon: HardDrive, testid: "nav-devices" },
      // The plan and the recordings sit here, not under Live. Neither of them
      // is happening: they are what a broadcast or an announcement is made
      // OUT of, and they are edited on the timescale of a campaign rather
      // than of a shift.
      { to: "/announcement-templates", label: "Templates", icon: ClipboardList,
        testid: "nav-announcement-templates" },
      { to: "/announcement-recordings", label: "Recordings", icon: Music,
        testid: "nav-announcement-recordings" },
    ],
  },
  {
    title: "Records",
    items: [
      { to: "/history", label: "Broadcast History", icon: History, testid: "nav-history" },
      // Beside Broadcast History rather than under Announcements: both answer
      // "what happened", and somebody looking for one will look where the
      // other is.
      { to: "/announcement-history", label: "Announcement History", icon: Megaphone,
        testid: "nav-announcement-history" },
      { to: "/logs", label: "System Logs", icon: ScrollText, testid: "nav-logs" },
    ],
  },
  {
    title: "Administration",
    items: [
      // Shown only to accounts holding menu.users.view. Presentation, not
      // protection - the backend answers 403 either way, and ProtectedRoute
      // enforces the same map against a direct URL visit.
      { to: "/users", label: "User Management", icon: Users, testid: "nav-users" },
      // Everybody, deliberately: read-only does not mean unable to secure
      // your own account.
      { to: "/account/password", label: "Change Password", icon: KeyRound, testid: "nav-password" },
    ],
  },
];

//: Ungrouped, and first. The dashboard is where somebody starts, before they
//: know which group they want - putting it inside "Live" made it look like one
//: of the live-operations pages rather than the way in to all of them.
const NAV_TOP = [
  { to: "/dashboard", label: "Dashboard", icon: Gauge, testid: "nav-dashboard" },
];

//: Flat, for anything that needs "every link" rather than the grouping.
const NAV = [...NAV_TOP, ...NAV_GROUPS.flatMap((group) => group.items)];

export default function Layout() {
  const { user, logout, can } = useAuth();
  const { active, playToken, pauseToken, stopPlayback } =
    useRecordingPlayback();
  const navigate = useNavigate();
  const [open, setOpen] = React.useState(false);
  // The wall clock, in the estate's timezone rather than the viewer's. Ticks
  // every second because an operator reads it while something is on air, and
  // a clock that is a minute stale is worse than no clock.
  const [clock, setClock] = React.useState(() => formatIstClock());
  React.useEffect(() => {
    const timer = setInterval(() => setClock(formatIstClock()), 1000);
    return () => clearInterval(timer);
  }, []);

  const handleLogout = () => { logout(); navigate("/login"); };
  const allowed = (item) => {
    const permission = MENU_PERMISSION_BY_PATH[item.to];
    return !permission || can(permission);
  };
  // A group with nothing visible in it is dropped entirely. A heading over an
  // empty space tells a reader something is missing without telling them what,
  // which is worse than the group simply not existing for that account.
  const visibleGroups = NAV_GROUPS
    .map((group) => ({ ...group, items: group.items.filter(allowed) }))
    .filter((group) => group.items.length > 0);

  return (
    // The shell is exactly one viewport tall and never scrolls itself, so the
    // document is never the vertical scroll owner. Main owns scrolling.
    <div data-testid="app-shell" className="h-screen bg-slate-50 overflow-hidden">
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
          <SpeakLinkMark className="text-blue-500" size={30} />
          <div>
            <div data-testid="sidebar-wordmark"
                 className="font-bold tracking-tight text-white text-lg leading-none">SpeakLink</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-slate-400 mt-0.5">Live Broadcast</div>
          </div>
        </div>
        <nav className="flex-1 min-h-0 overflow-y-auto px-3 py-4 space-y-4">
          {NAV_TOP.filter(allowed).length > 0 && (
            <div className="space-y-1" data-testid="nav-top">
              {NAV_TOP.filter(allowed).map((n) => (
                <NavLink key={n.to} to={n.to} data-testid={n.testid}
                         onClick={() => setOpen(false)}
                         className={({ isActive }) =>
                           `flex items-center gap-3 px-3 py-2.5 rounded-md text-sm font-medium transition-colors ${
                             isActive ? "bg-blue-700 text-white" : "text-slate-300 hover:bg-slate-800 hover:text-white"
                           }`}>
                  <n.icon size={18} />
                  {n.label}
                </NavLink>
              ))}
            </div>
          )}
          {visibleGroups.map((group) => (
            <div key={group.title} data-testid={`nav-group-${group.title.toLowerCase()}`}>
              <div className="px-3 pb-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-slate-500">
                {group.title}
              </div>
              <div className="space-y-1">
                {group.items.map((n) => (
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
              </div>
            </div>
          ))}
        </nav>
        <div className="shrink-0 p-3 border-t border-slate-800">
          {/* Above the account block, in reach from EVERY page. It used to be a
              card on the Console, which meant that stopping a broadcast that
              had gone wrong required first navigating to the page - at the one
              moment navigation is worth least. */}
          <div className="mb-3">
            <EmergencyStopControl />
          </div>
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
      <div data-testid="app-main-shell"
           className="md:ml-64 flex flex-col min-w-0 h-screen min-h-0 overflow-hidden">
        <header data-testid="app-header" className="h-16 shrink-0 bg-white border-b border-slate-200 flex items-center justify-between px-4 md:px-6">
          <button
            data-testid="sidebar-toggle-btn"
            className="md:hidden p-2 rounded-md hover:bg-slate-100"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? <X size={20} /> : <Menu size={20} />}
          </button>
          <div className="text-xs uppercase tracking-[0.15em] text-slate-500">HQ Broadcast Console · v1.0</div>
          <div data-testid="header-clock"
               className="ml-auto font-mono text-xs text-slate-600 sm:text-sm">
            {clock}
          </div>
        </header>
        {/* Room reserved across EVERY page while the player is up, so no
            table, pagination control or form action ends up underneath it. */}
        <main data-testid="app-main-scroll"
              className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden p-4 md:p-6"
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
