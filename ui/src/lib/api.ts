/**
 * API helper — works in both Electron and browser.
 * In Electron, window.nexus.getBackendUrl() gives the harness URL.
 * In dev web, uses Vite proxy (/api) or window.location.
 */

export async function getBackendUrl(): Promise<string> {
  // Electron: via preload
  const w = window as unknown as { nexus?: { getBackendUrl: () => Promise<string> } };
  if (w.nexus?.getBackendUrl) {
    try {
      const url = await w.nexus.getBackendUrl();
      if (url) return url;
    } catch {}
  }
  // Web dev: if we're on :5174, backend is on :8765 — Vite proxies /api, so relative works
  // But for absolute fetches, use current origin or fallback
  if (window.location.port === "5174") return "http://127.0.0.1:8765";
  return window.location.origin;
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const base = await getBackendUrl();
  const url = path.startsWith("/") ? `${base}${path}` : `${base}/${path}`;
  // If base is window.location.origin and we are on :5174 with proxy, relative path is better for CORS
  // Prefer relative when base is current origin
  if (base === window.location.origin) {
    return fetch(path, init);
  }
  return fetch(url, init);
}

export async function chatApi(message: string, sessionId?: string, hint?: string, model?: string): Promise<{ reply: string; session_id: string; success: boolean; mode?: string; error?: string }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  try {
    const res = await apiFetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId, hint, model }),
      signal: ctrl.signal,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`chat ${res.status}: ${text.slice(0, 500)}`);
    }
    return res.json();
  } catch (e: unknown) {
    if ((e as Error).name === "AbortError") throw new Error("chat timeout 90s — el modelo local tarda; reintenta o elige otro modelo");
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export async function healthApi(): Promise<{ status: string; version: string; brain: unknown; providers: Record<string, unknown> }> {
  const res = await apiFetch("/api/health");
  if (!res.ok) throw new Error(`health ${res.status}`);
  return res.json();
}

export type ProviderProbe = { ok: boolean; status: 'online' | 'offline'; models: string[]; registered: string[]; error?: string }
export type ProvidersResponse = {
  providers: Record<string, ProviderProbe>
  models: { id: string; provider: string; label: string; ctx: number; status: 'online' | 'offline' | 'loading' }[]
  router: { vram_budget_gb?: number; fallback_order?: string[]; strategy?: string }
}

export async function providersApi(): Promise<ProvidersResponse> {
  const res = await apiFetch("/api/providers");
  if (!res.ok) throw new Error(`providers ${res.status}`);
  return res.json();
}

export type ProvidersConfigResponse = {
  providers: Record<string, { base_url?: string; fallback_url?: string; api_key?: string; api_key_masked?: string; _has_env_key?: boolean; auth_style?: string; models?: string[]; enabled?: boolean; timeout_s?: number; ctx?: number }>
  router: { strategy?: string; vram_budget_gb?: number; prefer_speed?: boolean; fallback_order?: string[] }
}

export async function getProvidersConfig(): Promise<ProvidersConfigResponse> {
  const res = await apiFetch("/api/providers/config");
  if (!res.ok) throw new Error(`providers/config ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function saveProvidersConfig(payload: { providers: Record<string, unknown>; router?: Record<string, unknown> }): Promise<{ ok: boolean; providers: Record<string, unknown> }> {
  const res = await apiFetch("/api/providers/config", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!res.ok) {
    const t = await res.text();
    throw new Error(t.slice(0, 800));
  }
  return res.json();
}

export async function testProvider(payload: { provider: string; base_url: string; api_key?: string; auth_style?: string; fallback_url?: string }): Promise<{ ok: boolean; status: string; models: string[]; error?: string; latency_ms: number }> {
  const res = await apiFetch("/api/providers/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  if (!res.ok) throw new Error(`test ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function fsList(path: string): Promise<{ path: string; entries: { name: string; path: string; type: 'file'|'dir'; size: number }[] }> {
  const res = await apiFetch(`/api/fs/list?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
export async function fsRead(path: string): Promise<{ path: string; content: string; size: number }> {
  const res = await apiFetch(`/api/fs/read?path=${encodeURIComponent(path)}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
export async function fsWrite(path: string, content: string): Promise<{ ok: boolean; path: string; size: number }> {
  const res = await apiFetch("/api/fs/write", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, content }) });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
export async function execCmd(command: string, cwd = "./workspace", timeout = 15): Promise<{ ok: boolean; exit_code: number; output: string; cwd: string }> {
  const res = await apiFetch("/api/exec", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command, cwd, timeout }) });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export type SessionInfo = { id: string; project?: string; parent_id?: string | null; summary?: string; compact_count?: number; total_tokens?: number; created_at?: string; last_active?: string }

export async function sessionsApi(): Promise<{ sessions: SessionInfo[] }> {
  const res = await apiFetch("/api/sessions");
  if (!res.ok) throw new Error(`sessions ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function createSessionApi(project = "default"): Promise<SessionInfo> {
  const res = await apiFetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project }) });
  if (!res.ok) throw new Error(`sessions POST ${res.status}: ${await res.text()}`);
  return res.json();
}
