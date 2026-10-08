import { useCallback, useEffect, useMemo, useState } from 'react'
import { NavLink, useNavigate, useParams } from 'react-router-dom'
import {
  assignRole, assignRoom, createSkill, deletePack, deleteSkill, getSkill, listRooms, listSkills, restoreSkill, saveSkill,
  savePack, skillHistory, skillPreview, skillVersion, type Room, type SkillListing, type SkillLog, type SkillVersion,
} from '../api'
import Dialog, { btnDanger, btnGhost, btnPrimary, field } from '../components/Dialog'
import { useI18n } from '../i18n'

const TEMPLATE = (id: string) => `---\nname: ${id}\ndescription: One line: what this skill is for and when to use it.\n---\n# ${id}\n\n## When to use\n\n## Steps\n1. \n`

export default function Skills() {
  const { t } = useI18n()
  const { skillId } = useParams()
  const nav = useNavigate()
  const [data, setData] = useState<SkillListing | null>(null)
  const [rooms, setRooms] = useState<Room[]>([])
  const [q, setQ] = useState('')
  const [err, setErr] = useState('')
  const [creating, setCreating] = useState(false)
  const reload = useCallback(() => { listSkills().then(setData).catch(e => setErr(e.message)) }, [])
  useEffect(() => { reload(); listRooms().then(setRooms).catch(() => {}) }, [reload])
  const shown = (data?.skills ?? []).filter(s => !q || (s.id + ' ' + s.description).toLowerCase().includes(q.toLowerCase()))
  const view = skillId === '_packs' ? 'packs' : skillId === '_assign' ? 'assign' : skillId ? 'skill' : 'none'

  return (
    <>
      <aside aria-label={t('skills.title')} className="flex w-64 flex-none flex-col border-r border-line bg-panel">
        <header className="flex items-center justify-between px-3.5 pb-2 pt-3.5">
          <h2 className="text-[13px] font-semibold">{t('skills.title')}</h2>
          <button className="rounded-md px-2 py-1 text-xs text-mute hover:bg-sub hover:text-fg" onClick={() => setCreating(true)}>＋ {t('skills.new')}</button>
        </header>
        <nav className="px-1.5 pb-1">
          {(['_assign', '_packs'] as const).map(k => (
            <NavLink key={k} to={`/skills/${k}`} className={({ isActive }) => `my-px block rounded-md px-3.5 py-1.5 text-[13.5px] ${isActive ? 'bg-sub font-medium' : 'text-mute hover:bg-sub hover:text-fg'}`}>
              {t(k === '_assign' ? 'skills.assign' : 'skills.packs')}
            </NavLink>
          ))}
        </nav>
        <input aria-label={t('skills.filter')} placeholder={t('skills.filter')} value={q} onChange={e => setQ(e.target.value)}
          className="mx-3 mb-2 mt-1 rounded-[7px] border border-line bg-bg px-2.5 py-1.5 text-[13px] outline-none focus:border-accent" />
        <div className="flex-1 overflow-auto pb-2">
          {data === null && <p className="px-4 text-[13px] text-mute">{t('common.loading')}</p>}
          {shown.map(s => (
            <NavLink key={s.id} to={`/skills/${s.id}`} title={s.description}
              className={({ isActive }) => `mx-1.5 my-px block rounded-md px-3.5 py-1.5 ${isActive ? 'bg-sub' : 'hover:bg-sub'}`}>
              <div className="flex items-center gap-2 text-[13.5px]"><span className="truncate font-medium">{s.id}</span>
                {s.used_by.length > 0 && <span className="ml-auto flex-none text-[11px] text-mute">{t('skills.usedN', { n: s.used_by.length })}</span>}</div>
              <div className="truncate text-xs text-mute">{s.description || '—'}</div>
            </NavLink>
          ))}
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-auto">
        {err && <p role="alert" className="m-5 text-danger">{err}</p>}
        {data && view === 'skill' && <SkillEditor key={skillId} id={skillId!} data={data} onChanged={reload} onDeleted={() => { reload(); nav('/skills') }} />}
        {data && view === 'packs' && <Packs data={data} onChanged={reload} />}
        {data && view === 'assign' && <Assign data={data} rooms={rooms} onChanged={reload} />}
        {data && view === 'none' && <div className="grid h-full place-items-center p-8 text-center text-mute"><div><p className="mb-1">{t('skills.pick', { n: data.skills.length })}</p><p className="text-xs">{t('skills.help')}</p></div></div>}
      </main>
      <NewSkill open={creating} onClose={() => setCreating(false)} onCreated={id => { setCreating(false); reload(); nav(`/skills/${id}`) }} />
    </>
  )
}

