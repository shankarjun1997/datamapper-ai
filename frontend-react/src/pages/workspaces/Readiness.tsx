import { useState } from 'react';
import SessionPicker from '../../components/SessionPicker';
import { useReadiness, ReadinessColumn } from '../../lib/queries';

const LEVEL_COLOR: Record<string, string> = {
  ready: 'text-grn', review: 'text-cyan', risk: 'text-amber', blocker: 'text-danger',
};
const LEVEL_BG: Record<string, string> = {
  ready: 'bg-grn', review: 'bg-cyan', risk: 'bg-amber', blocker: 'bg-danger',
};

function ScoreRing({ score }: { score: number }) {
  const level = score >= 90 ? 'ready' : score >= 75 ? 'review' : score >= 60 ? 'risk' : 'blocker';
  return (
    <div className="flex items-center gap-4">
      <div
        className="grid h-24 w-24 place-items-center rounded-full"
        style={{ background: `conic-gradient(currentColor ${score * 3.6}deg, #1b2547 0deg)` }}
      >
        <div className="grid h-[78px] w-[78px] place-items-center rounded-full bg-ink">
          <span className="text-2xl font-bold text-white">{score}</span>
        </div>
      </div>
      <div className={LEVEL_COLOR[level]}>
        <div className="text-2xl font-bold capitalize">{level}</div>
        <div className="text-[12px] text-muted">overall readiness</div>
      </div>
    </div>
  );
}

export default function Readiness() {
  const [sid, setSid] = useState<string | null>(null);
  const { data, isLoading, error } = useReadiness(sid);

  return (
    <div>
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">Migration Readiness</h1>
          <p className="text-[13px] text-muted">Can this migrate — and how safely — before any ETL is written.</p>
        </div>
        <SessionPicker value={sid} onChange={setSid} />
      </div>

      {isLoading && <div className="text-muted">Assessing…</div>}
      {error && <div className="text-danger">{(error as Error).message}</div>}

      {data && (
        <>
          <div className="mb-5 flex flex-wrap items-center gap-8 rounded-xl border border-line bg-panel p-6">
            <ScoreRing score={data.overall_readiness} />
            <div className="flex gap-3">
              {(['ready', 'review', 'risk', 'blocker'] as const).map((k) => (
                <div key={k} className="rounded-lg border border-line bg-panel2 px-4 py-3 text-center">
                  <div className={`text-xl font-bold ${LEVEL_COLOR[k]}`}>{data.counts[k] ?? 0}</div>
                  <div className="text-[11px] capitalize text-muted">{k}</div>
                </div>
              ))}
            </div>
            <div className="text-[12px] text-muted">
              <div><span className="font-mono text-white">{data.source_platform}</span> → <span className="font-mono text-white">{data.target_platform}</span></div>
              <div>{data.assessed_columns} columns assessed</div>
            </div>
          </div>

          {data.blockers.length > 0 && (
            <div className="mb-5 rounded-xl border border-danger/40 bg-danger/10 p-4">
              <div className="mb-1 font-semibold text-danger">{data.blockers.length} blocker(s) must be resolved</div>
              <div className="text-[12px] text-muted">These types have no safe target representation — see the table below.</div>
            </div>
          )}

          <div className="overflow-hidden rounded-xl border border-line">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="bg-panel2 text-left text-[11px] uppercase tracking-wide text-dim">
                  <th className="p-3">Source</th><th className="p-3">Type</th>
                  <th className="p-3">Target</th><th className="p-3">Recommended</th>
                  <th className="p-3">Readiness</th><th className="p-3">Risks</th>
                </tr>
              </thead>
              <tbody>
                {data.columns.map((c: ReadinessColumn, i) => (
                  <tr key={i} className="border-t border-line/60">
                    <td className="p-3 font-mono text-cyan">{c.src_table}.{c.src_field}</td>
                    <td className="p-3 font-mono text-muted">{c.source_type}</td>
                    <td className="p-3 font-mono">{c.tgt_table}.{c.tgt_column}</td>
                    <td className="p-3 font-mono text-muted">{c.recommended_type}</td>
                    <td className="p-3">
                      <div className="flex items-center gap-2">
                        <div className="h-1.5 w-16 overflow-hidden rounded bg-[#1b2547]">
                          <div className={`h-full ${LEVEL_BG[c.level]}`} style={{ width: `${c.readiness}%` }} />
                        </div>
                        <span className={LEVEL_COLOR[c.level]}>{c.readiness}</span>
                      </div>
                    </td>
                    <td className="p-3 text-[11.5px] text-muted">{c.risks.join(' ') || '—'}</td>
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
