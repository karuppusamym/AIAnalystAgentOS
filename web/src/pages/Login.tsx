import { useEffect, useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { api, errorMessage, type AuthProviders } from "../api";
import { useAuth } from "../auth";
import { ErrorBox } from "../components/ui";

export function LoginPage() {
  const { user, login, loginWithToken } = useAuth();
  const nav = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [providers, setProviders] = useState<AuthProviders | null>(null);
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  useEffect(() => {
    api.authProviders().then(setProviders).catch(() => setProviders(null));
  }, []);

  // Single sign-on callback: the API redirects here with #sso_token=… (or #sso_error=…) in the fragment.
  useEffect(() => {
    const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
    const token = hash.get("sso_token");
    const ssoError = hash.get("sso_error");
    if (!token && !ssoError) return;
    window.history.replaceState(null, "", window.location.pathname);
    if (ssoError) {
      setError(ssoError);
      return;
    }
    const target = hash.get("return_to") || "/";
    loginWithToken(token as string)
      .then(() => nav(target.startsWith("/") && !target.startsWith("//") ? target : "/", { replace: true }))
      .catch((err) => setError(errorMessage(err)));
  }, [loginWithToken, nav]);

  if (user) return <Navigate to={from} replace />;
  // An unreachable or malformed answer keeps password sign-in available rather than blanking the form.
  const sso = providers?.oidc?.enabled ? providers.oidc : null;
  const passwordEnabled = providers?.password !== false;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password);
      nav(from, { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card card" onSubmit={submit} aria-labelledby="login-title">
        <div className="brand login-brand"><span className="brand-mark" aria-hidden="true">◆</span> Context2AI <strong>AnalystOS</strong></div>
        <h1 id="login-title">Sign in</h1>
        <p className="muted">Autonomous, governed analytics — every finding comes with its evidence.</p>
        {sso && sso.login_url && (
          <a className="btn btn-block" href={`${sso.login_url}?return_to=${encodeURIComponent(from)}`}>Sign in with {sso.name}</a>
        )}
        {passwordEnabled && (<>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" type="password" autoComplete="current-password" required value={password}
            onChange={(e) => setPassword(e.target.value)} />
        </div>
        </>)}
        <ErrorBox error={error} />
        {passwordEnabled && (
          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        )}
      </form>
    </div>
  );
}
