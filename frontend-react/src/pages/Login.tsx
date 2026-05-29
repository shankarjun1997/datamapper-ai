import { useState, FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api';
import { useAuth } from '../store/auth';

export default function Login() {
  const [tenant, setTenant] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const { setUser } = useAuth();
  const nav = useNavigate();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr('');
    setBusy(true);
    try {
      const d: any = await api('/api/auth/login', { method: 'POST', body: { tenant, email, password } });
      setUser({ email: d.email, tenant: d.tenant, tenant_name: d.tenant_name, role: d.role, plan: d.plan });
      nav('/migration');
    } catch (e: any) {
      setErr(e.message || 'Login failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid h-full place-items-center p-6">
      <form onSubmit={submit} className="w-[380px] rounded-2xl border border-line bg-panel p-8 shadow-2xl">
        <div className="mb-1 text-[17px] font-bold">Sign in to your workspace</div>
        <div className="mb-5 text-[12.5px] text-muted">xREF Migration Workspaces</div>
        {[
          { label: 'Workspace', val: tenant, set: setTenant, type: 'text', ph: 'acme-telecom' },
          { label: 'Email', val: email, set: setEmail, type: 'email', ph: 'you@company.com' },
          { label: 'Password', val: password, set: setPassword, type: 'password', ph: '••••••••' },
        ].map((f) => (
          <div key={f.label} className="mb-3">
            <label className="mb-1.5 block text-[11.5px] text-muted">{f.label}</label>
            <input
              type={f.type}
              value={f.val}
              placeholder={f.ph}
              onChange={(e) => f.set(e.target.value)}
              className="w-full rounded-lg border border-line bg-[#0c1330] px-3 py-2.5 font-mono text-[13px] text-white outline-none focus:border-cyan"
            />
          </div>
        ))}
        {err && <div className="mb-3 text-[12px] text-danger">{err}</div>}
        <button
          disabled={busy}
          className="mt-2 w-full rounded-lg bg-cyan py-2.5 text-[13px] font-bold text-[#04131b] disabled:opacity-60"
        >
          {busy ? 'Signing in…' : 'Sign in →'}
        </button>
      </form>
    </div>
  );
}
