import { useState, useEffect } from 'react';

const API_BASE_URL = import.meta.env.VITE_API_URL || window.location.origin;

/**
 * Tracks whether the given tour ("user" | "admin") has been seen -- stored
 * against the account in DynamoDB (via /auth/me + /auth/tour-seen), not
 * localStorage, so it's consistent across devices/browsers for the same login.
 */
export function useTour(tour) {
  const [tourOpen, setTourOpen] = useState(false);

  useEffect(() => {
    const auth = JSON.parse(localStorage.getItem('buildcare_auth') || 'null');
    if (!auth?.token) return;
    fetch(`${API_BASE_URL}/auth/me`, { headers: { Authorization: `Bearer ${auth.token}` } })
      .then((res) => res.json())
      .then((data) => setTourOpen(!data[`seen_tour_${tour}`]))
      .catch(() => {});
  }, [tour]);

  const closeTour = () => {
    setTourOpen(false);
    const auth = JSON.parse(localStorage.getItem('buildcare_auth') || 'null');
    if (!auth?.token) return;
    fetch(`${API_BASE_URL}/auth/tour-seen`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${auth.token}` },
      body: JSON.stringify({ tour }),
    }).catch(() => {});
  };

  return [tourOpen, setTourOpen, closeTour];
}
