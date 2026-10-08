// Thin client for the existing backend. Token lives in sessionStorage (per-origin), sent as Bearer.
const KEY = 'aaf-token'
export const token = { get: () => sessionStorage.getItem(KEY), set: (t: string) => sessionStorage.setItem(KEY, t), clear: () => sessionStorage.removeItem(KEY) }

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { ...(init.headers as Record<string, string>) }
  const t = token.get(); if (t) headers.Authorization = `Bearer ${t}`
  if (init.body && typeof init.body === 'string') headers['Content-Type'] = 'application/json'
  const r = await fetch(path, { ...init, headers })
  if (r.status === 401) { token.clear(); window.dispatchEvent(new Event('aaf-signed-out')) }
  const data = await r.json().catch(() => null)
  if (!r.ok) throw new ApiError(r.status, (data && (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))) || r.statusText)
  return data as T
}

/** Authorized binary fetch (images/files can't carry a Bearer header via <img src>). */
export async function blob(path: string): Promise<Blob> {
  const t = token.get()
  const r = await fetch(path, { headers: t ? { Authorization: `Bearer ${t}` } : {} })
  if (!r.ok) throw new ApiError(r.status, r.statusText)
  return r.blob()
}

const post = <T = { ok: boolean }>(path: string, body: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(body) })

export type RoomState = 'active' | 'archived' | 'deleted' | 'frozen'
export type Room = {
  id: number; name: string; state: RoomState; unread: number; pinned?: number; folder?: string; notify?: number
  auto_approve: { on: boolean; until: string | null }
}
export type Member = { id: string; label: string; online?: boolean; active?: number; last_read?: number }
export type Person = { id: string; label: string }
export type Delivery = { recipient: string; status: string; attempts: number; detail: string | null }
export type Message = {
  id: number; room: number; author: string; body: string; created: number
  notification_kind?: 'chat' | 'task'; reply_to?: number | null; mentions?: string[]
  file: { name: string; size: number; url: string } | null
  image: { url: string; mime?: string; width?: number; height?: number } | null
  delivery?: Delivery[]; likes?: Person[]; read_by?: (Person & { created?: number })[]
}
export type Agent = { id: string; label: string; online: boolean; acp_agent?: string | null; model?: string | null }
export type Config = { owner: string; chat_file_bytes: number | null; upload_bytes: number | null; version: string | null }
export type Approval = { on: boolean; until: string | null }
export type SearchHit = { id: number; author: string; created: number; snippet: string }

export type Outgoing = {
  body: string; reply_to?: number | null; notification_kind?: 'chat' | 'task'
  image?: string | null                                   // base64, PNG/JPEG/WEBP
  file?: { name: string; content: string } | null         // base64
  client_id: string                                       // same id on retry → server dedupes
}

export const login = (username: string, password: string) => post<{ token: string }>('/login', { username, password })
export const getConfig = () => api<Config>('/api/config')
export const listRooms = () => api<Room[]>('/api/rooms')
export const listAgents = () => api<Agent[]>('/api/agents')
export const roomMessages = (room: number, after = 0) => api<{ messages: Message[]; members: Member[]; has_more: boolean }>(`/api/rooms/${room}/messages?after=${after}`)
export const sendMessage = (room: number, m: Outgoing) => post<{ id: number }>(`/api/rooms/${room}/messages`, m)
export const markRead = (room: number, through: number) => post(`/api/rooms/${room}/read`, { through })
export const confirmRead = (room: number, messages: number[]) => post(`/api/rooms/${room}/confirm-read`, { messages })
export const setLike = (room: number, message: number, liked: boolean) => post(`/api/rooms/${room}/messages/${message}/like`, { liked })
export const createRoom = (name: string, members: string[]) => post<{ id: number }>('/api/rooms', { name, members })
export const inviteMember = (room: number, agent: string, reason: string) => post<{ ok: boolean; added: boolean }>(`/api/rooms/${room}/members`, { agent, reason })
export const renameRoom = (room: number, name: string) => post(`/api/rooms/${room}/manage`, { name })
export const roomAction = (room: number, action: 'archive' | 'delete' | 'restore') => post(`/api/rooms/${room}/manage`, { action })
export const pinRoom = (room: number, pinned: boolean) => post(`/api/rooms/${room}/manage`, { pinned })
export const getApproval = (room: number) => api<Approval>(`/api/rooms/${room}/approval`)
export const setApproval = (room: number, on: boolean, until: string | null) => post(`/api/rooms/${room}/approval`, { on, until })
export const searchRoom = (room: number, q: string, mentioned = false) =>
  api<{ results: SearchHit[]; has_more: boolean }>(`/api/rooms/${room}/search?${new URLSearchParams({ q, mentioned: String(mentioned) })}`)

/** Read a File as base64 (no data: prefix). */
export function fileToBase64(f: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ''))
    r.onerror = () => reject(r.error || new Error('read failed'))
    r.readAsDataURL(f)
  })
}

