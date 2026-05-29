// Thin API client. Relies on the backend's httpOnly cookie auth (credentials
// included) + CSRF double-submit on unsafe methods. In dev, Vite proxies /api
// to the FastAPI backend so cookies are same-origin.
const BASE = (import.meta as any).env?.VITE_API_URL || '';

function getCookie(name: string): string {
  const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return m ? decodeURIComponent(m[1]) : '';
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T = any>(
  path: string,
  opts: { method?: string; body?: unknown } = {},
): Promise<T> {
  const method = (opts.method || 'GET').toUpperCase();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    const csrf = getCookie('xref_csrf');
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }
  const res = await fetch(BASE + path, {
    method,
    headers,
    credentials: 'include',
    body: opts.body != null ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = j.detail || detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
