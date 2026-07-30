import { useNavigate } from 'react-router-dom';
import ComplaintsDashboard from './components/ComplaintsDashboard';
import Walkthrough from './components/Walkthrough.jsx';
import { useTour } from './useTour.js';
import './styles.css';

const getAuth = () => JSON.parse(localStorage.getItem('buildcare_auth') || 'null');

const ADMIN_TOUR_STEPS = [
  { selector: '#tour-filters', title: 'Filter by status', body: 'Quickly filter complaints by Open, In Progress, Resolved, or Closed.' },
  { selector: '#tour-search', title: 'Search complaints', body: 'Search by resident name, email, complaint type, reference ID, or location.' },
  {
    selector: '#tour-insights',
    title: 'Insights',
    body: 'Rule-based alerts that look across all complaints together: a Cluster flags 3+ same-type complaints near the same spot within 14 days (possibly one shared cause), Recurring flags a spot that keeps coming back over up to 90 days (a past fix may not have held), and a Spike flags a category running 2x+ above its normal monthly rate. Click an alert to filter the table to just those complaints.',
  },
  {
    selector: '#tour-columns',
    title: 'Complaint columns',
    body: 'Reference is the unique complaint ID. Name/Email identify the resident who filed it. Type is the complaint category (used for clustering above). Description and Location are free text from the call. Status is editable per row — change it directly from the dropdown.',
  },
  { selector: '.log-account', title: 'Your account', body: 'This shows the email you\'re logged in with as an admin.' },
  { selector: '.log-logout', title: 'Log out', body: 'Tap here whenever you want to sign out of your admin account.' },
];

export default function AdminApp() {
  const navigate = useNavigate();
  const logout = () => {
    localStorage.removeItem('buildcare_auth');
    navigate('/', { replace: true });
  };

  const [tourOpen, setTourOpen, closeTour] = useTour('admin');

  return (
    <div className="log-view">
      <Walkthrough steps={ADMIN_TOUR_STEPS} open={tourOpen} onClose={closeTour} />
      <div className="log-topbar">
        <span className="log-title">STE BuildCare — Complaints Log</span>
        <div className="log-account-group">
          <button className="log-help" onClick={() => setTourOpen(true)} aria-label="Help">?</button>
          <span className="log-account">{getAuth()?.email}</span>
          <button className="log-logout" onClick={logout}>Log out</button>
        </div>
      </div>
      <div className="log-content"><ComplaintsDashboard /></div>
    </div>
  );
}
