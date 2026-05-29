import { useEffect } from 'react';
import { useSessions } from '../lib/queries';

export default function SessionPicker({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (id: string) => void;
}) {
  const { data, isLoading } = useSessions();

  useEffect(() => {
    if (!value && data && data.length) onChange(data[0].id);
  }, [data, value, onChange]);

  return (
    <select
      value={value || ''}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-line bg-panel px-3 py-2 text-[13px] text-white outline-none focus:border-cyan"
    >
      {isLoading && <option>Loading sessions…</option>}
      {!isLoading && (!data || data.length === 0) && <option value="">No sessions</option>}
      {data?.map((s) => (
        <option key={s.id} value={s.id}>
          {s.filename || s.id.slice(0, 8)} · {s.status}
        </option>
      ))}
    </select>
  );
}
