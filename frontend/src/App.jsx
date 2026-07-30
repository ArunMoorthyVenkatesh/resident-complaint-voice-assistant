import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Splash from './Splash.jsx';
import LoginApp from './LoginApp.jsx';
import CallApp from './CallApp.jsx';
import AdminApp from './AdminApp.jsx';

function getAuth() {
  return JSON.parse(localStorage.getItem('buildcare_auth') || 'null');
}

function ProtectedRoute({ children, requireRole }) {
  const auth = getAuth();
  if (!auth?.token) return <Navigate to="/" replace />;
  if (requireRole && auth.role !== requireRole) return <Navigate to="/" replace />;
  return children;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Splash />} />
        <Route path="/login" element={<LoginApp />} />
        <Route
          path="/user"
          element={
            <ProtectedRoute>
              <CallApp />
            </ProtectedRoute>
          }
        />
        <Route
          path="/admin"
          element={
            <ProtectedRoute requireRole="admin">
              <AdminApp />
            </ProtectedRoute>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
