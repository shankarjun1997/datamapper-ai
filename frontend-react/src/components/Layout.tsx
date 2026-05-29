import { NavLink, Outlet, useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { useAuth } from '../store/auth';

const WORKSPACES = [
  { to: '/discovery', label: 'Discovery', icon: '🗂' },
  { to: '/mapping', label: 'Mapping', icon: '🔗' },
  { to: '/lineage', label: 'Lineage', icon: '🕸' },
  { to: '/migration', label: 'Migration', icon: '🚀' },
  { to: '/governance', label: 'Governance', icon: '🛡' },
];

export default function Layout() {
  const { user, setUser } = useAuth();
  const nav = useNavigate();

  async function logout() {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* ignore */ }
    setUser(null);
    nav('/login');
  }

  return (
    <div className="grid h-full" style={{ gridTemplateColumns: '232px 1fr' }}>
      <aside className="flex flex-col gap-1 border-r border-line bg-[#0c142b] p-4">
        <div className="mb-4 flex items-center gap-2 px-1 text-[15px] font-extrabold">
          <span className="grid h-6 w-6 place-items-center rounded-md bg-gradient-to-br from-cyan to-violet text-[13px] font-extrabold text-[#06101f]">x</span>
          xREF
        </div>
        {WORKSPACES.map((w) => (
          <NavLink
            key={w.to}
            to={w.to}
            className={({ isActive }) =>
              `flex items-center gap-2.5 rounded-lg px-3 py-2.5 text-[13px] font-medium ${
                isActive ? 'bg-panel2 text-white' : 'text-muted hover:text-white'
              }`
            }
          >
            <span>{w.icon}</span>
            {w.label}
          </NavLink>
        ))}
        <div className="mt-auto border-t border-line pt-3 text-[11.5px] text-dim">
          <div className="text-[12.5px] text-muted">{user?.tenant_name || user?.tenant}</div>
          <div>{user?.email}</div>
          <button onClick={logout} className="mt-2 text-dim hover:text-danger">Sign out</button>
        </div>
      </aside>
      <main className="overflow-auto p-7">
        <Outlet />
      </main>
    </div>
  );
}