// ---- Agents ---------------------------------------------------------------------------------
export type CatalogAgent = {
  id: string; label: string; installed: boolean; cmd: string[] | null; npm: string | null; needs: string | null
  custom: boolean; logged_in: boolean | null; login_detail: string; install_cmd: string[] | null
}
export type CatalogRole = { id: string; agent: string | null; rank: string | null }
export type SkillRef = { id: string; path: string; description: string }
export type Catalog = { agents: CatalogAgent[]; skills: SkillRef[]; roles: CatalogRole[] }
export type InstallJob = { id: string; agent: string; state: 'running' | 'done' | 'failed'; exit?: number | null; output?: string; offset?: number }
export type Session = {
  room: number | null; room_name?: string | null; state: 'busy' | 'open' | 'parked' | 'closed'
  session: string | null; context_percent: number | null; last_activity: number | null; recall_pending: boolean
}
export type Models = { default: string | null; models: { id: string; name?: string }[]; source?: string }
export type NewRole = { role: string; agent: string; label?: string; rank?: 'lead' | 'worker'; skill?: string; persona?: string; model?: string }
export type Run = { role: string; started: number; ended: number | null; exit: number | null; msgs_handled: number; error: string | null }
export type QuotaWindow = { label: string; remaining_percent: number | null; resets_at?: string | null; unlimited?: boolean }
export type Quota = { engine: string; agent: string; in_use: boolean; windows: QuotaWindow[]; plan?: string | null; note?: string; error?: string; observed_at?: string | null }

export const agentCatalog = () => api<Catalog>('/api/agents/catalog')
export const installAgent = (agent: string) => post<InstallJob>('/api/agents/install', { agent, confirm: true })
export const installStatus = (job: string, offset: number) => api<InstallJob>(`/api/agents/install/${encodeURIComponent(job)}?offset=${offset}`)
export const probeAgent = (agent: string) => post<{ ok: boolean; error?: string; detail?: string }>('/api/agents/probe', { agent })
export const addRole = (r: NewRole) => post<{ ok: boolean; message: string }>('/api/agents/roles', r)
export const agentModels = (agent: string) => api<Models>(`/api/agents/models/${encodeURIComponent(agent)}`)
export type RoomEngine = { role: string; per_room: boolean; overridden: boolean; acp_agent?: string | null; model?: string | null; default_agent?: string | null; default_model?: string | null }
export const roomEngines = (room: number) => api<RoomEngine[]>(`/api/rooms/${room}/engines`)
export const setRoomEngine = (room: number, role: string, agent: string, model: string) =>
  post<RoomEngine & { ok: boolean; state: 'set' | 'cleared' }>(`/api/rooms/${room}/agents/${encodeURIComponent(role)}/engine`, { agent, model })
export const setEngine = (role: string, agent: string, model: string) => post<{ ok: boolean; changed: boolean }>(`/api/agents/roles/${encodeURIComponent(role)}/engine`, { agent, model })
export const roleSessions = (role: string) => api<{ role: string; sessions: Session[] }>(`/api/agents/${encodeURIComponent(role)}/sessions`)
export const closeSession = (role: string, room: number) => post(`/api/agents/${encodeURIComponent(role)}/sessions/${room}/close`, {})
export const recentRuns = (limit = 30) => api<{ runs: Run[] }>(`/api/runs?limit=${limit}`)
export const accountQuota = () => api<{ accounts: Quota[] }>('/api/account-quota')

// ---- Skills ---------------------------------------------------------------------------------
export type SkillItem = { id: string; description: string; packs: string[]; used_by: string[]; bytes: number; mtime: number }
export type SkillRoleCfg = { persona_file: string | null; skills: string[]; skill_packs: string[]; rank: string | null }
export type SkillListing = { skills: SkillItem[]; packs: Record<string, string[]>; rooms: Record<string, { skills: string[]; packs: string[] }>; roles: Record<string, SkillRoleCfg> }
export type SkillVersion = { version: string; bytes: number }
export type SkillLog = { at?: number; actor?: string; action?: string; skill?: string; note?: string }
const put = <T = { ok: boolean }>(path: string, body: unknown) => api<T>(path, { method: 'PUT', body: JSON.stringify(body) })
const del = <T = { ok: boolean }>(path: string) => api<T>(path, { method: 'DELETE' })
export const listSkills = () => api<SkillListing>('/api/skills')
export const getSkill = (id: string) => api<{ id: string; content: string; used_by: string[] }>(`/api/skills/${encodeURIComponent(id)}`)
export const createSkill = (id: string, content: string) => post('/api/skills', { id, content })
export const saveSkill = (id: string, content: string, note = '') => put(`/api/skills/${encodeURIComponent(id)}`, { content, note })
export const deleteSkill = (id: string) => del(`/api/skills/${encodeURIComponent(id)}`)
export const skillHistory = (id: string) => api<{ versions: SkillVersion[]; log: SkillLog[] }>(`/api/skills/${encodeURIComponent(id)}/history`)
export const skillVersion = (id: string, v: string) => api<{ content: string }>(`/api/skills/${encodeURIComponent(id)}/history/${encodeURIComponent(v)}`)
export const restoreSkill = (id: string, version: string) => post(`/api/skills/${encodeURIComponent(id)}/restore`, { version })
export const savePack = (name: string, skills: string[]) => put(`/api/skill-packs/${encodeURIComponent(name)}`, { skills })
export const deletePack = (name: string) => del(`/api/skill-packs/${encodeURIComponent(name)}`)
export const assignRole = (role: string, skills: string[], packs: string[]) => put(`/api/skill-assign/role/${encodeURIComponent(role)}`, { skills, packs })
export const assignRoom = (room: number, skills: string[], packs: string[]) => put(`/api/skill-assign/room/${room}`, { skills, packs })
export const skillPreview = (role: string, room?: number) =>
  api<{ persona: string | null; skills: string[] }>(`/api/skill-preview?role=${encodeURIComponent(role)}${room != null ? `&room=${room}` : ''}`)

