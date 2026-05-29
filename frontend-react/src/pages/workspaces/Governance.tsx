export default function Governance() {
  return (
    <div>
      <h1 className="text-xl font-bold">Governance</h1>
      <p className="mb-5 text-[13px] text-muted">Approvals, audit log, and change history.</p>
      <div className="rounded-xl border border-line bg-panel p-8 text-center text-muted">
        <div className="mb-2 text-3xl">🛡</div>
        <div className="text-[13px]">Audit trail &amp; approval workflows — backed by <span className="font-mono">/api/admin/audit</span> and mapping versions.</div>
        <div className="mt-1 text-[12px] text-dim">Surfacing here next.</div>
      </div>
    </div>
  );
}
