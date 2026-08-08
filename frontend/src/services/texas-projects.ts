// Texas Projects service — fetches the ERCOT interconnection-queue + TCEQ
// air-permit pin layer from `backend/`'s standalone FastAPI service (see
// backend/README.md and docs/frontend-map-integration.md). Powers the
// TexasProjectsLayer on the Energy Atlas map.
//
// Unlike the rest of this app's data layers, this one is NOT routed through
// the sebuf-generated RPC clients / gateway — `backend/` is a separate
// Python service (Supabase-backed) with its own plain REST + CORS surface,
// so this is a direct `fetch()` of its `/map` endpoint. In-memory cache,
// 5-minute TTL (this data changes on the order of days, not seconds — no
// need for the 60s live-position TTL used by AIS/tanker layers).

// Non-browser fallback (node:test etc. — see guard below), where there's no
// Vite dev-server proxy to go through.
const DEFAULT_BASE_URL = 'http://localhost:8000';
// Browser default: same-origin, proxied by Vite (see '/api/texas-backend' in
// vite.config.ts) rather than an absolute http://localhost:8000. In
// Codespaces/tunneled dev the browser runs on a different machine than the
// container, so a hardcoded localhost:8000 resolves to the client's own
// machine and fails; the forwarded Vite origin doesn't have that problem.
const DEFAULT_BROWSER_BASE_URL = '/api/texas-backend';

// Guarded like VITE_ENABLE_IRAN_ATTACKS in map-layer-definitions.ts:
// import.meta.env is undefined under node:test, where this module can be
// imported directly outside a Vite build.
function getBaseUrl(): string {
  if (typeof window === 'undefined') return DEFAULT_BASE_URL;
  return import.meta.env.VITE_TEXAS_BACKEND_URL || DEFAULT_BROWSER_BASE_URL;
}

const CACHE_TTL_MS = 5 * 60_000;

export interface TexasProjectFeatureProperties {
  layer: 'tceq' | 'ercot';
  stage: 'queued' | 'permitting' | 'permit_only';
  id: string;
  name: string;
  county: string;
  state: string;
  precision: 'exact' | 'county';
  capacity_mw: number | null;
  fuel: string | null;
  technology: string | null;
  on_thesis: boolean | null;
  resolution_status: string | null;
}

export interface TexasProjectsResponse {
  type: 'FeatureCollection';
  features: Array<{
    type: 'Feature';
    geometry: { type: 'Point'; coordinates: [number, number] };
    properties: TexasProjectFeatureProperties;
  }>;
  meta?: {
    counts?: Record<string, number>;
    by_stage?: Record<string, number>;
  };
}

export interface TexasProjectsQuery {
  source?: 'all' | 'tceq' | 'ercot';
  stage?: 'queued' | 'permitting' | 'permit_only';
  resolvedOnly?: boolean;
  onThesis?: boolean;
  county?: string;
  minMw?: number;
  limit?: number;
}

interface CacheSlot {
  data: TexasProjectsResponse;
  fetchedAt: number;
}

let cache: CacheSlot | null = null;
let cacheKey = '';

function buildUrl(query: TexasProjectsQuery): string {
  const params = new URLSearchParams();
  if (query.source) params.set('source', query.source);
  if (query.stage) params.set('stage', query.stage);
  if (query.resolvedOnly) params.set('resolved_only', 'true');
  if (query.onThesis !== undefined) params.set('on_thesis', String(query.onThesis));
  if (query.county) params.set('county', query.county);
  if (query.minMw !== undefined) params.set('min_mw', String(query.minMw));
  if (query.limit !== undefined) params.set('limit', String(query.limit));
  const qs = params.toString();
  return `${getBaseUrl()}/map${qs ? `?${qs}` : ''}`;
}

const emptyFallback: TexasProjectsResponse = { type: 'FeatureCollection', features: [] };

/**
 * Fetches Texas ERCOT/TCEQ project pins. Returns cached data (5 min TTL,
 * keyed on the query) on repeat calls; on fetch failure, falls back to the
 * last successful response for the same query (or an empty collection if
 * there isn't one) so the layer degrades gracefully instead of blanking.
 */
export async function fetchTexasProjects(
  query: TexasProjectsQuery = {},
  opts: { signal?: AbortSignal } = {},
): Promise<TexasProjectsResponse> {
  const url = buildUrl(query);
  if (cache && cacheKey === url && Date.now() - cache.fetchedAt < CACHE_TTL_MS) {
    return cache.data;
  }
  try {
    const res = await fetch(url, { signal: opts.signal });
    if (!res.ok) throw new Error(`Texas projects backend returned ${res.status}`);
    const data = (await res.json()) as TexasProjectsResponse;
    cache = { data, fetchedAt: Date.now() };
    cacheKey = url;
    return data;
  } catch (error) {
    if (opts.signal?.aborted) throw error;
    console.warn('[texas-projects] fetch failed, using last-known data:', error);
    return cacheKey === url && cache ? cache.data : emptyFallback;
  }
}
