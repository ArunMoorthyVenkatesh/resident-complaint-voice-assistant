import ComplaintsDashboard from './components/ComplaintsDashboard';
import './styles.css';

export default function AdminApp() {
  return (
    <div className="log-view">
      <div className="log-topbar">
        <span className="log-title">STE BuildCare — Complaints Log</span>
      </div>
      <div className="log-content"><ComplaintsDashboard /></div>
    </div>
  );
}