function SkillEditor({ id, data, onChanged, onDeleted }: { id: string; data: SkillListing; onChanged: () => void; onDeleted: () => void }) {
  const { t } = useI18n()
  const [orig, setOrig] = useState<string | null>(null)
  const [text, setText] = useState('')
  const [note, setNote] = useState('')
  const [usedBy, setUsedBy] = useState<string[]>([])
  const [hist, setHist] = useState<{ versions: SkillVersion[]; log: SkillLog[] } | null>(null)
  const [peek, setPeek] = useState<{ v: string; content: string } | null>(null)
  const [msg, setMsg] = useState(''); const [err, setErr] = useState(''); const [busy, setBusy] = useState(false)
  const load = useCallback(() => {
    getSkill(id).then(d => { setOrig(d.content); setText(d.content); setUsedBy(d.used_by) }).catch(e => setErr(e.message))
    skillHistory(id).then(setHist).catch(() => setHist({ versions: [], log: [] }))
  }, [id])
  useEffect(load, [load])
  const dirty = orig !== null && text !== orig
  useEffect(() => {
    const h = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault() }
    window.addEventListener('beforeunload', h); return () => window.removeEventListener('beforeunload', h)
  }, [dirty])
  const item = data.skills.find(s => s.id === id)
  async function act(fn: () => Promise<unknown>, done: string) {
    setBusy(true); setErr(''); setMsg('')
    try { await fn(); setMsg(done); onChanged(); load() } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  if (err && orig === null) return <p role="alert" className="m-6 text-danger">{err}</p>
  if (orig === null) return <p className="p-6 text-mute">{t('common.loading')}</p>
  return (
    <div className="flex h-full flex-col p-6">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h1 className="text-lg font-semibold">{id}</h1>
        {item?.packs.map(p => <span key={p} className="rounded-full bg-sub px-2 py-0.5 text-xs text-mute">{p}</span>)}
        <div className="ml-auto flex gap-1.5">
          <button className={btnDanger} disabled={busy || usedBy.length > 0} title={usedBy.length ? t('skills.inUse') : ''}
            onClick={() => { if (window.confirm(t('skills.deleteConfirm', { id }))) { setBusy(true); deleteSkill(id).then(onDeleted, e => { setErr(e.message); setBusy(false) }) } }}>{t('common.delete')}</button>
        </div>
      </div>
      <p className="mb-3 text-xs text-mute">{usedBy.length ? t('skills.usedBy', { list: usedBy.join('、') }) : t('skills.unused')}</p>
      <textarea aria-label={t('skills.content')} spellCheck={false} className={`${field} min-h-72 flex-1 resize-none font-mono text-[12.5px] leading-relaxed`} value={text} onChange={e => setText(e.target.value)} />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <input aria-label={t('skills.note')} placeholder={t('skills.note')} className={`${field} max-w-sm`} value={note} maxLength={300} onChange={e => setNote(e.target.value)} />
        {dirty && <button className={btnGhost} onClick={() => setText(orig)}>{t('skills.discard')}</button>}
        <button className={btnPrimary} disabled={busy || !dirty} onClick={() => act(() => saveSkill(id, text, note).then(() => setNote('')), t('skills.saved'))}>{t('common.save')}</button>
        {msg && <span role="status" className="text-[13px] text-ok">{msg}</span>}
        {err && <span role="alert" className="text-[13px] text-danger">{err}</span>}
      </div>
      <details className="mt-5 rounded-xl border border-line bg-panel">
        <summary className="cursor-pointer px-4 py-2.5 text-[13px] font-medium">{t('skills.history')} {hist ? `(${hist.versions.length})` : ''}</summary>
        <div className="border-t border-line px-4 py-3 text-[13px]">
          {!hist?.versions.length ? <p className="text-mute">{t('skills.noHistory')}</p> : (
            <ul className="space-y-1">
              {hist.versions.map(v => (
                  <li key={v.version} className="flex items-center gap-2">
                    <code>{v.version}</code><span className="text-xs text-mute">{v.bytes} B</span>
                    <button className="ml-auto rounded px-2 py-0.5 text-xs text-mute hover:bg-sub hover:text-fg"
                      onClick={() => skillVersion(id, v.version).then(d => setPeek({ v: v.version, content: d.content }))}>{t('skills.view')}</button>
                    <button className="rounded px-2 py-0.5 text-xs text-accent hover:bg-accent-soft" disabled={busy}
                      onClick={() => { if (window.confirm(t('skills.restoreConfirm', { v: v.version }))) act(() => restoreSkill(id, v.version), t('skills.restored')) }}>{t('skills.restore')}</button>
                  </li>
              ))}
            </ul>
          )}
          {hist && hist.log.length > 0 && (
            <ul className="mt-3 border-t border-line pt-2 text-xs text-mute">
              {hist.log.slice(-8).reverse().map((l, i) => <li key={i}>{l.at ? new Date(l.at * 1000).toLocaleString() : ''} · {l.actor} · {l.action}{l.note ? ` — ${l.note}` : ''}</li>)}
            </ul>
          )}
        </div>
      </details>
      <Dialog open={!!peek} onClose={() => setPeek(null)} title={peek ? `${id} @ ${peek.v}` : ''}>
        <pre className="max-h-[60vh] overflow-auto rounded-lg bg-sub p-3 font-mono text-xs whitespace-pre-wrap">{peek?.content}</pre>
        <div className="mt-3 flex justify-end"><button className={btnGhost} onClick={() => setPeek(null)}>{t('common.close')}</button></div>
      </Dialog>
    </div>
  )
}

