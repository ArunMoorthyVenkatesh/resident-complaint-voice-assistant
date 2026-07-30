import { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { isValidEmail, passwordChecks, passwordScore, passwordLabel } from './passwordStrength';
import './styles.css';

const API_BASE_URL = import.meta.env.VITE_API_URL || window.location.origin;
const AUTH_STORAGE_KEY = 'buildcare_auth';

// The Navigation Timing entry's "reload" type reflects the browser-level page
// load and doesn't change on client-side route changes -- so this must only be
// checked once per real page load, or Splash sending you back here after its
// timer would look like another reload and bounce you out again forever.
let hasCheckedReload = false;

export default function LoginApp() {
  const navigate = useNavigate();
  const location = useLocation();

  // A browser refresh (not client-side navigation) on this page sends you
  // back through the landing screen, same as logout does.
  useEffect(() => {
    if (hasCheckedReload) return;
    hasCheckedReload = true;
    const navEntry = performance.getEntriesByType('navigation')[0];
    if (navEntry?.type === 'reload') {
      navigate('/', { replace: true });
    }
  }, [navigate]);

  const [mode, setMode] = useState('login'); // 'login' | 'signup'
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState('user');
  const [error, setError] = useState('');
  const [pendingMsg, setPendingMsg] = useState(location.state?.pendingMsg || '');
  const [busy, setBusy] = useState(false);

  const switchMode = (m) => {
    setMode(m);
    setError('');
    setPendingMsg('');
  };

  const emailTouched = email.length > 0;
  const emailValid = isValidEmail(email);
  const checks = passwordChecks(password);
  const score = passwordScore(password);
  const label = passwordLabel(score);
  const passwordValid = Object.values(checks).every(Boolean);

  const submit = async (e) => {
    e.preventDefault();
    setError('');
    setPendingMsg('');

    if (!isValidEmail(email)) {
      setError('Please enter a valid email address.');
      return;
    }
    if (mode === 'signup' && !passwordValid) {
      setError('Password does not meet all requirements below.');
      return;
    }

    setBusy(true);
    try {
      const body = mode === 'login' ? { email, password } : { email, password, role };
      const res = await fetch(`${API_BASE_URL}/auth/${mode === 'login' ? 'login' : 'signup'}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Something went wrong.');

      if (mode === 'signup') {
        setMode('login');
        setPassword('');
        setPendingMsg('Account created. You can log in now.');
        return;
      }

      localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(data));
      navigate(data.role === 'admin' ? '/admin' : '/user', { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="noir-backdrop">
      <div className="auth-card">
        <div className="auth-eyebrow">STE BuildCare</div>
        <div className="auth-title">{mode === 'login' ? 'Welcome back' : 'Create account'}</div>
        <div className="auth-subtitle">
          {mode === 'login' ? 'Sign in to continue' : 'A moment to set up — then you\'re in'}
        </div>

        <div className="auth-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'login'}
            className={`auth-tab ${mode === 'login' ? 'auth-tab--active' : ''}`}
            onClick={() => switchMode('login')}
          >
            LOG IN
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'signup'}
            className={`auth-tab ${mode === 'signup' ? 'auth-tab--active' : ''}`}
            onClick={() => switchMode('signup')}
          >
            SIGN UP
          </button>
        </div>

        <form className="auth-form" onSubmit={submit}>
          <div className="auth-field">
            <label className="auth-label" htmlFor="email">Email address</label>
            <input
              id="email"
              className="auth-input"
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
            />
            {emailTouched && !emailValid && (
              <div className="auth-error">Please enter a valid email address.</div>
            )}
          </div>

          <div className="auth-field">
            <label className="auth-label" htmlFor="password">Password</label>
            <input
              id="password"
              className="auth-input"
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              required
            />
          </div>

          {mode === 'signup' && password.length > 0 && (
            <div className="auth-field">
              <div className="pw-strength">
                {[0, 1, 2, 3, 4].map((i) => (
                  <div
                    key={i}
                    className={`pw-strength-bar ${i < score ? `pw-strength-bar--filled pw-${label.toLowerCase()}` : ''}`}
                  />
                ))}
              </div>
              <div className="pw-strength-label">{label}</div>
              <div className="pw-rules">
                <span className={`pw-rule ${checks.length ? 'pw-rule--met' : ''}`}>8+ chars</span>
                <span className={`pw-rule ${checks.upper ? 'pw-rule--met' : ''}`}>Aa</span>
                <span className={`pw-rule ${checks.lower ? 'pw-rule--met' : ''}`}>aa</span>
                <span className={`pw-rule ${checks.digit ? 'pw-rule--met' : ''}`}>0-9</span>
                <span className={`pw-rule ${checks.special ? 'pw-rule--met' : ''}`}>!@#</span>
              </div>
            </div>
          )}

          {mode === 'signup' && (
            <div className="auth-field">
              <label className="auth-label">Account type</label>
              <div className="auth-role-group">
                <button
                  type="button"
                  className={`auth-role-option ${role === 'user' ? 'auth-role-option--active' : ''}`}
                  onClick={() => setRole('user')}
                >
                  Resident
                </button>
                <button
                  type="button"
                  className={`auth-role-option ${role === 'admin' ? 'auth-role-option--active' : ''}`}
                  onClick={() => setRole('admin')}
                >
                  Admin
                </button>
              </div>
            </div>
          )}

          {pendingMsg && <div className="auth-pending">{pendingMsg}</div>}
          {error && <div className="auth-error">{error}</div>}
          <button className="auth-submit" type="submit" disabled={busy}>
            {busy ? 'Please wait…' : mode === 'login' ? 'Log In' : 'Create Account'}
          </button>
        </form>
      </div>
    </div>
  );
}
