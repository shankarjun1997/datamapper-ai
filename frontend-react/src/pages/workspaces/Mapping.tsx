export default function Mapping() {
  return (
    <div>
      <h1 className="text-xl font-bold">Mapping</h1>
      <p className="mb-5 text-[13px] text-muted">Confidence-based source → target column mapping with approval gates.</p>
      <div className="rounded-xl border border-line bg-panel p-8 text-center text-muted">
        <div className="mb-2 text-3xl">🔗</div>
        <div className="text-[13px]">The mapping grid lives in the classic app today and is being migrated into this workspace.</div>
        <div className="mt-1 text-[12px] text-dim">Backed by <span className="font-mono">/api/sessions/&#123;id&#125;/mappings</span>.</div>
      </div>
    </div>
  );
}
