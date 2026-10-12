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
  // Web (dev :5174 con proxy de Vite, o servido por el backend): mismo origen.
  // El proxy/target se configura con NEXUS_API_URL (vite.config.ts), sin hardcodear.
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

export async function chatApi(
  message: string,
  sessionId?: string,
  hint?: string,
  model?: string,
  attachments?: { id: string; url?: string; name?: string; mime?: string; is_image?: boolean }[],
): Promise<{ reply: string; session_id: string; success: boolean; mode?: string; error?: string; images?: string[] }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 90000);
  try {
    const res = await apiFetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: sessionId, hint, model, attachments }),
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

// Streaming token-a-token (pi): consume /api/chat/stream (SSE) y llama onDelta por chunk.
export async function chatStreamApi(
  message: string,
  opts: { sessionId?: string; model?: string; onDelta?: (t: string) => void } = {},
): Promise<{ reply: string; success: boolean }> {
  const base = await getBackendUrl();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 180000); // cap 3 min: evita UI colgada
  try {
    // POST con body: no expone el mensaje en la URL (logs/límite) y envía session_id.
    const res = await fetch(`${base}/api/chat/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ message, session_id: opts.sessionId, model: opts.model }),
      signal: ctrl.signal,
    });
    if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let reply = "";
    const handleEvent = (raw: string) => {
      const dataLines = raw
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.replace(/^data:\s*/, "").replace(/\r$/, ""));
      if (!dataLines.length) return;
      const line = dataLines.join("\n").trim();
      if (!line || line === "[DONE]") return;
      try {
        const obj = JSON.parse(line);
        if (obj.delta && opts.onDelta) opts.onDelta(String(obj.delta));
        if (obj.reply) reply = String(obj.reply);
      } catch {
        /* chunk no-JSON: ignorar */
      }
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      // Split tolerante a CRLF (SSE puede usar \r\n\r\n).
      const parts = buf.split(/\r?\n\r?\n/);
      if (parts.length > 1) {
        buf = parts.pop() as string;
        for (const ev of parts) handleEvent(ev);
      }
    }
    if (buf.trim()) handleEvent(buf); // remanente sin \n\n final (último chunk)
    return { reply, success: true };
  } catch (e) {
    if ((e as Error).name === "AbortError") throw new Error("stream timeout 180s — el modelo local tarda; reintenta");
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

export async function providersApi(refresh = false): Promise<ProvidersResponse> {
  const res = await apiFetch(`/api/providers${refresh ? "?refresh=1" : ""}`);
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

export async function sessionsApi(limit = 1000): Promise<{ sessions: SessionInfo[] }> {
  const res = await apiFetch(`/api/sessions?limit=${limit}`);
  if (!res.ok) throw new Error(`sessions ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function createSessionApi(project = "default"): Promise<SessionInfo> {
  const res = await apiFetch("/api/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project }) });
  if (!res.ok) throw new Error(`sessions POST ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function deleteSessionApi(sessionId: string): Promise<{ ok: boolean; deleted: boolean; id: string }> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  if (!res.ok && res.status !== 404) throw new Error(`sessions DELETE ${res.status}: ${await res.text()}`);
  return { ok: res.ok, deleted: res.ok, id: sessionId };
}

export type SessionMessage = { role: string; content: string; tool_name?: string; created_at?: string; model?: string; tokens?: number }

export async function sessionMessagesApi(sessionId: string, limit = 200): Promise<SessionMessage[]> {
  const res = await apiFetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`);
  if (!res.ok) throw new Error(`messages ${res.status}`);
  const j = await res.json();
  return (j.messages ?? []) as SessionMessage[];
}

/** FTS5 global (todas las sesiones). El path de la sesión solo fija la ruta; la búsqueda es global. */
export async function searchApi(query: string): Promise<{ role: string; content: string }[]> {
  const res = await apiFetch(`/api/sessions/global/search?q=${encodeURIComponent(query)}`);
  if (!res.ok) throw new Error(`search ${res.status}`);
  const j = await res.json();
  return (j.results ?? []) as { role: string; content: string }[];
}

/* -- DECK: adjuntos, voz, keys, permisos ----------------------- */

export type UploadResult = { id: string; url: string; name: string; mime: string; is_image: boolean; size: number }

export async function uploadApi(file: File): Promise<UploadResult> {
  const fd = new FormData()
  fd.append("file", file, file.name)
  const res = await apiFetch("/api/upload", { method: "POST", body: fd })
  if (!res.ok) throw new Error(`upload ${res.status}: ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

export type VoiceStatus = { tts_available: boolean; stt_available: boolean; stt_model?: string; tts_model?: string }

export async function voiceStatusApi(): Promise<VoiceStatus> {
  const res = await apiFetch("/api/voice/status")
  if (!res.ok) throw new Error(`voice ${res.status}`)
  return res.json()
}

export async function voiceSttApi(audio: Blob, language = "es"): Promise<{ text: string; duration?: number }> {
  const res = await apiFetch(`/api/voice/stt?language=${encodeURIComponent(language)}`, {
    method: "POST",
    headers: { "Content-Type": audio.type || "audio/webm" },
    body: audio,
  })
  if (!res.ok) throw new Error(`stt ${res.status}: ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

export type KeyEntry = { env: string; label: string; providers: string[]; configured: boolean; source: string; masked: string }

export async function keysApi(): Promise<{ keys: KeyEntry[]; env_file: string }> {
  const res = await apiFetch("/api/keys")
  if (!res.ok) throw new Error(`keys ${res.status}`)
  return res.json()
}

export async function keyPutApi(env: string, value: string): Promise<{ ok: boolean; configured: boolean; masked: string }> {
  const res = await apiFetch("/api/keys", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ env, value }),
  })
  if (!res.ok) throw new Error((await res.text()).slice(0, 400))
  return res.json()
}

export async function permissionApi(): Promise<{ permission: string; options: string[] }> {
  const res = await apiFetch("/api/permission")
  if (!res.ok) throw new Error(`permission ${res.status}`)
  return res.json()
}

export async function permissionPutApi(permission: string): Promise<{ ok: boolean; permission: string }> {
  const res = await apiFetch("/api/permission", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ permission }),
  })
  if (!res.ok) throw new Error((await res.text()).slice(0, 400))
  return res.json()
}

