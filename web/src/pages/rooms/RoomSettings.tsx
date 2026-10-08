import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  freezeRoom, getApproval, getReminder, inviteMember, listFolders, moveRoom, pinRoom, reminderFile, reminderFiles, renameRoom, roomAction,
  saveReminderFile, searchRoom, setApproval, setNotify, setReminder, stopReminder, type Agent, type Member, type Room, type SearchHit,
} from '../../api'
import Dialog, { btnDanger, btnGhost, btnPrimary, field } from '../../components/Dialog'
import { useI18n } from '../../i18n'
import { HUMAN } from './shared'

type Tab = 'general' | 'members' | 'approval' | 'reminder' | 'search'

export default function RoomSettings({ open, onClose, room, members, agents, onChanged, onJump }: {
  open: boolean; onClose: () => void; room: Room; members: Member[]; agents: Agent[]; onChanged: () => void; onJump: (id: number) => void
}) {
  const { t } = useI18n()
  const [tab, setTab] = useState<Tab>('general')
  const tabs: Tab[] = ['general', 'members', 'approval', 'reminder', 'search']
  return (
    <Dialog open={open} onClose={onClose} title={t('room.settingsTitle', { room: room.name })}>
      <div role="tablist" className="-mt-1 mb-4 flex gap-1 border-b border-line">
        {tabs.map(k => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
            className={`-mb-px border-b-2 px-2.5 py-1.5 text-[13px] ${tab === k ? 'border-accent text-fg' : 'border-transparent text-mute hover:text-fg'}`}>{t(`room.tab.${k}`)}</button>
        ))}
      </div>
      {tab === 'general' && <General room={room} onChanged={onChanged} onClose={onClose} />}
      {tab === 'members' && <Members room={room} members={members} agents={agents} onChanged={onChanged} />}
      {tab === 'approval' && <Approval room={room} onChanged={onChanged} />}
      {tab === 'reminder' && <Reminder room={room} />}
      {tab === 'search' && <Search room={room} members={members} onJump={onJump} />}
    </Dialog>
  )
}

function useAction() {
  const [err, setErr] = useState(''); const [busy, setBusy] = useState(false)
  async function run(fn: () => Promise<unknown>) {
    setBusy(true); setErr('')
    try { await fn(); return true } catch (x) { setErr((x as Error).message); return false } finally { setBusy(false) }
  }
  return { err, busy, run }
}

function General({ room, onChanged, onClose }: { room: Room; onChanged: () => void; onClose: () => void }) {
  const { t } = useI18n()
  const nav = useNavigate()
  const [name, setName] = useState(room.name)
  const [folders, setFolders] = useState<string[]>([])
  const { err, busy, run } = useAction()
  useEffect(() => { listFolders().then(setFolders).catch(() => {}) }, [])
  const act = async (fn: () => Promise<unknown>) => { if (await run(fn)) onChanged() }
  const frozen = room.state === 'frozen'
  return (
    <div className="space-y-4 text-[13px]">
      <form className="flex gap-2" onSubmit={e => { e.preventDefault(); act(() => renameRoom(room.id, name.trim())) }}>
        <input aria-label={t('rooms.name')} className={field} value={name} maxLength={80} onChange={e => setName(e.target.value)} />
        <button className={btnPrimary} disabled={busy || !name.trim() || name.trim() === room.name}>{t('common.save')}</button>
      </form>
      <div className="grid grid-cols-2 gap-3">
        <label><span className="mb-1 block text-mute">{t('room.folder')}</span>
          <select className={field} value={room.folder ?? ''} disabled={busy} onChange={e => act(() => moveRoom(room.id, e.target.value))}>
            <option value="">{t('room.noFolder')}</option>{folders.map(f => <option key={f} value={f}>{f}</option>)}</select></label>
        <label className="flex items-end gap-2 pb-2"><input type="checkbox" checked={!!room.notify} disabled={busy || room.state !== 'active'}
          onChange={e => act(() => setNotify(room.id, e.target.checked))} /><span>{t('room.notify')}</span></label>
      </div>
      <p className="-mt-2 text-xs text-mute">{t('room.notifyHelp')}</p>
      <div className="flex flex-wrap gap-2">
        <button className={btnGhost} disabled={busy} onClick={() => act(() => pinRoom(room.id, !room.pinned))}>{room.pinned ? t('room.unpin') : t('room.pin')}</button>
        {(room.state === 'active' || frozen) && <button className={btnGhost} disabled={busy} onClick={() => {
          const reason = window.prompt(t(frozen ? 'room.unfreezeReason' : 'room.freezeReason'))
          if (reason?.trim()) act(() => freezeRoom(room.id, !frozen, reason.trim()))
        }}>{frozen ? t('room.unfreeze') : t('room.freeze')}</button>}
        {room.state === 'active' && <button className={btnGhost} disabled={busy} onClick={() => act(() => roomAction(room.id, 'archive'))}>{t('room.archive')}</button>}
        {(room.state === 'archived' || room.state === 'deleted') && <button className={btnGhost} disabled={busy} onClick={() => act(() => roomAction(room.id, 'restore'))}>{t('room.restore')}</button>}
        {room.state !== 'deleted' && (
          <button className={btnDanger} disabled={busy} onClick={() => {
            if (window.confirm(t('room.deleteConfirm', { room: room.name }))) act(() => roomAction(room.id, 'delete')).then(() => { onClose(); nav('/rooms') })
          }}>{t('room.delete')}</button>
        )}
      </div>
      <p className="text-xs text-mute">{t('room.stateNote')}</p>
      {err && <p role="alert" className="text-danger">{err}</p>}
    </div>
  )
}

