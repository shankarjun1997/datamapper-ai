import { useMetadataStats } from '../../lib/queries';

export default function Discovery() {
  const { data, isLoading, error } = useMetadataStats();
  const order = ['system', 'database', 'schema', 'table', 'column', 'relationship'];

  return (
    <div>
      <h1 className="text-xl font-bold">Discovery</h1>
      <p className="mb-5 text-[13px] text-muted">Canonical metadata repository — every discovered asset, versioned.</p>

      {isLoading && <div className="text-muted">Loading catalog…</div>}
      {error && <div className="text-danger">{(error as Error).message}</div>}

      {data && (
        <>
          <div className="mb-5 flex gap-3">
            <div className="rounded-lg border border-line bg-panel px-5 py-4 text-center">
              <div className="text-2xl font-bold text-white">{data.total_objects}</div>
              <div className="text-[11px] text-muted">catalog objects</div>
            </div>
            <div className="rounded-lg border border-line bg-panel px-5 py-4 text-center">
              <div className="text-2xl font-bold text-cyan">{data.total_versions}</div>
              <div className="text-[11px] text-muted">versions tracked</div>
            </div>
          </div>
          <div className="grid grid-cols-3 gap-3">
            {order.map((t) => (
              <div key={t} className="rounded-lg border border-line bg-panel2 px-4 py-3">
                <div className="text-lg font-bold text-white">{data.by_type[t] ?? 0}</div>
                <div className="text-[11px] capitalize text-muted">{t}s</div>
              </div>
            ))}
          </div>
          {data.total_objects === 0 && (
            <p className="mt-5 text-[12.5px] text-muted">
              No metadata yet. Ingest a discovered schema via <span className="font-mono text-cyan">POST /api/metadata/ingest</span> (e.g. from a mapping session).
            </p>
          )}
        </>
      )}
    </div>
  );
}
