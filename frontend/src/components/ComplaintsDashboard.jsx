import { useState, useEffect } from 'react';

const API_BASE_URL = import.meta.env.VITE_API_URL || window.location.origin;
const API_KEY      = import.meta.env.VITE_API_KEY;
const HEADERS      = { 'X-API-Key': API_KEY };

const STATUS_COLORS = {
  Open:        { bg: 'rgba(214,48,49,0.10)',  color: '#d63031', border: 'rgba(214,48,49,0.25)' },
  'In Progress':{ bg: 'rgba(37,99,235,0.10)', color: '#2563eb', border: 'rgba(37,99,235,0.25)' },
  Resolved:    { bg: 'rgba(45,214,126,0.12)',  color: '#2dd67e', border: 'rgba(45,214,126,0.3)' },
  Closed:      { bg: 'rgba(120,130,150,0.12)', color: '#889aaa', border: 'rgba(120,130,150,0.3)' },
};



const ALERT_STYLES = {
  cluster:   { label: 'Possible shared issue', color: '#d63031' },
  recurring: { label: 'Recurring',             color: '#e17055' },
  spike:     { label: 'Category spike',        color: '#e17055' },
};

export default function ComplaintsDashboard() {
  const [complaints, setComplaints] = useState([]);
  const [loading,    setLoading]    = useState(true);
  const [error,      setError]      = useState('');
  const [filter,     setFilter]     = useState('All');
  const [typeFilter, setTypeFilter] = useState('All');
  const [dateFrom,   setDateFrom]   = useState('');
  const [dateTo,     setDateTo]     = useState('');
  const [search,     setSearch]     = useState('');
  const [alerts,     setAlerts]     = useState([]);
  const [filtersOpen,  setFiltersOpen]  = useState(true);
  const [insightsOpen, setInsightsOpen] = useState(true);
  const [updatingId,   setUpdatingId]   = useState(null);
  const [statusError,  setStatusError]  = useState('');

  const normalise = (c) => ({
    'Complaint ID':   c.complaint_id   || c['Complaint ID']   || '',
    'Timestamp':      c.timestamp      || c['Timestamp']      || '',
    'Name':           c.name           || c['Name']           || '',
    'Email':          c.email          || c['Email']          || '',
    'Complaint Type': c.complaint_type || c['Complaint Type'] || '',
    'Description':    c.description    || c['Description']    || '',
    'Location':       c.location       || c['Location']       || '',
    'Status':         c.status         || c['Status']         || 'Open',
    'Transcript':     c.transcript     || c['Transcript']     || '',
  });

  const fetchComplaints = async () => {
    setLoading(true);
    setError('');
    try {
      const res = await fetch(`${API_BASE_URL}/complaints`, { headers: HEADERS });
      const data = await res.json();
      const parseTs = (ts) => new Date((ts || '').replace(' SGT', ''));
      const sorted = (data.complaints || []).map(normalise).sort((a, b) =>
        parseTs(b.Timestamp) - parseTs(a.Timestamp)
      );
      setComplaints(sorted);
    } catch (err) {
      setError(err.message || 'Failed to load complaints.');
    } finally {
      setLoading(false);
    }
  };

  const fetchPatterns = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/complaints/patterns`, { headers: HEADERS });
      const data = await res.json();
      setAlerts(data.alerts || []);
    } catch (_) {
      // Non-critical -- the complaints table still works without pattern alerts.
    }
  };

  const handleStatusChange = async (complaintId, newStatus) => {
    const prev = complaints;
    setStatusError('');
    setUpdatingId(complaintId);
    // Optimistic update -- revert if the request fails.
    setComplaints(cs => cs.map(c => c['Complaint ID'] === complaintId ? { ...c, Status: newStatus } : c));
    try {
      const res = await fetch(`${API_BASE_URL}/complaints/${complaintId}/status`, {
        method: 'PATCH',
        headers: { ...HEADERS, 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) throw new Error(`Failed to update status (${res.status})`);
    } catch (err) {
      setComplaints(prev);
      setStatusError(`Could not update ${complaintId}: ${err.message}`);
    } finally {
      setUpdatingId(null);
    }
  };

  useEffect(() => { fetchComplaints(); fetchPatterns(); }, []);

  const statuses = ['All', 'Open', 'In Progress', 'Resolved', 'Closed'];
  const types = ['All', ...Array.from(new Set(complaints.map(c => c['Complaint Type']).filter(Boolean))).sort()];

  const parseTs = (ts) => new Date((ts || '').replace(' SGT', ''));

  const filtered = complaints.filter(c => {
    const matchStatus = filter === 'All' || c.Status === filter;
    const matchType   = typeFilter === 'All' || c['Complaint Type'] === typeFilter;
    const q = search.toLowerCase();
    const matchSearch = !q
      || c['Complaint ID']?.toLowerCase().includes(q)
      || c.Name?.toLowerCase().includes(q)
      || c.Email?.toLowerCase().includes(q)
      || c['Complaint Type']?.toLowerCase().includes(q)
      || c.Description?.toLowerCase().includes(q)
      || c.Location?.toLowerCase().includes(q);
    const ts = parseTs(c.Timestamp);
    const matchFrom = !dateFrom || ts >= new Date(dateFrom);
    const matchTo   = !dateTo   || ts <= new Date(dateTo + 'T23:59:59');
    return matchStatus && matchType && matchSearch && matchFrom && matchTo;
  });

  const counts = statuses.reduce((acc, s) => {
    acc[s] = s === 'All' ? complaints.length : complaints.filter(c => c.Status === s).length;
    return acc;
  }, {});

  const hasActiveFilters = filter !== 'All' || typeFilter !== 'All' || dateFrom || dateTo || search;
  const clearFilters = () => { setFilter('All'); setTypeFilter('All'); setDateFrom(''); setDateTo(''); setSearch(''); };

  return (
    <div className="dash-wrap">
      {/* Header */}
      <div className="dash-header">
        <div>
          <h2 className="dash-title">Complaints Log</h2>
          <p className="dash-sub">
            {hasActiveFilters
              ? `Showing ${filtered.length} of ${complaints.length} complaints`
              : `${complaints.length} total complaint${complaints.length !== 1 ? 's' : ''} recorded`}
          </p>
        </div>
        <button className="dash-refresh" onClick={() => { fetchComplaints(); fetchPatterns(); }} disabled={loading}>
          {loading ? 'Loading…' : '↻ Refresh'}
        </button>
      </div>

      {/* Filters */}
      <div className="dash-section">
        <button className="dash-section-title" onClick={() => setFiltersOpen(o => !o)}>
          <span className={`dash-chevron ${filtersOpen ? 'open' : ''}`}>▸</span>
          Filters
          {hasActiveFilters && <span className="dash-section-badge">active</span>}
        </button>
        {filtersOpen && (
          <div className="dash-controls">
            <div id="tour-filters" className="dash-filters">
              {statuses.map(s => (
                <button
                  key={s}
                  className={`filter-btn ${filter === s ? 'active' : ''}`}
                  onClick={() => setFilter(s)}
                >
                  {s} <span className="filter-count">{counts[s]}</span>
                </button>
              ))}
            </div>
            <div className="dash-filters-row">
              <select className="dash-select" value={typeFilter} onChange={e => setTypeFilter(e.target.value)}>
                {types.map(t => <option key={t} value={t}>{t === 'All' ? 'All Types' : t}</option>)}
              </select>
              <input
                className="dash-date-input"
                type="date"
                value={dateFrom}
                onChange={e => setDateFrom(e.target.value)}
                title="From date"
              />
              <span className="dash-date-sep">–</span>
              <input
                className="dash-date-input"
                type="date"
                value={dateTo}
                onChange={e => setDateTo(e.target.value)}
                title="To date"
              />
              <input
                id="tour-search"
                className="dash-search"
                type="text"
                placeholder="Search by name, email, type, ID, location…"
                value={search}
                onChange={e => setSearch(e.target.value)}
              />
              {hasActiveFilters && (
                <button className="dash-clear" onClick={clearFilters}>Clear filters</button>
              )}
            </div>
          </div>
        )}
      </div>

      {/* Insights (cross-complaint pattern alerts) */}
      {alerts.length > 0 && (
        <div id="tour-insights" className="dash-section">
          <button className="dash-section-title" onClick={() => setInsightsOpen(o => !o)}>
            <span className={`dash-chevron ${insightsOpen ? 'open' : ''}`}>▸</span>
            Insights
            <span className="dash-section-badge">{alerts.length}</span>
          </button>
          {insightsOpen && (
            <div className="dash-alerts">
              {alerts.map((a, i) => {
                const style = ALERT_STYLES[a.type] || ALERT_STYLES.cluster;
                return (
                  <button
                    key={i}
                    className="dash-alert"
                    style={{ borderLeftColor: style.color }}
                    onClick={() => setSearch(a.location_key || a.complaint_type || '')}
                    title="Click to filter the table to these complaints"
                  >
                    <span className="dash-alert-label" style={{ color: style.color }}>{style.label}</span>
                    <span className="dash-alert-msg">{a.message}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Error */}
      {error && <div className="dash-error">{error}</div>}
      {statusError && <div className="dash-error">{statusError}</div>}

      {/* Table */}
      {!loading && !error && (
        filtered.length === 0 ? (
          <div className="dash-empty">No complaints found.</div>
        ) : (
          <div className="dash-table-wrap">
            <table className="dash-table">
              <thead id="tour-columns">
                <tr>
                  <th>Reference</th>
                  <th>Date & Time</th>
                  <th>Name</th>
                  <th>Email</th>
                  <th>Type</th>
                  <th>Description</th>
                  <th>Location</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((c, i) => {
                  const sc = STATUS_COLORS[c.Status] || STATUS_COLORS.Open;
                  return (
                    <tr key={i} className="dash-row">
                      <td>
                        <span className="complaint-id">{c['Complaint ID'] || '—'}</span>
                      </td>
                      <td className="dash-date">{c.Timestamp || '—'}</td>
                      <td className="dash-name">{c.Name || '—'}</td>
                      <td className="dash-name">{c.Email || '—'}</td>
                      <td>
                        <span className="complaint-type-tag">{c['Complaint Type'] || '—'}</span>
                      </td>
                      <td className="dash-desc">{c.Description || '—'}</td>
                      <td className="dash-location">{c.Location || '—'}</td>
                      <td>
                        <select
                          className="status-badge status-select"
                          style={{ background: sc.bg, color: sc.color, border: `1px solid ${sc.border}` }}
                          value={c.Status || 'Open'}
                          disabled={updatingId === c['Complaint ID']}
                          onChange={e => handleStatusChange(c['Complaint ID'], e.target.value)}
                        >
                          {statuses.filter(s => s !== 'All').map(s => (
                            <option key={s} value={s}>{s}</option>
                          ))}
                        </select>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}

      {loading && <div className="dash-loading">Loading complaints…</div>}
    </div>
  );
}