// ---- Files ----------------------------------------------------------------------------------
export type Space = { id: string; label: string; writable: boolean; path: string; exists: boolean }
export type FileRow = { name: string; rel: string; size: number; day: string; modified: string; path: string; partial: boolean | number }
export type FileList = { files: FileRow[]; total: number; offset: number; limit: number; truncated: boolean; ready: boolean }
export const filesMe = () => api<{ owner: string; spaces: Space[] }>('/files/api/me')
export const listFiles = (space: string, q: string, offset = 0) =>
  api<FileList>(`/files/api/files?${new URLSearchParams({ space, q, offset: String(offset) })}`)
export const fileDownloadUrl = (space: string, rel: string) => `/files/api/download?${new URLSearchParams({ space, rel })}`
/** Raw-body upload (not base64); the portal requires the X-Portal-Request header. */
export async function uploadFile(space: string, f: File, onProgress?: (p: number) => void) {
  return new Promise<{ name: string; size: number }>((resolve, reject) => {
    const x = new XMLHttpRequest()
    x.open('POST', `/files/api/upload?${new URLSearchParams({ space, name: f.name })}`)
    const t = token.get(); if (t) x.setRequestHeader('Authorization', `Bearer ${t}`)
    x.setRequestHeader('X-Portal-Request', '1')
    x.upload.onprogress = e => { if (e.lengthComputable) onProgress?.(e.loaded / e.total) }
    x.onload = () => {
      let d: any = null; try { d = JSON.parse(x.responseText) } catch { /* empty */ }
      if (x.status >= 200 && x.status < 300) resolve(d)
      else reject(new ApiError(x.status, (d && d.detail) || x.statusText))
    }
    x.onerror = () => reject(new ApiError(0, 'network error'))
    x.send(f)
  })
}

// ---- Room extras: reminders, folders, notify, freeze, global search ---------------------------
export type Reminder = { body: string; minutes: 10 | 15 | 30; enabled: boolean | number; next_due: number | null }
export const getReminder = (room: number) => api<Reminder>(`/api/rooms/${room}/reminder`)
export const setReminder = (room: number, body: string, minutes: number, enabled: boolean) =>
  post<{ ok: boolean; next_due: number; sent_initial: boolean }>(`/api/rooms/${room}/reminder`, { body, minutes, enabled })
export const stopReminder = (room: number) => post(`/api/rooms/${room}/reminder/stop`, {})
export const reminderFiles = (room: number) => api<{ directory: string; files: string[] }>(`/api/rooms/${room}/reminder-files`)
export const reminderFile = (room: number, name: string) => api<{ name: string; body: string }>(`/api/rooms/${room}/reminder-files?name=${encodeURIComponent(name)}`)
export const saveReminderFile = (room: number, name: string, body: string) => post<{ name: string }>(`/api/rooms/${room}/reminder-files`, { name, body })
export const listFolders = () => api<string[]>('/api/folders')
export const createFolder = (name: string) => post('/api/folders', { name })
export const renameFolder = (original: string, name: string) => post('/api/folders', { name, original })
export const deleteFolder = (name: string) => post('/api/folders', { name, delete: true })
export const moveRoom = (room: number, folder: string) => post(`/api/rooms/${room}/manage`, { folder })
export const setNotify = (room: number, enabled: boolean) => post(`/api/rooms/${room}/notify`, { enabled })
export const freezeRoom = (room: number, frozen: boolean, reason: string) => post<{ ok: boolean; state: string }>(`/api/rooms/${room}/freeze`, { frozen, reason })
export type GlobalHit = { id: number; room: number; author: string; body: string; room_name: string }
export const searchAll = (q: string) => api<{ messages: GlobalHit[]; has_more: boolean }>(`/api/search?q=${encodeURIComponent(q)}`)
