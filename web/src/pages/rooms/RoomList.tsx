import { useEffect, useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import { createFolder, createRoom, deleteFolder, listFolders, renameFolder, searchAll, type Agent, type GlobalHit, type Room } from '../../api'
import Dialog, { btnGhost, btnPrimary, field } from '../../components/Dialog'
import { useI18n } from '../../i18n'

type Sec = { key: string; label: string; match: (r: Room) => boolean; collapsible?: boolean }
const live = (r: Room) => r.state !== 'archived' && r.state !== 'deleted'

export default function RoomList({ rooms, agents, onChanged }: { rooms: Room[] | null; agents: Agent[]; onChanged: () => void }) {
  const { t } = useI18n()
  const nav = useNavigate()
  const [creating, setCreating] = useState(false)
  const [managing, setManaging] = useState(false)
  const [filter, setFilter] = useState('')
  const [hits, setHits] = useState<GlobalHit[] | null>(null)
  const [folders, setFolders] = useState<string[]>([])
  const [closed, setClosed] = useState<Record<string, boolean>>(() => {
    try { return JSON.parse(localStorage.getItem('aaf-closed-sections') || '') } catch { return { 'rooms.archived': true, 'rooms.trash': true } }
  })
  const loadFolders = () => listFolders().then(setFolders).catch(() => {})
  useEffect(() => { loadFolders() }, [rooms?.length])
  useEffect(() => { localStorage.setItem('aaf-closed-sections', JSON.stringify(closed)) }, [closed])
  const shown = (rooms ?? []).filter(r => !filter || r.name.toLowerCase().includes(filter.toLowerCase()))
  const sections: Sec[] = [
    { key: 'rooms.pinned', label: t('rooms.pinned'), match: r => !!r.pinned && live(r) },
    ...folders.map(f => ({ key: `folder:${f}`, label: f, collapsible: true, match: (r: Room) => !r.pinned && live(r) && r.folder === f })),
    { key: 'rooms.title', label: folders.length ? t('rooms.unfiled') : t('rooms.title'), match: r => !r.pinned && live(r) && (!r.folder || !folders.includes(r.folder)) },
    { key: 'rooms.archived', label: t('rooms.archived'), collapsible: true, match: r => r.state === 'archived' },
    { key: 'rooms.trash', label: t('rooms.trash'), collapsible: true, match: r => r.state === 'deleted' },
  ]
  async function searchMessages() {
    const q = filter.trim(); if (!q) return
    try { setHits((await searchAll(q)).messages) } catch { setHits([]) }
  }
  return (
    <aside aria-label={t('rooms.title')} className="flex w-60 flex-none flex-col border-r border-line bg-panel">
      <header className="flex items-center justify-between px-3.5 pb-2 pt-3.5">
        <h2 className="text-[13px] font-semibold">{t('rooms.title')}</h2>
        <div className="flex">
          <button className="rounded-md px-1.5 py-1 text-xs text-mute hover:bg-sub hover:text-fg" title={t('rooms.folders')} aria-label={t('rooms.folders')} onClick={() => setManaging(true)}>▤</button>
          <button className="rounded-md px-2 py-1 text-xs text-mute hover:bg-sub hover:text-fg" onClick={() => setCreating(true)}>＋ {t('rooms.new')}</button>
        </div>
      </header>
      <form onSubmit={e => { e.preventDefault(); searchMessages() }}>
        <input aria-label={t('rooms.filter')} placeholder={t('rooms.filterOrSearch')} value={filter} onChange={e => { setFilter(e.target.value); setHits(null) }}
          className="mx-3 mb-2 w-[calc(100%-1.5rem)] rounded-[7px] border border-line bg-bg px-2.5 py-1.5 text-[13px] outline-none focus:border-accent" />
      </form>
      <div className="flex-1 overflow-auto pb-2">
        {filter.trim() && hits === null && <button className="mx-3 mb-1 text-xs text-accent hover:underline" onClick={searchMessages}>{t('rooms.searchMessages', { q: filter.trim() })}</button>}
        {hits && (
          <section aria-label={t('rooms.messageHits')} className="mb-2 border-b border-line pb-2">
            <div className="px-4 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wider text-mute">{t('rooms.messageHits')} · {hits.length}</div>
            {hits.length === 0 && <p className="px-4 text-xs text-mute">{t('room.noResults')}</p>}
            {hits.map(h => (
              <button key={h.id} onClick={() => nav(`/rooms/${h.room}#msg-${h.id}`)} className="mx-1.5 block w-[calc(100%-0.75rem)] rounded-md px-3 py-1.5 text-left hover:bg-sub">
                <span className="block text-[11px] text-mute">#{h.room_name} · #{h.id}</span><span className="line-clamp-2 text-xs">{h.body}</span>
              </button>
            ))}
          </section>
        )}
        {rooms === null && <p className="px-4 text-[13px] text-mute">{t('common.loading')}</p>}
        {rooms !== null && rooms.length === 0 && <p className="px-4 text-[13px] text-mute">{t('rooms.empty')}</p>}
        {sections.map(s => {
          const list = shown.filter(s.match)
          if (!list.length) return null
          const open = !s.collapsible || !closed[s.key] || !!filter
          return (
            <section key={s.key} className="mb-1">
              <button disabled={!s.collapsible} onClick={() => setClosed(o => ({ ...o, [s.key]: !o[s.key] }))}
                className="flex w-full items-center gap-1 px-4 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wider text-mute">
                {s.collapsible && <span className={`inline-block transition-transform ${open ? 'rotate-90' : ''}`}>›</span>}
                <span className="truncate">{s.label}</span> {s.collapsible && <span className="ml-auto font-normal">{list.length}</span>}
              </button>
              {open && list.map(r => <RoomLink key={r.id} r={r} />)}
            </section>
          )
        })}
      </div>
      <CreateRoom open={creating} agents={agents} onClose={() => setCreating(false)} onCreated={onChanged} />
      <Folders open={managing} folders={folders} onClose={() => setManaging(false)} onChanged={() => { loadFolders(); onChanged() }} />
    </aside>
  )
}

function Folders({ open, folders, onClose, onChanged }: { open: boolean; folders: string[]; onClose: () => void; onChanged: () => void }) {
  const { t } = useI18n()
  const [name, setName] = useState('')
  const [err, setErr] = useState('')
  const act = async (fn: () => Promise<unknown>) => { setErr(''); try { await fn(); onChanged() } catch (e) { setErr((e as Error).message) } }
  return (
    <Dialog open={open} onClose={onClose} title={t('rooms.folders')}>
      <div className="space-y-3 text-[13px]">
        <p className="text-xs text-mute">{t('rooms.foldersHelp')}</p>
        <ul className="overflow-hidden rounded-lg border border-line">
          {folders.length === 0 && <li className="px-3 py-2 text-mute">{t('rooms.noFolders')}</li>}
          {folders.map(f => (
            <li key={f} className="flex items-center gap-2 border-b border-line px-3 py-1.5 last:border-0">
              <span className="flex-1 truncate">{f}</span>
              <button className="rounded px-2 py-0.5 text-xs text-mute hover:bg-sub hover:text-fg" onClick={() => { const n = window.prompt(t('rooms.renameFolder'), f); if (n?.trim() && n.trim() !== f) act(() => renameFolder(f, n.trim())) }}>{t('rooms.rename')}</button>
              <button className="rounded px-2 py-0.5 text-xs text-danger hover:bg-danger/10" onClick={() => { if (window.confirm(t('rooms.deleteFolderConfirm', { f }))) act(() => deleteFolder(f)) }}>{t('common.delete')}</button>
            </li>
          ))}
        </ul>
        <form className="flex gap-2" onSubmit={e => { e.preventDefault(); const n = name.trim(); if (n) act(() => createFolder(n)).then(() => setName('')) }}>
          <input aria-label={t('rooms.newFolder')} placeholder={t('rooms.newFolder')} className={field} value={name} maxLength={40} onChange={e => setName(e.target.value)} />
          <button className={btnPrimary} disabled={!name.trim()}>{t('rooms.create')}</button>
        </form>
        {err && <p role="alert" className="text-danger">{err}</p>}
        <div className="flex justify-end"><button className={btnGhost} onClick={onClose}>{t('common.close')}</button></div>
      </div>
    </Dialog>
  )
}

function RoomLink({ r }: { r: Room }) {
  const { t } = useI18n()
  return (
    <NavLink to={`/rooms/${r.id}`}
      className={({ isActive }) => `mx-1.5 my-px flex items-center gap-2 rounded-md px-3.5 py-1.5 text-[13.5px] ${isActive ? 'bg-sub font-medium text-fg' : 'text-mute hover:bg-sub hover:text-fg'} ${r.state === 'deleted' ? 'line-through opacity-70' : ''}`}>
      <span className="opacity-60">#</span><span className="truncate">{r.name}</span>
      {r.unread > 0
        ? <span className="ml-auto rounded-full bg-accent px-1.5 text-[11px] font-semibold text-white" aria-label={t('rooms.unread', { n: r.unread })}>{r.unread}</span>
        : r.auto_approve?.on ? <span className="ml-auto text-[11px] text-warn" title={t('room.autoOn')}>⚡</span> : null}
    </NavLink>
  )
}

function CreateRoom({ open, agents, onClose, onCreated }: { open: boolean; agents: Agent[]; onClose: () => void; onCreated: () => void }) {
  const { t } = useI18n()
  const nav = useNavigate()
  const [name, setName] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const choices = agents.filter(a => a.id !== 'Owner')
  async function submit() {
    setBusy(true); setErr('')
    try {
      const r = await createRoom(name.trim(), picked)
      setName(''); setPicked([]); onClose(); onCreated(); nav(`/rooms/${r.id}`)
    } catch (x) { setErr((x as Error).message) } finally { setBusy(false) }
  }
  return (
    <Dialog open={open} onClose={onClose} title={t('rooms.new')}>
      <form onSubmit={e => { e.preventDefault(); submit() }}>
        <label className="mb-3 block text-[13px]"><span className="mb-1 block text-mute">{t('rooms.name')}</span>
          <input className={field} value={name} maxLength={80} onChange={e => setName(e.target.value)} required autoFocus /></label>
        <fieldset className="mb-3 text-[13px]"><legend className="mb-1 text-mute">{t('rooms.pickMembers')}</legend>
          <div className="max-h-48 overflow-auto rounded-lg border border-line p-1">
            {choices.map(a => (
              <label key={a.id} className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-sub">
                <input type="checkbox" checked={picked.includes(a.id)} onChange={e => setPicked(p => e.target.checked ? [...p, a.id] : p.filter(x => x !== a.id))} />
                <span>{a.label}</span><span className="ml-auto text-xs text-mute">{a.id}</span>
              </label>
            ))}
          </div>
        </fieldset>
        {err && <p role="alert" className="mb-3 text-[13px] text-danger">{err}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className={btnGhost} onClick={onClose}>{t('common.cancel')}</button>
          <button className={btnPrimary} disabled={busy || !name.trim() || !picked.length}>{t('rooms.create')}</button>
        </div>
      </form>
    </Dialog>
  )
}
