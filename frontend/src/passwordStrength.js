const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function isValidEmail(email) {
  return EMAIL_RE.test(email);
}

// Mirrors the backend's policy (auth.py: check_password_strength) so the UI
// can show live feedback before the request round-trip.
export function passwordChecks(password) {
  return {
    length: password.length >= 8,
    upper: /[A-Z]/.test(password),
    lower: /[a-z]/.test(password),
    digit: /[0-9]/.test(password),
    special: /[^A-Za-z0-9]/.test(password),
  };
}

export function passwordScore(password) {
  const checks = passwordChecks(password);
  return Object.values(checks).filter(Boolean).length; // 0-5
}

export function passwordLabel(score) {
  if (score <= 2) return 'Weak';
  if (score <= 4) return 'Medium';
  return 'Strong';
}
