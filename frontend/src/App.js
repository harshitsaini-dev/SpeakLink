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
import SystemLogs from "@/pages/SystemLogs";
import Receiver from "@/pages/Receiver";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/receiver" element={<Receiver />} />
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
