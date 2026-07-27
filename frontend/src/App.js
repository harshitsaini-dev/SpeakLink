import React from "react";
import "@/App.css";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { AuthProvider } from "@/contexts/AuthContext";
import ProtectedRoute from "@/components/ProtectedRoute";
import Layout from "@/components/Layout";
import Login from "@/pages/Login";
import BroadcastConsole from "@/pages/BroadcastConsole";
import StoreManagement from "@/pages/StoreManagement";
import BroadcastHistory from "@/pages/BroadcastHistory";
import ReceiverStatus from "@/pages/ReceiverStatus";
import ReceiverDevices from "@/pages/ReceiverDevices";
import SystemLogs from "@/pages/SystemLogs";
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
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Navigate to="/console" replace />} />
            <Route path="/console" element={<BroadcastConsole />} />
            <Route path="/stores" element={<StoreManagement />} />
            {/* The Store id is in the path, never a credential. Both secrets
                this page can show - an enrolment code and a rotated credential -
                live in component state only, so a reload loses them. */}
            <Route path="/stores/:storeId/devices" element={<ReceiverDevices />} />
            <Route path="/history" element={<BroadcastHistory />} />
            <Route path="/receivers" element={<ReceiverStatus />} />
            <Route path="/logs" element={<SystemLogs />} />
          </Route>
          <Route path="*" element={<Navigate to="/console" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