function Reminder({ room }: { room: Room }) {
  const { t } = useI18n()
  const [body, setBody] = useState('')
  const [minutes, setMinutes] = useState(30)
  const [enabled, setEnabled] = useState(false)
  const [next, setNext] = useState<number | null>(null)
  const [files, setFiles] = useState<string[]>([])
  const [dir, setDir] = useState('')
  const [pick, setPick] = useState('')
  const [saveAs, setSaveAs] = useState('')
  const [msg, setMsg] = useState('')
  const { err, busy, run } = useAction()
  const load = () => {
    getReminder(room.id).then(r => { setBody(r.body); setMinutes(r.minutes || 30); setEnabled(!!r.enabled); setNext(r.next_due) }).catch(() => {})
    reminderFiles(room.id).then(d => { setFiles(d.files); setDir(d.directory) }).catch(() => {})
  }
  useEffect(load, [room.id])
  const nextText = next && enabled ? new Date(next * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null
  return (
    <div className="space-y-3 text-[13px]">
      <p className="text-mute">{t('room.reminderHelp')}</p>
      <p>{enabled ? <span className="text-ok">● {t('room.reminderOn', { m: minutes })}{nextText ? ` · ${t('room.reminderNext', { time: nextText })}` : ''}</span> : <span className="text-mute">{t('room.reminderOff')}</span>}</p>
      <div className="flex gap-2">
        <select aria-label={t('room.loadTemplate')} className={field} value={pick} onChange={e => setPick(e.target.value)}>
          <option value="">{t('room.loadTemplate')}…</option>{files.map(f => <option key={f} value={f}>{f}</option>)}</select>
        <button className={btnGhost} disabled={!pick || busy} onClick={() => run(async () => { setBody((await reminderFile(room.id, pick)).body); setMsg('') })}>{t('room.load')}</button>
      </div>
      <textarea aria-label={t('room.reminderText')} className={`${field} h-40 font-mono text-xs`} maxLength={12000} value={body} onChange={e => setBody(e.target.value)} />
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label={t('room.every')} className={`${field} w-auto`} value={minutes} onChange={e => setMinutes(Number(e.target.value))}>
          {[10, 15, 30].map(m => <option key={m} value={m}>{t('room.everyN', { m })}</option>)}</select>
        <button className={btnPrimary} disabled={busy || !body.trim() || room.state !== 'active'} onClick={async () => {
          if (await run(async () => { const r = await setReminder(room.id, body, minutes, true); setNext(r.next_due); setEnabled(true); setMsg(r.sent_initial ? t('room.reminderSent') : t('room.reminderSaved')) })) {/* ok */}
        }}>{enabled ? t('room.reminderUpdate') : t('room.reminderStart')}</button>
        {enabled && <button className={btnGhost} disabled={busy} onClick={() => run(async () => { await stopReminder(room.id); setEnabled(false); setMsg(t('room.reminderStopped')) })}>{t('room.reminderStop')}</button>}
      </div>
      <details className="rounded-lg border border-line px-3 py-2">
        <summary className="cursor-pointer text-mute">{t('room.saveAsFile')}</summary>
        <form className="mt-2 flex gap-2" onSubmit={e => { e.preventDefault(); run(async () => { const r = await saveReminderFile(room.id, saveAs.trim(), body); setMsg(t('room.savedAs', { name: r.name })); setSaveAs(''); load() }) }}>
          <input aria-label={t('room.fileName')} placeholder="my-standup" className={field} value={saveAs} onChange={e => setSaveAs(e.target.value)} />
          <button className={btnGhost} disabled={busy || !saveAs.trim() || !body.trim()}>{t('common.save')}</button>
        </form>
        {dir && <p className="mt-1 text-xs text-mute">{dir}</p>}
      </details>
      {msg && <p role="status" className="text-ok">{msg}</p>}
      {err && <p role="alert" className="text-danger">{err}</p>}
    </div>
  )
}

function Members({ room, members, agents, onChanged }: { room: Room; members: Member[]; agents: Agent[]; onChanged: () => void }) {
  const { t } = useI18n()
  const inRoom = new Set(members.map(m => m.id))
  const candidates = agents.filter(a => a.id !== HUMAN && !inRoom.has(a.id))
  const [agent, setAgent] = useState(candidates[0]?.id ?? '')
  const [reason, setReason] = useState('')
  const { err, busy, run } = useAction()
  return (
    <div className="text-[13px]">
      <ul className="mb-4 max-h-40 overflow-auto rounded-lg border border-line">
        {members.filter(m => !m.id.startsWith('__')).map(m => (
          <li key={m.id} className="flex justify-between border-b border-line px-3 py-1.5 last:border-0">
            <span>{m.id === HUMAN ? t('msg.you') : m.label}</span><span className="text-mute">{m.active === 0 ? t('room.inactive') : m.id}</span>
          </li>
        ))}
      </ul>
      {candidates.length === 0 ? <p className="text-mute">{t('room.everyoneIn')}</p> : (
        <form className="space-y-2" onSubmit={async e => { e.preventDefault(); if (await run(() => inviteMember(room.id, agent, reason.trim()))) { setReason(''); onChanged() } }}>
          <select aria-label={t('room.invite')} className={field} value={agent} onChange={e => setAgent(e.target.value)}>
            {candidates.map(a => <option key={a.id} value={a.id}>{a.label} ({a.id})</option>)}
          </select>
          <input aria-label={t('room.inviteReason')} placeholder={t('room.inviteReason')} className={field} value={reason} maxLength={500} onChange={e => setReason(e.target.value)} />
          <div className="flex justify-end"><button className={btnPrimary} disabled={busy || !agent || !reason.trim()}>{t('room.invite')}</button></div>
        </form>
      )}
      {err && <p role="alert" className="mt-2 text-danger">{err}</p>}
    </div>
  )
}

function Approval({ room, onChanged }: { room: Room; onChanged: () => void }) {
  const { t } = useI18n()
  const today = new Date().toISOString().slice(0, 10)
  const week = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10)
  const [state, setState] = useState(room.auto_approve)
  const [until, setUntil] = useState(room.auto_approve.until?.slice(0, 10) || week)
  const [forever, setForever] = useState(room.auto_approve.on && !room.auto_approve.until)
  const { err, busy, run } = useAction()
  useEffect(() => { getApproval(room.id).then(a => { setState(a); if (a.until) setUntil(a.until.slice(0, 10)) }).catch(() => {}) }, [room.id])
  const apply = async (on: boolean) => {
    if (await run(() => setApproval(room.id, on, on && !forever ? until : null))) { setState({ on, until: on && !forever ? until : null }); onChanged() }
  }
  return (
    <div className="space-y-3 text-[13px]">
      <p className="text-mute">{t('room.approvalHelp')}</p>
      <p>{state.on ? <span className="text-warn">⚡ {state.until ? t('room.autoUntil', { date: state.until.slice(0, 10) }) : t('room.autoOn')}</span> : t('room.approvalOff')}</p>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-1.5"><input type="checkbox" checked={forever} onChange={e => setForever(e.target.checked)} />{t('room.noExpiry')}</label>
        {!forever && <input type="date" aria-label={t('room.until')} className={`${field} w-auto`} min={today} value={until} onChange={e => setUntil(e.target.value)} />}
      </div>
      <div className="flex justify-end gap-2">
        {state.on && <button className={btnGhost} disabled={busy} onClick={() => apply(false)}>{t('room.approvalTurnOff')}</button>}
        <button className={btnPrimary} disabled={busy || (!forever && !until)} onClick={() => apply(true)}>{state.on ? t('common.save') : t('room.approvalTurnOn')}</button>
      </div>
      {err && <p role="alert" className="text-danger">{err}</p>}
    </div>
  )
}

