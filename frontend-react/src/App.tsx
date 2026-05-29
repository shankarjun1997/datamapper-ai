import { useEffect } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { api } from './lib/api';
import { useAuth } from './store/auth';
import Layout from './components/Layout';
import Login from './pages/Login';
import Discovery from './pages/workspaces/Discovery';
import Mapping from './pages/workspaces/Mapping';
import Lineage from './pages/workspaces/Lineage';
import Readiness from './pages/workspaces/Readiness';
import Governance from './pages/workspaces/Governance';

export default function App() {
  const { user, ready, setUser, setReady } = useAuth();

  useEffect(() => {
    api('/api/auth/me')
      .then((u: any) => setUser({ email: u.email, tenant: u.tenant, tenant_name: u.tenant_name, role: u.role, plan: u.plan }))
      .catch(() => setUser(null))
      .finally(() => setReady(true));
  }, [setUser, setReady]);

  if (!ready) {
    return <div className="grid h-full place-items-center text-muted">Loading…</div>;
  }

  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/" replace /> : <Login />} />
      {!user ? (
        <Route path="*" element={<Navigate to="/login" replace />} />
      ) : (
        <Route element={<Layout />}>
          <Route index element={<Navigate to="/migration" replace />} />
          <Route path="/discovery" element={<Discovery />} />
          <Route path="/mapping" element={<Mapping />} />
          <Route path="/lineage" element={<Lineage />} />
          <Route path="/migration" element={<Readiness />} />
          <Route path="/governance" element={<Governance />} />
          <Route path="*" element={<Navigate to="/migration" replace />} />
        </Route>
      )}
    </Routes>
  );
}