/* -- Ayudantes (LLM independiente por asistente) --------------- */

export type Agent = {
  id: string
  name: string
  model: string
  provider: string
  system_prompt: string
  description: string
  emoji: string
  color: string
  gema: string
  workspace?: string
  toolsets: string[]
  skills: string[]
  mcp: string[]
  created_at?: string
  updated_at?: string
  builtin?: boolean
}

export type Gema = {
  name: string
  description: string
  system_prompt: string
  tools: string[] | null
  temperature: number
  max_turns: number
  model_hint: string
  planning: boolean
}

export async function gemasApi(): Promise<{ gemas: Gema[] }> {
  const res = await apiFetch("/api/gemas")
  if (!res.ok) throw new Error(`gemas ${res.status}`)
  return res.json()
}

export type AssignResult = { model: string; provider: string; reason: string; catalog_size: number }

export async function assignApi(params: { gema?: string; agent?: string; model?: string }): Promise<{ assign: AssignResult }> {
  const q = new URLSearchParams()
  if (params.gema) q.set("gema", params.gema)
  if (params.agent) q.set("agent", params.agent)
  if (params.model) q.set("model", params.model)
  const res = await apiFetch(`/api/models/assign?${q.toString()}`)
  if (!res.ok) throw new Error(`assign ${res.status}`)
  return res.json()
}

export async function agentsApi(): Promise<{ agents: Agent[] }> {
  const res = await apiFetch("/api/agents")
  if (!res.ok) throw new Error(`agents ${res.status}: ${(await res.text()).slice(0, 300)}`)
  return res.json()
}

export async function agentCreateApi(payload: Partial<Agent> & { name: string }): Promise<{ ok: boolean; agent: Agent }> {
  const res = await apiFetch("/api/agents", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!res.ok) throw new Error((await res.text()).slice(0, 400))
  return res.json()
}

export async function agentUpdateApi(id: string, patch: Partial<Agent>): Promise<{ ok: boolean; agent: Agent }> {
  const res = await apiFetch(`/api/agents/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  })
  if (!res.ok) throw new Error((await res.text()).slice(0, 400))
  return res.json()
}

export async function agentDeleteApi(id: string): Promise<{ ok: boolean }> {
  const res = await apiFetch(`/api/agents/${encodeURIComponent(id)}`, { method: "DELETE" })
  if (!res.ok) throw new Error((await res.text()).slice(0, 400))
  return res.json()
}

export type Checkpoint = { id: string; path: string; ts: string }

export async function checkpointsApi(sessionId: string): Promise<{ checkpoints: Checkpoint[] }> {
  const res = await apiFetch(`/api/checkpoints?session_id=${encodeURIComponent(sessionId)}`)
  if (!res.ok) throw new Error(`checkpoints ${res.status}`)
  return res.json()
}

export async function checkpointDiffApi(id: string, sessionId: string): Promise<{ ok: boolean; path?: string; diff?: string; error?: string }> {
  const res = await apiFetch(`/api/checkpoints/${encodeURIComponent(id)}/diff?session_id=${encodeURIComponent(sessionId)}`)
  if (!res.ok) throw new Error(`checkpoint diff ${res.status}`)
  return res.json()
}

export async function checkpointRestoreApi(sessionId: string, id: string): Promise<{ ok: boolean; restored?: string; error?: string }> {
  const res = await apiFetch(`/api/checkpoints/restore`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, checkpoint_id: id }),
  })
  if (!res.ok) throw new Error((await res.text()).slice(0, 300))
  return res.json()
}