function Search({ room, members, onJump }: { room: Room; members: Member[]; onJump: (id: number) => void }) {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const [mentioned, setMentioned] = useState(false)
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const { err, busy, run } = useAction()
  const who = (id: string) => id === HUMAN ? t('msg.you') : members.find(m => m.id === id)?.label || id
  return (
    <div className="text-[13px]">
      <form className="mb-3 flex gap-2" onSubmit={e => { e.preventDefault(); run(async () => setHits((await searchRoom(room.id, q.trim(), mentioned)).results)) }}>
        <input aria-label={t('room.searchBox')} placeholder={t('room.searchBox')} className={field} value={q} maxLength={80} onChange={e => setQ(e.target.value)} autoFocus />
        <button className={btnPrimary} disabled={busy || (!q.trim() && !mentioned)}>{t('rooms.search')}</button>
      </form>
      <label className="mb-3 flex items-center gap-1.5 text-mute"><input type="checkbox" checked={mentioned} onChange={e => setMentioned(e.target.checked)} />{t('room.onlyMentions')}</label>
      {err && <p role="alert" className="mb-2 text-danger">{err}</p>}
      {hits && (hits.length === 0 ? <p className="text-mute">{t('room.noResults')}</p> : (
        <ul className="max-h-72 overflow-auto rounded-lg border border-line">
          {hits.map(h => (
            <li key={h.id}><button onClick={() => onJump(h.id)} className="block w-full border-b border-line px-3 py-2 text-left last:border-0 hover:bg-sub">
              <span className="text-xs text-mute">#{h.id} · {who(h.author)}</span>
              <span className="block truncate">{h.snippet}</span>
            </button></li>
          ))}
        </ul>
      ))}
    </div>
  )
}
