import React from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { Radio, LayoutDashboard, Store as StoreIcon, History, Radar, ScrollText, LogOut, Menu, X } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

const NAV = [
  { to: "/console", label: "Broadcast Console", icon: LayoutDashboard, testid: "nav-console" },
  { to: "/stores", label: "Store Management", icon: StoreIcon, testid: "nav-stores" },
  { to: "/history", label: "Broadcast History", icon: History, testid: "nav-history" },
  { to: "/receivers", label: "Receiver Status", icon: Radar, testid: "nav-receivers" },
  { to: "/logs", label: "System Logs", icon: ScrollText, testid: "nav-logs" },
];

export default function Layout() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = React.useState(false);

  const handleLogout = () => { logout(); navigate("/login"); };

  return (
    <div className="min-h-screen flex bg-slate-50">
      {/* Sidebar */}
      <aside className={`${open ? "translate-x-0" : "-translate-x-full"} md:translate-x-0 fixed md:static z-40 w-64 h-screen bg-slate-900 text-slate-100 flex flex-col transition-transform`}>
        <div className="h-16 px-5 flex items-center gap-2 border-b border-slate-800">
          <Radio className="text-red-500" size={22} />
          <div>
            <div className="font-bold tracking-tight text-white text-lg leading-none">EchoCast</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-slate-400 mt-0.5">Live Broadcast</div>
          </div>
        </div>
        <nav className="flex-1 px-3 py-4 space-y-1">
          {NAV.map((n) => (
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
        <div className="p-3 border-t border-slate-800">
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
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-16 bg-white border-b border-slate-200 flex items-center justify-between px-4 md:px-6">
          <button
            data-testid="sidebar-toggle-btn"
            className="md:hidden p-2 rounded-md hover:bg-slate-100"
            onClick={() => setOpen((o) => !o)}
          >
            {open ? <X size={20} /> : <Menu size={20} />}
          </button>
          <div className="text-xs uppercase tracking-[0.15em] text-slate-500">HQ Broadcast Console · v1.0</div>
          <div className="text-xs text-slate-500 hidden sm:block">Windows 11 · Local Server · SQLite</div>
        </header>
        <main className="flex-1 p-4 md:p-6 overflow-x-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
