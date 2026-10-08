import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { confirmRead, markRead, roomEngines, roomMessages, setLike, type Agent, type Member, type Message, type Room, type RoomEngine } from '../../api'
import RoomEngineDialog from './RoomEngineDialog'
import AuthImage from '../../components/AuthImage'
import Icon from '../../components/Icon'
import { colorFor, renderBody, timeOf } from '../../lib/format'
import { useI18n } from '../../i18n'
import Composer from './Composer'
import RoomSettings from './RoomSettings'
import { HUMAN, usePoll } from './shared'

const SYSTEM = new Set(['__system__', '__reminder__'])

export default function Thread({ room, agents, showMembers, toggleMembers, onRoomChanged }: {
  room: Room; agents: Agent[]; showMembers: boolean; toggleMembers: () => void; onRoomChanged: () => void
}) {
  const { t, lang } = useI18n()
  const [msgs, setMsgs] = useState<Message[]>([])
  const [members, setMembers] = useState<Member[]>([])
  const [loaded, setLoaded] = useState(false)
  const [replyTo, setReplyTo] = useState<Message | null>(null)
  const [settings, setSettings] = useState(false)
  const [flash, setFlash] = useState<number | null>(null)
  const feed = useRef<HTMLDivElement>(null)
  const sig = useRef('')
  const readThrough = useRef(0)
  const lastId = useRef(0)

  const load = useCallback(async () => {
    try {
      // Page through everything (server caps 500 per call) so long rooms aren't truncated.
      const all: Message[] = []; let after = 0, d
      do {
        d = await roomMessages(room.id, after)
        all.push(...d.messages); if (d.messages.length) after = d.messages.at(-1)!.id
      } while (d.has_more)
      setMembers(d.members); setLoaded(true)
      // Re-render only when something visible changed (new message, likes, receipts, delivery).
      const s = JSON.stringify(all.map(m => [m.id, m.likes?.length, m.read_by?.length, m.delivery?.map(x => x.status).join()]))
      if (s !== sig.current) {
        const el = feed.current
        const atBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight < 80
        const grew = all.at(-1)?.id !== lastId.current
        sig.current = s; lastId.current = all.at(-1)?.id ?? 0; setMsgs(all)
        if (atBottom && grew) requestAnimationFrame(() => { const f = feed.current; if (f) f.scrollTop = f.scrollHeight })
      }
      // Fetch cursor only (unread counter). Read receipts are explicit, per message (spec): see confirm().
      const top = all.at(-1)?.id ?? 0
      if (top > readThrough.current) { readThrough.current = top; markRead(room.id, top).catch(() => {}) }
    } catch { /* keep last view; next poll retries */ }
  }, [room.id])
  usePoll(load, 3000, [load])

  const label = useCallback((id: string) => id === HUMAN ? t('msg.you') : members.find(m => m.id === id)?.label || agents.find(a => a.id === id)?.label || id, [members, agents, t])
  const byId = useMemo(() => new Map(msgs.map(m => [m.id, m])), [msgs])
  const readOnly = room.state !== 'active'
  const until = room.auto_approve?.on ? room.auto_approve.until : null

  const loc = useLocation()
  useEffect(() => {
    const m = /^#msg-(\d+)$/.exec(loc.hash)
    if (m && msgs.some(x => x.id === Number(m[1]))) setTimeout(() => jump(Number(m[1])), 0)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loc.hash, msgs.length > 0])

  function jump(id: number) {
    document.getElementById(`msg-${id}`)?.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
    setFlash(id); setTimeout(() => setFlash(null), 1500)
  }

  async function confirm(m: Message) {
    setMsgs(list => list.map(x => x.id === m.id ? { ...x, read_by: [...(x.read_by ?? []), { id: HUMAN, label: '' }] } : x))
    try { await confirmRead(room.id, [m.id]) } finally { load() }
  }

  async function toggleLike(m: Message) {
    const liked = !m.likes?.some(p => p.id === HUMAN)
    setMsgs(list => list.map(x => x.id === m.id ? { ...x, likes: liked ? [...(x.likes ?? []), { id: HUMAN, label: '' }] : (x.likes ?? []).filter(p => p.id !== HUMAN) } : x))
    try { await setLike(room.id, m.id, liked) } finally { load() }
  }

  return (
    <>
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-[52px] flex-none items-center gap-2.5 border-b border-line bg-panel px-5">
          <span className="text-mute">#</span><h1 className="truncate text-[15px] font-semibold">{room.name}</h1>
          {room.state !== 'active' && <span className="rounded-full bg-sub px-2 py-0.5 text-xs text-mute">{t(`room.state.${room.state}`)}</span>}
          {room.auto_approve?.on && (
            <button onClick={() => setSettings(true)} className="rounded-full bg-warn/12 px-2 py-0.5 text-xs text-warn">
              ⚡ {until ? t('room.autoUntil', { date: until.slice(0, 10) }) : t('room.autoOn')}
            </button>
          )}
          <div className="flex-1" />
          <button className="rounded-md px-2 py-1 text-[13px] text-mute hover:bg-sub hover:text-fg" onClick={() => setSettings(true)}>{t('room.settings')}</button>
          <button className="rounded-md p-1.5 text-mute hover:bg-sub hover:text-fg" title={t('room.hideMembers')} aria-label={t('room.hideMembers')}
            aria-pressed={showMembers} onClick={toggleMembers}><Icon name="more" /></button>
        </div>
        <div ref={feed} className="flex-1 overflow-auto py-4">
          {!loaded && <p className="px-5 text-mute">{t('common.loading')}</p>}
          {loaded && msgs.length === 0 && <p className="px-5 text-mute">{t('room.emptyThread')}</p>}
          {msgs.map(m => SYSTEM.has(m.author)
            ? <div key={m.id} id={`msg-${m.id}`} className={`whitespace-pre-wrap break-words py-1.5 pl-[68px] pr-5 text-[12.5px] text-mute ${flash === m.id ? 'bg-accent-soft' : ''}`}>{renderBody(m.body)}</div>
            : <MessageRow key={m.id} m={m} parent={m.reply_to ? byId.get(m.reply_to) : undefined} label={label} lang={lang}
                flash={flash === m.id} readOnly={readOnly} onReply={() => setReplyTo(m)} onLike={() => toggleLike(m)} onConfirm={() => confirm(m)} onJump={jump} />)}
        </div>
        {readOnly
          ? <div className="mx-5 mb-4 rounded-[10px] border border-dashed border-line px-4 py-3 text-[13px] text-mute">{t('room.readOnly')}</div>
          : <Composer room={room} members={members} replyTo={replyTo} clearReply={() => setReplyTo(null)} onSent={load} label={label} />}
      </main>
      {showMembers && <MembersPanel room={room.id} members={members} agents={agents} />}
      <RoomSettings open={settings} onClose={() => setSettings(false)} room={room} members={members} agents={agents}
        onChanged={() => { onRoomChanged(); load() }} onJump={id => { setSettings(false); jump(id) }} />
    </>
  )
}

