import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import './styles.css';

export default function Splash() {
  const navigate = useNavigate();

  useEffect(() => {
    const timer = setTimeout(() => navigate('/login', { replace: true }), 1800);
    return () => clearTimeout(timer);
  }, [navigate]);

  return (
    <div className="noir-backdrop splash-screen">
      <div className="splash-mark">
        <span className="splash-letter">STE BuildCare</span>
      </div>
      <div className="splash-rule" />
      <div className="splash-tagline">Building Supervisor Assistant</div>
    </div>
  );
}
