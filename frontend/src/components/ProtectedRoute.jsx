import React from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { MENU_PERMISSION_BY_PATH, firstAllowedRoute } from "@/lib/menuPermissions";

/**
 * Menu hiding in Layout.jsx is a courtesy, not a boundary. This is the
 * boundary: visiting a protected route directly by URL - typed, bookmarked,
 * or reached by back-button - is blocked here even though no link to it was
 * ever rendered. The backend enforces the same rule again, independently, on
 * every request that route makes.
 */
export default function ProtectedRoute({ children }) {
  const { user, loading, can } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50 text-slate-500 text-sm">
        Loading…
      </div>
    );
  }
  if (!user) return <Navigate to="/login" replace />;

  const requiredPermission = MENU_PERMISSION_BY_PATH[location.pathname];
  if (requiredPermission && !can(requiredPermission)) {
    return <Navigate to={firstAllowedRoute(can)} replace />;
  }
  return children;
}