function MessageRow({ m, parent, label, lang, flash, readOnly, onReply, onLike, onConfirm, onJump }: {
  m: Message; parent?: Message; label: (id: string) => string; lang: string; flash: boolean; readOnly: boolean
  onReply: () => void; onLike: () => void; onConfirm: () => void; onJump: (id: number) => void
}) {
  const { t } = useI18n()
  const liked = m.likes?.some(p => p.id === HUMAN)
  const readers = (m.read_by ?? []).filter(p => p.id !== m.author)
  const pending = (m.delivery ?? []).filter(d => d.status === 'pending').length
  const failed = (m.delivery ?? []).filter(d => d.status === 'failed').length
  const mentionsMe = m.author !== HUMAN && m.mentions?.includes(HUMAN)
  const iConfirmed = m.read_by?.some(p => p.id === HUMAN)
  return (
    <article id={`msg-${m.id}`} className={`group relative grid grid-cols-[36px_1fr] gap-3 px-5 py-2 hover:bg-sub ${flash ? 'bg-accent-soft' : ''} ${mentionsMe ? 'border-l-2 border-accent' : ''}`}>
      <div className="grid size-8 place-items-center rounded-lg text-[13px] font-semibold text-white" style={{ background: m.author === HUMAN ? 'var(--mute)' : colorFor(m.author) }}>
        {label(m.author).slice(0, 1).toUpperCase()}
      </div>
      <div className="min-w-0">
        {m.reply_to != null && (
          <button onClick={() => onJump(m.reply_to!)} className="mb-0.5 block max-w-full truncate text-left text-xs text-mute hover:text-fg">
            ↳ #{m.reply_to} {parent ? `${label(parent.author)}: ${parent.body.slice(0, 80)}` : ''}
          </button>
        )}
        <div className="flex items-baseline gap-2">
          <b className="font-semibold">{label(m.author)}</b>
          <time className="text-xs text-mute" dateTime={new Date(m.created * 1000).toISOString()} title={new Date(m.created * 1000).toLocaleString(lang)}>{timeOf(m.created, lang)}</time>
          <span className="text-xs text-mute">#{m.id}</span>
          {m.notification_kind === 'task' && <span className="rounded border border-line px-1.5 text-[11px] text-mute">{t('msg.task')}</span>}
        </div>
        {m.body && <div className="mt-0.5 whitespace-pre-wrap break-words">{renderBody(m.body)}</div>}
        {m.image && <div className="mt-1.5"><AuthImage src={m.image.url} alt={t('msg.image')} className="max-h-72 max-w-full rounded-lg border border-line" /></div>}
        {m.file && <FileChip url={m.file.url} name={m.file.name} size={m.file.size} />}
        <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-mute">
          {(m.likes?.length ?? 0) > 0 && <span title={m.likes!.map(p => p.id === HUMAN ? t('msg.you') : p.label).join(', ')}>♥ {m.likes!.length}</span>}
          {m.author === HUMAN && readers.length > 0 && <span>{t('msg.readBy', { names: readers.map(p => p.label).join(', ') })}</span>}
          {m.author === HUMAN && pending > 0 && <span>{t('msg.pending', { n: pending })}</span>}
          {m.author === HUMAN && failed > 0 && <span className="text-danger">{t('msg.failed', { n: failed })}</span>}
          {m.author !== HUMAN && (iConfirmed
            ? <span>✓ {t('msg.confirmed')}</span>
            : <button className="rounded px-1.5 text-accent hover:bg-accent-soft disabled:opacity-50" disabled={readOnly}
                aria-label={t('msg.confirmAria', { id: m.id })} onClick={onConfirm}>{t('msg.confirm')}</button>)}
        </div>
      </div>
      {!readOnly && (
        <div className="absolute right-4 top-1 hidden gap-0.5 rounded-lg border border-line bg-panel p-0.5 shadow-sm group-hover:flex group-focus-within:flex">
          <button className="rounded-md px-2 py-0.5 text-xs hover:bg-sub" onClick={onReply}>{t('msg.reply')}</button>
          <button className={`rounded-md px-2 py-0.5 text-xs hover:bg-sub ${liked ? 'text-danger' : ''}`} aria-pressed={liked} onClick={onLike}>♥ {liked ? t('msg.unlike') : t('msg.like')}</button>
        </div>
      )}
    </article>
  )
}

