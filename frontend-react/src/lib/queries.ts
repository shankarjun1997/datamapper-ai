import { useQuery } from '@tanstack/react-query';
import { api } from './api';

export interface SessionSummary {
  id: string;
  status: string;
  stage: string;
  created_at: string;
  filename?: string;
}

export function useSessions() {
  return useQuery({
    queryKey: ['sessions'],
    queryFn: () => api<SessionSummary[]>('/api/sessions'),
  });
}

export interface ReadinessReport {
  source_platform: string;
  target_platform: string;
  overall_readiness: number;
  overall_level: string;
  assessed_columns: number;
  counts: Record<string, number>;
  blockers: any[];
  risks: { column: string; risk: string }[];
  columns: ReadinessColumn[];
}
export interface ReadinessColumn {
  src_table: string; src_field: string; tgt_table: string; tgt_column: string;
  source_type: string; target_type: string; recommended_type: string;
  readiness: number; level: string; risks: string[];
}

export function useReadiness(sid: string | null, src?: string, tgt?: string) {
  const qs = new URLSearchParams();
  if (src) qs.set('source_platform', src);
  if (tgt) qs.set('target_platform', tgt);
  return useQuery({
    enabled: !!sid,
    queryKey: ['readiness', sid, src, tgt],
    queryFn: () => api<ReadinessReport>(`/api/sessions/${sid}/readiness?${qs.toString()}`),
  });
}

export interface LineageGraph {
  nodes: { id: string; side: 'source' | 'target'; table: string; column: string }[];
  edges: { from: string; to: string; mapping_type: string; confidence: number | null; status: string; transform: string }[];
  table_lineage: { from_table: string; to_table: string }[];
  stats: Record<string, number>;
}

export function useLineage(sid: string | null) {
  return useQuery({
    enabled: !!sid,
    queryKey: ['lineage', sid],
    queryFn: () => api<LineageGraph>(`/api/sessions/${sid}/lineage`),
  });
}

export interface MetadataStats {
  total_objects: number;
  by_type: Record<string, number>;
  total_versions: number;
}

export function useMetadataStats() {
  return useQuery({
    queryKey: ['metadata-stats'],
    queryFn: () => api<MetadataStats>('/api/metadata/stats'),
  });
}