function NewSkill({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const { t } = useI18n()
  const [id, setId] = useState('')
  const [err, setErr] = useState(''); const [busy, setBusy] = useState(false)
  async function submit() {
    setBusy(true); setErr('')
    try { await createSkill(id, TEMPLATE(id)); setId(''); onCreated(id) } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  return (
    <Dialog open={open} onClose={onClose} title={t('skills.new')}>
      <form className="space-y-3 text-[13px]" onSubmit={e => { e.preventDefault(); submit() }}>
        <label className="block"><span className="mb-1 block text-mute">{t('skills.id')}</span>
          <input className={field} value={id} onChange={e => setId(e.target.value.toLowerCase())} pattern="[a-z0-9][a-z0-9_-]{0,63}" required placeholder="release-checklist" autoFocus /></label>
        <p className="text-xs text-mute">{t('skills.idHelp')}</p>
        {err && <p role="alert" className="text-danger">{err}</p>}
        <div className="flex justify-end gap-2"><button type="button" className={btnGhost} onClick={onClose}>{t('common.cancel')}</button><button className={btnPrimary} disabled={busy || !id}>{t('rooms.create')}</button></div>
      </form>
    </Dialog>
  )
}

function Checks({ all, value, onChange, label }: { all: string[]; value: string[]; onChange: (v: string[]) => void; label: string }) {
  return (
    <fieldset className="max-h-56 overflow-auto rounded-lg border border-line p-1" aria-label={label}>
      {all.length === 0 && <p className="px-2 py-1 text-xs text-mute">—</p>}
      {all.map(s => (
        <label key={s} className="flex items-center gap-2 rounded px-2 py-1 text-[13px] hover:bg-sub">
          <input type="checkbox" checked={value.includes(s)} onChange={e => onChange(e.target.checked ? [...value, s] : value.filter(x => x !== s))} />{s}
        </label>
      ))}
    </fieldset>
  )
}

function Packs({ data, onChanged }: { data: SkillListing; onChanged: () => void }) {
  const { t } = useI18n()
  const names = Object.keys(data.packs)
  const [sel, setSel] = useState<string>(names[0] ?? '')
  const [members, setMembers] = useState<string[]>(data.packs[sel] ?? [])
  const [newName, setNewName] = useState('')
  const [msg, setMsg] = useState(''); const [err, setErr] = useState('')
  useEffect(() => setMembers(data.packs[sel] ?? []), [sel, data])
  const ids = data.skills.map(s => s.id)
  async function act(fn: () => Promise<unknown>, done: string) {
    setErr(''); setMsg('')
    try { await fn(); setMsg(done); onChanged() } catch (e) { setErr((e as Error).message) }
  }
  return (
    <div className="mx-auto max-w-3xl p-6">
      <h1 className="mb-1 text-lg font-semibold">{t('skills.packs')}</h1>
      <p className="mb-5 text-[13px] text-mute">{t('skills.packsHelp')}</p>
      <div className="grid gap-5 sm:grid-cols-[200px_1fr]">
        <div>
          <ul className="mb-2 overflow-hidden rounded-lg border border-line">
            {names.length === 0 && <li className="px-3 py-2 text-[13px] text-mute">{t('skills.noPacks')}</li>}
            {names.map(n => <li key={n}><button onClick={() => setSel(n)} className={`block w-full px-3 py-1.5 text-left text-[13px] ${n === sel ? 'bg-accent-soft text-accent' : 'hover:bg-sub'}`}>{n} <span className="text-xs text-mute">{data.packs[n].length}</span></button></li>)}
          </ul>
          <form className="flex gap-1.5" onSubmit={e => { e.preventDefault(); const n = newName.trim(); if (n) act(() => savePack(n, []), t('skills.packCreated')).then(() => { setSel(n); setNewName('') }) }}>
            <input aria-label={t('skills.newPack')} placeholder={t('skills.newPack')} className={`${field} py-1.5`} value={newName} onChange={e => setNewName(e.target.value)} maxLength={40} />
            <button className={btnGhost}>＋</button>
          </form>
        </div>
        {sel && (
          <div>
            <Checks all={ids} value={members} onChange={setMembers} label={sel} />
            <div className="mt-3 flex gap-2">
              <button className={btnPrimary} onClick={() => act(() => savePack(sel, members), t('skills.saved'))}>{t('common.save')}</button>
              <button className={btnDanger} onClick={() => { if (window.confirm(t('skills.deletePackConfirm', { n: sel }))) act(() => deletePack(sel), t('skills.packDeleted')).then(() => setSel('')) }}>{t('common.delete')}</button>
            </div>
          </div>
        )}
      </div>
      {msg && <p role="status" className="mt-3 text-[13px] text-ok">{msg}</p>}
      {err && <p role="alert" className="mt-3 text-[13px] text-danger">{err}</p>}
    </div>
  )
}

function Assign({ data, rooms, onChanged }: { data: SkillListing; rooms: Room[]; onChanged: () => void }) {
  const { t } = useI18n()
  const roleIds = Object.keys(data.roles).filter(r => data.roles[r].rank !== 'human')
  const [target, setTarget] = useState<string>(roleIds[0] ? `role:${roleIds[0]}` : '')
  const [skills, setSkills] = useState<string[]>([])
  const [packs, setPacks] = useState<string[]>([])
  const [preview, setPreview] = useState<{ persona: string | null; skills: string[] } | null>(null)
  const [previewRoom, setPreviewRoom] = useState<string>('')
  const [msg, setMsg] = useState(''); const [err, setErr] = useState('')
  const [kind, key] = useMemo(() => { const i = target.indexOf(':'); return [target.slice(0, i), target.slice(i + 1)] }, [target])
  useEffect(() => {
    const cur = kind === 'role' ? data.roles[key] : data.rooms[key] ?? { skills: [], packs: [] }
    setSkills(kind === 'role' ? (cur as any)?.skills ?? [] : (cur as any).skills ?? [])
    setPacks(kind === 'role' ? (cur as any)?.skill_packs ?? [] : (cur as any).packs ?? [])
  }, [kind, key, data])
  const previewRole = kind === 'role' ? key : roleIds[0]
  useEffect(() => {
    if (!previewRole) return
    const room = kind === 'room' ? Number(key) : previewRoom ? Number(previewRoom) : undefined
    skillPreview(previewRole, room).then(setPreview).catch(() => setPreview(null))
  }, [previewRole, kind, key, previewRoom, data])
  async function save() {
    setErr(''); setMsg('')
    try { kind === 'role' ? await assignRole(key, skills, packs) : await assignRoom(Number(key), skills, packs); setMsg(t('skills.saved')); onChanged() }
    catch (e) { setErr((e as Error).message) }
  }
  const active = rooms.filter(r => r.state !== 'deleted')
  return (
    <div className="mx-auto max-w-4xl p-6">
      <h1 className="mb-1 text-lg font-semibold">{t('skills.assign')}</h1>
      <p className="mb-5 text-[13px] text-mute">{t('skills.assignHelp')}</p>
      <label className="mb-4 block max-w-sm text-[13px]"><span className="mb-1 block text-mute">{t('skills.target')}</span>
        <select className={field} value={target} onChange={e => setTarget(e.target.value)}>
          <optgroup label={t('agents.roles')}>{roleIds.map(r => <option key={r} value={`role:${r}`}>{r}</option>)}</optgroup>
          <optgroup label={t('rooms.title')}>{active.map(r => <option key={r.id} value={`room:${r.id}`}>#{r.id} {r.name}</option>)}</optgroup>
        </select></label>
      <div className="grid gap-4 sm:grid-cols-2">
        <div><h3 className="mb-1.5 text-xs font-medium uppercase tracking-wider text-mute">{t('skills.title')}</h3><Checks all={data.skills.map(s => s.id)} value={skills} onChange={setSkills} label={t('skills.title')} /></div>
        <div><h3 className="mb-1.5 text-xs font-medium uppercase tracking-wider text-mute">{t('skills.packs')}</h3><Checks all={Object.keys(data.packs)} value={packs} onChange={setPacks} label={t('skills.packs')} /></div>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button className={btnPrimary} disabled={!target} onClick={save}>{t('common.save')}</button>
        {msg && <span role="status" className="text-[13px] text-ok">{msg}</span>}
        {err && <span role="alert" className="text-[13px] text-danger">{err}</span>}
      </div>
      {preview && (
        <div className="mt-6 rounded-xl border border-line bg-panel p-4 text-[13px]">
          <div className="mb-2 flex flex-wrap items-center gap-2"><h3 className="font-medium">{t('skills.preview', { role: previewRole })}</h3>
            {kind === 'role' && <select aria-label={t('skills.previewRoom')} className={`${field} ml-auto w-auto py-1 text-xs`} value={previewRoom} onChange={e => setPreviewRoom(e.target.value)}>
              <option value="">{t('skills.noRoom')}</option>{active.map(r => <option key={r.id} value={r.id}>#{r.id} {r.name}</option>)}</select>}</div>
          <p className="text-xs text-mute">{t('skills.persona')}: {preview.persona ?? '—'}</p>
          <p className="mt-1">{preview.skills.length ? preview.skills.join(' · ') : <span className="text-mute">{t('skills.none')}</span>}</p>
        </div>
      )}
    </div>
  )
}
