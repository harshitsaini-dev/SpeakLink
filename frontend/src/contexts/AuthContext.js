import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
import { api, getToken, setToken, clearToken } from "@/lib/api";

const AuthCtx = createContext(null);

/**
 * The one authoritative permission set for the signed-in account.
 *
 * Fetched from GET /auth/permissions - the same resolver the backend uses on
 * every protected request - so the frontend never re-derives "what can this
 * role do" from the role string itself. Hiding a menu item or disabling a
 * button with `can()` is a courtesy to the person looking at the screen; the
 * backend enforces the same rule again, independently, on every request.
 */
export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [permissions, setPermissions] = useState(new Set());
  const [loading, setLoading] = useState(true);

  const loadPermissions = useCallback(async () => {
    try {
      const { data } = await api.get("/auth/permissions");
      setPermissions(new Set(data.permissions || []));
    } catch {
      // A failed fetch must not silently grant anything - an empty set means
      // every can() check returns false, which is the fail-closed default.
      setPermissions(new Set());
    }
  }, []);

  useEffect(() => {
    (async () => {
      if (!getToken()) { setLoading(false); return; }
      try {
        const { data } = await api.get("/auth/me");
        setUser(data);
        await loadPermissions();
      } catch {
        clearToken();
      } finally {
        setLoading(false);
      }
    })();
  }, [loadPermissions]);

  const login = async (username, password) => {
    const { data } = await api.post("/auth/login", { username, password });
    setToken(data.access_token);
    setUser(data.user);
    await loadPermissions();
    return data.user;
  };

  const logout = () => { clearToken(); setUser(null); setPermissions(new Set()); };

  const can = (code) => permissions.has(code);

  return (
    <AuthCtx.Provider value={{ user, loading, login, logout, permissions, can, refreshPermissions: loadPermissions }}>
      {children}
    </AuthCtx.Provider>
  );
}

export const useAuth = () => useContext(AuthCtx);
