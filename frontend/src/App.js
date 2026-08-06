import React from "react";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "@/contexts/AuthContext";
import { BroadcastProvider } from "@/contexts/BroadcastContext";
import ProtectedRoute from "@/components/ProtectedRoute";
import Layout from "@/components/Layout";
import Login from "@/pages/Login";
import BroadcastConsole from "@/pages/BroadcastConsole";
import ActiveBroadcasts from "@/pages/ActiveBroadcasts";
import StoreManagement from "@/pages/StoreManagement";
import MasterVolume from "@/pages/MasterVolume";
import BroadcastHistory from "@/pages/BroadcastHistory";
import ReceiverStatus from "@/pages/ReceiverStatus";
import ReceiverDevices from "@/pages/ReceiverDevices";
import ReceiverDeviceFleet from "@/pages/ReceiverDeviceFleet";
import SystemLogs from "@/pages/SystemLogs";
import UserManagement from "@/pages/UserManagement";
import ChangePassword from "@/pages/ChangePassword";
// Receiver is intentionally not imported or routed - see the note below.

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          {/* /receiver is deliberately not routed. The page connected to
              /ws/receiver/{token}, a backend route that does not exist, and
              reaching it required a Store credential in the URL. A Receiver
              computer will earn its own credential through one-time enrolment
              instead; see RECEIVER_ENROLMENT.md. The component is kept for that
              rework rather than deleted. */}
          <Route
            element={
              <ProtectedRoute>
                <BroadcastProvider>
                  <Layout />
                </BroadcastProvider>
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to="/console" replace />} />
            <Route path="/console" element={<BroadcastConsole />} />
            {/* Supervision, gated by broadcast.active_view in
                MENU_PERMISSION_BY_PATH - which ProtectedRoute enforces on a
                direct URL visit, not only on the sidebar link. */}
            <Route path="/active-broadcasts" element={<ActiveBroadcasts />} />
            <Route path="/stores" element={<StoreManagement />} />
            <Route path="/master-volume" element={<MasterVolume />} />
            {/* The Store id is in the path, never a credential. Both secrets
                this page can show - an enrolment code and a rotated credential -
                live in component state only, so a reload loses them. */}
            <Route path="/stores/:storeId/devices" element={<ReceiverDevices />} />
            {/* The fleet-wide view. Same Devices, asked about the other way
                round: "where is that Device?" rather than "what is in this
                Store?". Enrolment and credentials stay on the per-Store page. */}
            <Route path="/devices" element={<ReceiverDeviceFleet />} />
            <Route path="/history" element={<BroadcastHistory />} />
            <Route path="/receivers" element={<ReceiverStatus />} />
            <Route path="/logs" element={<SystemLogs />} />
            {/* Reachable by any signed-in account, on purpose. Read-only does
                not mean unable to secure your own account. */}
            <Route path="/account/password" element={<ChangePassword />} />
            {/* Deliberately routed for every signed-in account rather than
                hidden behind a role check here. The page shows what this
                account may do and the backend answers 403 to anything else -
                a route that only administrators can reach in the router is a
                lock on a door with no walls. */}
            <Route path="/users" element={<UserManagement />} />
          </Route>
          <Route path="*" element={<Navigate to="/console" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
