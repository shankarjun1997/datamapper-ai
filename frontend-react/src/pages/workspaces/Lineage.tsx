import { useState } from 'react';
import SessionPicker from '../../components/SessionPicker';
import { useLineage } from '../../lib/queries';

export default function Lineage() {
  const [sid, setSid] = useState<string | null>(null);
  const { data, isLoading, error } = useLineage(sid);

  const sources = data?.nodes.filter((n) => n.side === 'source') ?? [];
  const targets = data?.nodes.filter((n) => n.side === 'target') ?? [];

  return (
    <div>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">Lineage</h1>
          <p className="text-[13px] text-muted">End-to-end source → target column lineage.</p>
        </div>
        <SessionPicker value={sid} onChange={setSid} />
      </div>

      {isLoading && <div className="text-muted">Building lineage…</div>}
      {error && <div className="text-danger">{(error as Error).message}</div>}

      {data && (
        <>
          <div className="mb-5 flex gap-3">
            {Object.entries(data.stats).map(([k, v]) => (
              <div key={k} className="rounded-lg border border-line bg-panel px-4 py-3 text-center">
                <div className="text-xl font-bold text-white">{v}</div>
                <div className="text-[11px] text-muted">{k.replace(/_/g, ' ')}</div>
              </div>
            ))}
          </div>

          <div className="mb-5 rounded-xl border border-line bg-panel p-5">
            <div className="mb-3 text-[11px] uppercase tracking-wide text-dim">Table lineage</div>
            <div className="flex flex-wrap gap-2">
              {data.table_lineage.map((t, i) => (
                <div key={i} className="flex items-center gap-2 rounded-lg border border-line bg-panel2 px-3 py-1.5 text-[12px]">
                  <span className="font-mono text-cyan">{t.from_table}</span>
                  <span className="text-dim">→</span>
                  <span className="font-mono text-grn">{t.to_table}</span>
                </div>
              ))}
              {data.table_lineage.length === 0 && <span className="text-[12px] text-muted">No mappings yet.</span>}
            </div>
          </div>

          <div className="grid gap-4" style={{ gridTemplateColumns: '1fr 40px 1fr' }}>
            <Column title={`Sources (${sources.length})`} color="text-cyan" items={sources} />
            <div className="grid place-items-center text-2xl text-dim">→</div>
            <Column title={`Targets (${targets.length})`} color="text-grn" items={targets} />
          </div>

          <div className="mt-5 overflow-hidden rounded-xl border border-line">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="bg-panel2 text-left text-[11px] uppercase tracking-wide text-dim">
                  <th className="p-3">Source column</th><th className="p-3">Target column</th>
                  <th className="p-3">Type</th><th className="p-3">Confidence</th><th className="p-3">Transform</th>
                </tr>
              </thead>
              <tbody>
                {data.edges.map((e, i) => (
                  <tr key={i} className="border-t border-line/60">
                    <td className="p-3 font-mono text-cyan">{e.from.replace('src:', '')}</td>
                    <td className="p-3 font-mono text-grn">{e.to.replace('tgt:', '')}</td>
                    <td className="p-3 text-muted">{e.mapping_type || '—'}</td>
                    <td className="p-3 text-muted">{e.confidence != null ? `${Math.round(e.confidence * 100)}%` : '—'}</td>
                    <td className="p-3 text-[11.5px] text-muted">{e.transform || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function Column({ title, color, items }: { title: string; color: string; items: { id: string; table: string; column: string }[] }) {
  return (
    <div className="rounded-xl border border-line bg-panel p-4">
      <div className={`mb-3 text-[11px] uppercase tracking-wide ${color}`}>{title}</div>
      <div className="grid gap-1.5">
        {items.map((n) => (
          <div key={n.id} className="rounded-md border border-line bg-panel2 px-3 py-1.5 font-mono text-[12px]">
            <span className="text-dim">{n.table}.</span>{n.column}
          </div>
        ))}
        {items.length === 0 && <span className="text-[12px] text-muted">None</span>}
      </div>
    </div>
  );
}
