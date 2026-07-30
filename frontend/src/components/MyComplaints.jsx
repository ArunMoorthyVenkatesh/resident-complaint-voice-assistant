import { useState, useEffect } from 'react';

const API_BASE_URL = import.meta.env.VITE_API_URL || window.location.origin;

const STATUS_COLORS = {
  Open:         { bg: 'rgba(214,48,49,0.10)',  color: '#d63031', border: 'rgba(214,48,49,0.25)' },
  'In Progress':{ bg: 'rgba(37,99,235,0.10)',  color: '#2563eb', border: 'rgba(37,99,235,0.25)' },
  Resolved:     { bg: 'rgba(45,214,126,0.12)', color: '#2dd67e', border: 'rgba(45,214,126,0.3)' },
  Closed:       { bg: 'rgba(120,130,150,0.12)',color: '#889aaa', border: 'rgba(120,130,150,0.3)' },
};

export default function MyComplaints({ onClose }) {
  const [complaints, setComplaints] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const auth = JSON.parse(localStorage.getItem('buildcare_auth') || 'null');
    fetch(`${API_BASE_URL}/my-complaints`, {
      headers: { Authorization: `Bearer ${auth?.token || ''}` },
    })
      .then((res) => res.json())
      .then((data) => setComplaints(data.complaints || []))
      .catch(() => setError('Failed to load your complaints.'))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="mc-fullscreen">
      <div className="log-topbar">
        <button className="log-back" onClick={onClose}>←</button>
        <span className="log-title">My Complaints</span>
      </div>

      <div className="log-content">
        {loading && <div className="dash-empty">Loading…</div>}
        {error && <div className="dash-empty">{error}</div>}
        {!loading && !error && complaints.length === 0 && (
          <div className="dash-empty">You haven't logged any complaints yet.</div>
        )}

        {!loading && !error && complaints.length > 0 && (
          <div className="dash-table-wrap">
            <table className="dash-table">
              <thead>
                <tr>
                  <th>Reference</th>
                  <th>Date & Time</th>
                  <th>Type</th>
                  <th>Description</th>
                  <th>Location</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {complaints.map((c) => {
                  const sc = STATUS_COLORS[c.status] || STATUS_COLORS.Open;
                  return (
                    <tr key={c.complaint_id} className="dash-row">
                      <td><span className="complaint-id">{c.complaint_id || '—'}</span></td>
                      <td className="dash-date">{c.timestamp || '—'}</td>
                      <td><span className="complaint-type-tag">{c.complaint_type || '—'}</span></td>
                      <td className="dash-desc">{c.description || '—'}</td>
                      <td className="dash-location">{c.location || '—'}</td>
                      <td>
                        <span
                          className="status-badge"
                          style={{ background: sc.bg, color: sc.color, border: `1px solid ${sc.border}` }}
                        >
                          {c.status || 'Open'}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
