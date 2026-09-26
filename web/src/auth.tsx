import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, session, type User } from "./api";

interface AuthState {
  user: User | null;
  login: (email: string, password: string) => Promise<void>;
  /** Single sign-on: the callback hands the AnalystOS token back in the URL fragment. */
  loginWithToken: (token: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(() => (session.token ? session.user : null));

  const logout = useCallback(() => {
    session.clear();
    setUser(null);
  }, []);

  useEffect(() => {
    session.onUnauthorized(logout);
    return () => session.onUnauthorized(null);
  }, [logout]);

  // Validate a stored token once on load (it may have expired).
  useEffect(() => {
    if (!session.token) return;
    api.me().then((u) => setUser(u)).catch(() => undefined);
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const resp = await api.login(email, password);
    session.set(resp.access_token, resp.user);
    setUser(resp.user);
  }, []);

  const loginWithToken = useCallback(async (token: string) => {
    session.set(token, null as unknown as User);
    try {
      const me = await api.me();
      session.set(token, me);
      setUser(me);
    } catch (err) {
      session.clear();
      throw err;
    }
  }, []);

  const value = useMemo(() => ({ user, login, loginWithToken, logout }), [user, login, loginWithToken, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