function FileChip({ url, name, size }: { url: string; name: string; size: number }) {
  const [busy, setBusy] = useState(false)
  async function download() {
    setBusy(true)
    try {
      const { blob } = await import('../../api')
      const b = await blob(url); const u = URL.createObjectURL(b)
      const a = document.createElement('a'); a.href = u; a.download = name; a.click()
      setTimeout(() => URL.revokeObjectURL(u), 10_000)
    } finally { setBusy(false) }
  }
  return (
    <button onClick={download} disabled={busy} className="mt-1.5 inline-flex items-center gap-1.5 rounded-md border border-line bg-panel px-2.5 py-1 text-[12.5px] text-mute hover:text-fg">
      📎 {name} <span className="opacity-70">{fmtSize(size)}</span>
    </button>
  )
}

export function fmtSize(n: number) {
  return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`
}

function MembersPanel({ room, members, agents }: { room: number; members: Member[]; agents: Agent[] }) {
  const { t } = useI18n()
  const people = members.filter(m => !m.id.startsWith('__'))
  const [engines, setEngines] = useState<RoomEngine[]>([])
  const [editing, setEditing] = useState<RoomEngine | null>(null)
  const loadEngines = useCallback(() => { roomEngines(room).then(setEngines).catch(() => setEngines([])) }, [room])
  useEffect(loadEngines, [loadEngines, members.length])
  return (
    <aside aria-label={t('room.members')} className="w-[280px] flex-none overflow-auto border-l border-line bg-panel p-4">
      <h3 className="mb-2.5 mt-1 text-xs font-medium uppercase tracking-wider text-mute">{t('room.members')} · {people.length}</h3>
      {people.map(m => {
        const a = agents.find(x => x.id === m.id)
        const e = engines.find(x => x.role === m.id)
        const online = m.id === HUMAN || !!a?.online
        const name = m.id === HUMAN ? t('msg.you') : m.label || m.id
        const engineText = e ? [e.acp_agent, e.model].filter(Boolean).join(' · ') : [a?.acp_agent, a?.model].filter(Boolean).join(' · ')
        return (
          <div key={m.id} role={e ? 'button' : undefined} tabIndex={e ? 0 : undefined} title={e ? t('engine.open') : undefined}
            onClick={e ? () => setEditing(e) : undefined} onKeyDown={e ? ev => { if (ev.key === 'Enter') setEditing(e) } : undefined}
            className={`grid grid-cols-[28px_1fr_auto] items-center gap-2.5 rounded-lg p-2 hover:bg-sub ${e ? 'cursor-pointer' : ''}`}>
            <div className="grid size-7 place-items-center rounded-[7px] text-xs font-semibold text-white" style={{ background: m.id === HUMAN ? 'var(--mute)' : colorFor(m.id) }}>{name.slice(0, 1).toUpperCase()}</div>
            <div className="min-w-0">
              <b className="font-medium">{name}</b>
              {engineText && <small className="block truncate text-xs text-mute">{engineText}{e?.overridden && <span className="ml-1.5 rounded bg-accent-soft px-1 text-[11px] text-accent">{t('engine.thisRoom')}</span>}</small>}
            </div>
            <span title={online ? t('agent.online') : t('agent.offline')} className={`size-2 rounded-full ${online ? 'bg-ok' : 'bg-off'}`} />
          </div>
        )
      })}
      {editing && <RoomEngineDialog room={room} engine={editing} label={members.find(m => m.id === editing.role)?.label || editing.role}
        onClose={() => setEditing(null)} onSaved={loadEngines} />}
    </aside>
  )
}
