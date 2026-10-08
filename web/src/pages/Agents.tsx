import { useCallback, useEffect, useRef, useState } from 'react'
import { NavLink, useParams } from 'react-router-dom'
import {
  accountQuota, addRole, agentCatalog, agentModels, closeSession, installAgent, installStatus, listAgents, probeAgent,
  roleSessions, setEngine, type Agent, type Catalog, type CatalogAgent, type Models, type Quota, type Session,
} from '../api'
import ModelSelect from '../components/ModelSelect'
import Dialog, { btnGhost, btnPrimary, field } from '../components/Dialog'
import { colorFor, timeOf } from '../lib/format'
import { useI18n } from '../i18n'
import { usePoll } from './rooms/shared'

export default function Agents() {
  const { t } = useI18n()
  const { roleId } = useParams()
  const [cat, setCat] = useState<Catalog | null>(null)
  const [live, setLive] = useState<Agent[]>([])
  const [err, setErr] = useState('')
  const [adding, setAdding] = useState(false)
  const reload = useCallback(() => { agentCatalog().then(setCat).catch(e => setErr(e.message)) }, [])
  useEffect(reload, [reload])
  usePoll(() => { listAgents().then(setLive).catch(() => {}) }, 9000, [])
  const roles = (cat?.roles ?? []).filter(r => r.agent !== 'manual')
  const sel = roleId ?? null

  return (
    <>
      <aside aria-label={t('agents.title')} className="flex w-60 flex-none flex-col border-r border-line bg-panel">
        <header className="flex items-center justify-between px-3.5 pb-2 pt-3.5">
          <h2 className="text-[13px] font-semibold">{t('agents.title')}</h2>
          <button className="rounded-md px-2 py-1 text-xs text-mute hover:bg-sub hover:text-fg" onClick={() => setAdding(true)}>＋ {t('agents.addRole')}</button>
        </header>
        <NavLink end to="/agents" className={({ isActive }) => `mx-1.5 my-px rounded-md px-3.5 py-1.5 text-[13.5px] ${isActive ? 'bg-sub font-medium' : 'text-mute hover:bg-sub hover:text-fg'}`}>
          {t('agents.engines')}
        </NavLink>
        <div className="px-4 pb-1 pt-3 text-[11px] font-medium uppercase tracking-wider text-mute">{t('agents.roles')}</div>
        <div className="flex-1 overflow-auto pb-2">
          {roles.map(r => {
            const a = live.find(x => x.id === r.id)
            return (
              <NavLink key={r.id} to={`/agents/${r.id}`}
                className={({ isActive }) => `mx-1.5 my-px flex items-center gap-2 rounded-md px-3.5 py-1.5 text-[13.5px] ${isActive ? 'bg-sub font-medium text-fg' : 'text-mute hover:bg-sub hover:text-fg'}`}>
                <span className={`size-2 flex-none rounded-full ${a?.online ? 'bg-ok' : 'bg-off'}`} />
                <span className="truncate">{a?.label || r.id}</span>
                <span className="ml-auto text-[11px] text-mute">{r.agent}</span>
              </NavLink>
            )
          })}
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-auto">
        {err && <p role="alert" className="m-5 text-danger">{err}</p>}
        {!cat ? <p className="p-6 text-mute">{t('common.loading')}</p>
          : sel ? <RoleDetail key={sel} role={sel} cat={cat} live={live.find(x => x.id === sel)} onChanged={reload} />
          : <Engines cat={cat} onChanged={reload} />}
      </main>
      {cat && <AddRole open={adding} onClose={() => setAdding(false)} cat={cat} onAdded={reload} />}
    </>
  )
}

function Section({ title, children, action }: { title: string; children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <section className="mb-6">
      <div className="mb-2.5 flex items-center"><h2 className="text-[13px] font-semibold">{title}</h2><div className="ml-auto">{action}</div></div>
      {children}
    </section>
  )
}

const pill = (ok: boolean | null, yes: string, no: string, unknown: string) =>
  <span className={`rounded-full px-2 py-0.5 text-[11.5px] ${ok === true ? 'bg-ok/12 text-ok' : ok === false ? 'bg-warn/12 text-warn' : 'bg-sub text-mute'}`}>{ok === true ? yes : ok === false ? no : unknown}</span>

function Engines({ cat, onChanged }: { cat: Catalog; onChanged: () => void }) {
  const { t } = useI18n()
  const [quota, setQuota] = useState<Quota[] | null>(null)
  useEffect(() => { accountQuota().then(d => setQuota(d.accounts)).catch(() => setQuota([])) }, [])
  return (
    <div className="mx-auto max-w-4xl p-6">
      <h1 className="mb-1 text-lg font-semibold">{t('agents.engines')}</h1>
      <p className="mb-5 text-[13px] text-mute">{t('agents.enginesHelp')}</p>
      <Section title={t('agents.installed')}>
        <div className="overflow-hidden rounded-xl border border-line bg-panel">
          {cat.agents.map(a => <EngineRow key={a.id} a={a} usedBy={cat.roles.filter(r => r.agent === a.id).map(r => r.id)} onChanged={onChanged} />)}
        </div>
      </Section>
      <Section title={t('agents.quota')}>
        {quota === null ? <p className="text-[13px] text-mute">{t('common.loading')}</p> : quota.length === 0 ? <p className="text-[13px] text-mute">{t('agents.noQuota')}</p> : (
          <div className="grid gap-3 sm:grid-cols-2">
            {quota.map(q => (
              <div key={q.agent} className="rounded-xl border border-line bg-panel p-3.5 text-[13px]">
                <div className="mb-1.5 flex items-center gap-2"><b className="font-medium">{q.engine}</b>{q.in_use && <span className="text-xs text-mute">· {t('agents.inUse')}</span>}{q.plan && <span className="ml-auto text-xs text-mute">{q.plan}</span>}</div>
                {q.windows.map(w => (
                  <div key={w.label} className="mb-1.5">
                    <div className="flex justify-between text-xs text-mute"><span>{w.label}</span><span>{w.unlimited ? '∞' : w.remaining_percent != null ? t('agents.left', { n: w.remaining_percent }) : '—'}</span></div>
                    {!w.unlimited && w.remaining_percent != null && <div className="mt-1 h-1 overflow-hidden rounded bg-sub"><i className={`block h-full ${w.remaining_percent < 15 ? 'bg-danger' : 'bg-accent'}`} style={{ width: `${w.remaining_percent}%` }} /></div>}
                  </div>
                ))}
                {(q.note || q.error) && <p className={`text-xs ${q.error ? 'text-danger' : 'text-mute'}`}>{q.error || q.note}</p>}
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}

function EngineRow({ a, usedBy, onChanged }: { a: CatalogAgent; usedBy: string[]; onChanged: () => void }) {
  const { t } = useI18n()
  const [job, setJob] = useState<{ id: string; state: string; out: string } | null>(null)
  const [probe, setProbe] = useState<string>('')
  const [busy, setBusy] = useState(false)
  const offset = useRef(0)
  useEffect(() => {
    if (!job || job.state !== 'running') return
    const id = setInterval(async () => {
      try {
        const s = await installStatus(job.id, offset.current)
        offset.current = s.offset ?? offset.current
        setJob(j => j && { ...j, state: s.state, out: j.out + (s.output ?? '') })
        if (s.state !== 'running') onChanged()
      } catch { /* retry next tick */ }
    }, 1500)
    return () => clearInterval(id)
  }, [job, onChanged])
  async function install() {
    if (!window.confirm(t('agents.installConfirm', { agent: a.label, cmd: (a.install_cmd ?? []).join(' ') }))) return
    setBusy(true)
    try { const j = await installAgent(a.id); offset.current = 0; setJob({ id: j.id, state: j.state, out: '' }) }
    catch (e) { setProbe((e as Error).message) } finally { setBusy(false) }
  }
  async function test() {
    setBusy(true); setProbe(t('agents.testing'))
    try { const r = await probeAgent(a.id); setProbe(r.ok ? `✓ ${t('agents.probeOk')}` : `✕ ${r.error ?? ''}`) }
    catch (e) { setProbe(`✕ ${(e as Error).message}`) } finally { setBusy(false) }
  }
  return (
    <div className="border-b border-line px-4 py-3 last:border-0">
      <div className="flex flex-wrap items-center gap-2.5">
        <b className="font-medium">{a.label}</b><span className="text-xs text-mute">{a.id}</span>
        {pill(a.installed, t('agents.isInstalled'), t('agents.notInstalled'), '')}
        {a.installed && pill(a.logged_in, t('agents.loggedIn'), t('agents.notLoggedIn'), t('agents.loginUnknown'))}
        {usedBy.length > 0 && <span className="text-xs text-mute">{t('agents.usedBy', { roles: usedBy.join(', ') })}</span>}
        <div className="ml-auto flex gap-1.5">
          {a.installed && <button className={btnGhost} disabled={busy} onClick={test}>{t('agents.test')}</button>}
          {!a.installed && a.install_cmd && <button className={btnPrimary} disabled={busy || job?.state === 'running'} onClick={install}>{t('agents.install')}</button>}
        </div>
      </div>
      {a.login_detail && a.installed && <p className="mt-1 text-xs text-mute">{a.login_detail}</p>}
      {!a.installed && a.needs && <p className="mt-1 text-xs text-mute">{a.needs}</p>}
      {probe && <p className="mt-1 text-xs">{probe}</p>}
      {job && <pre aria-label={t('agents.installLog')} className="mt-2 max-h-48 overflow-auto rounded-lg bg-sub p-2.5 font-mono text-[11.5px] whitespace-pre-wrap">{job.out || '…'}{job.state !== 'running' && `\n[${job.state}]`}</pre>}
    </div>
  )
}

function RoleDetail({ role, cat, live, onChanged }: { role: string; cat: Catalog; live?: Agent; onChanged: () => void }) {
  const { t, lang } = useI18n()
  const r = cat.roles.find(x => x.id === role)
  const [sessions, setSessions] = useState<Session[] | null>(null)
  const [agent, setAgent] = useState(live?.acp_agent || r?.agent || '')
  const [model, setModel] = useState(live?.model || '')
  const [models, setModels] = useState<Models | null>(null)
  const [msg, setMsg] = useState(''); const [busy, setBusy] = useState(false)
  const loadSessions = useCallback(() => { roleSessions(role).then(d => setSessions(d.sessions)).catch(e => setMsg(e.message)) }, [role])
  usePoll(loadSessions, 5000, [loadSessions])
  useEffect(() => { if (agent) agentModels(agent).then(setModels).catch(() => setModels(null)) }, [agent])
  if (!r) return <p className="p-6 text-mute">{t('agents.noRole')}</p>
  const installed = cat.agents.filter(a => a.installed)
  async function save() {
    setBusy(true); setMsg('')
    try { const d = await setEngine(role, agent, model.trim()); setMsg(d.changed ? t('agents.engineSaved') : t('agents.noChange')); onChanged() }
    catch (e) { setMsg((e as Error).message) } finally { setBusy(false) }
  }
  async function close(room: number) {
    if (!window.confirm(t('agents.closeConfirm', { room }))) return
    try { await closeSession(role, room); loadSessions() } catch (e) { setMsg((e as Error).message) }
  }
  const stateCls: Record<string, string> = { busy: 'bg-ok/12 text-ok', open: 'bg-accent-soft text-accent', parked: 'bg-sub text-mute', closed: 'bg-sub text-mute' }
  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-5 flex items-center gap-3">
        <div className="grid size-10 place-items-center rounded-xl text-base font-semibold text-white" style={{ background: colorFor(role) }}>{(live?.label || role).slice(0, 1).toUpperCase()}</div>
        <div><h1 className="text-lg font-semibold">{live?.label || role}</h1><p className="text-[13px] text-mute">{role} · {r.rank === 'lead' ? t('agents.rankLead') : t('agents.rankWorker')} · {live?.online ? t('agent.online') : t('agent.offline')}</p></div>
      </div>
      <Section title={t('agents.engine')}>
        {!cat.agents.some(x => x.id === r.agent) ? <p className="rounded-xl border border-line bg-panel p-4 text-[13px] text-mute">{t('agents.notAcp', { driver: r.agent ?? '?' })}</p> : <div className="grid gap-3 rounded-xl border border-line bg-panel p-4 text-[13px] sm:grid-cols-[1fr_1fr_auto] sm:items-end">
          <label><span className="mb-1 block text-mute">{t('agents.agent')}</span>
            <select className={field} value={agent} onChange={e => { setAgent(e.target.value); setModel('') }}>
              {!installed.some(a => a.id === agent) && agent && <option value={agent}>{agent} ({t('agents.notInstalled')})</option>}
              {installed.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select></label>
          <div><label htmlFor={`model-${role}`} className="mb-1 block text-mute">{t('agents.model')}</label>
            <ModelSelect id={`model-${role}`} className={field} models={models} value={model} onChange={setModel} /></div>
          <button className={btnPrimary} disabled={busy || !agent} onClick={save}>{busy ? t('agents.checking') : t('common.save')}</button>
          {models?.source && <p className="text-xs text-mute sm:col-span-3">{t('agents.modelSource', { s: models.source })}</p>}
          <p className="text-xs text-mute sm:col-span-3">{t('agents.engineHelp')}</p>
          {msg && <p role="status" className="sm:col-span-3">{msg}</p>}
        </div>}
      </Section>
      <Section title={t('agents.sessions')} action={<span className="text-xs text-mute">{t('agents.sessionsHelp')}</span>}>
        {sessions === null ? <p className="text-[13px] text-mute">{t('common.loading')}</p> : sessions.length === 0 ? <p className="text-[13px] text-mute">{t('agents.noSessions')}</p> : (
          <div className="overflow-hidden rounded-xl border border-line bg-panel text-[13px]">
            <table className="w-full">
              <thead className="bg-sub text-left text-xs text-mute"><tr>
                <th className="px-3 py-2 font-medium">{t('agents.room')}</th><th className="px-3 py-2 font-medium">{t('agents.state')}</th>
                <th className="px-3 py-2 font-medium">{t('agent.context')}</th><th className="px-3 py-2 font-medium">{t('agents.lastActive')}</th><th /></tr></thead>
              <tbody>
                {sessions.map(s => (
                  <tr key={s.room ?? 'default'} className="border-t border-line">
                    <td className="px-3 py-2">{s.room == null ? <span className="text-mute">{t('agents.defaultSession')}</span> : <>#{s.room} {s.room_name}</>}</td>
                    <td className="px-3 py-2"><span className={`rounded-full px-2 py-0.5 text-[11.5px] ${stateCls[s.state]}`}>{t(`agents.s.${s.state}`)}</span>{s.recall_pending && <span className="ml-1.5 text-xs text-warn">{t('agents.recall')}</span>}</td>
                    <td className="px-3 py-2">{s.context_percent == null ? '—' : (
                      <div className="flex items-center gap-2"><div className="h-1 w-20 overflow-hidden rounded bg-sub"><i className={`block h-full ${s.context_percent > 80 ? 'bg-warn' : 'bg-accent'}`} style={{ width: `${s.context_percent}%` }} /></div><span className="text-xs text-mute">{s.context_percent}%</span></div>)}</td>
                    <td className="px-3 py-2 text-xs text-mute">{s.last_activity ? timeOf(s.last_activity, lang) : '—'}</td>
                    <td className="px-3 py-2 text-right">{s.room != null && s.state === 'open' &&
                      <button className="rounded px-2 py-0.5 text-xs text-mute hover:bg-sub hover:text-fg" onClick={() => close(s.room!)}>{t('agents.close')}</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </div>
  )
}

function AddRole({ open, onClose, cat, onAdded }: { open: boolean; onClose: () => void; cat: Catalog; onAdded: () => void }) {
  const { t } = useI18n()
  const installed = cat.agents.filter(a => a.installed)
  const [f, setF] = useState({ role: '', label: '', agent: installed[0]?.id ?? '', rank: 'worker' as 'lead' | 'worker', mode: 'skill' as 'skill' | 'persona', skill: '', persona: '', model: '' })
  const [err, setErr] = useState(''); const [ok, setOk] = useState(''); const [busy, setBusy] = useState(false)
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF(x => ({ ...x, [k]: e.target.value }))
  const [newModels, setNewModels] = useState<Models | null>(null)
  useEffect(() => { if (open && f.agent) agentModels(f.agent).then(setNewModels).catch(() => setNewModels(null)) }, [open, f.agent])
  async function submit() {
    setBusy(true); setErr(''); setOk('')
    try {
      const r = await addRole({ role: f.role, label: f.label, agent: f.agent, rank: f.rank, model: f.model,
        ...(f.mode === 'skill' ? { skill: f.skill } : { persona: f.persona }) })
      setOk(r.message); onAdded()
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }
  return (
    <Dialog open={open} onClose={onClose} title={t('agents.addRole')}>
      <form className="space-y-3 text-[13px]" onSubmit={e => { e.preventDefault(); submit() }}>
        <div className="grid grid-cols-2 gap-3">
          <label><span className="mb-1 block text-mute">{t('agents.roleId')}</span><input className={field} value={f.role} onChange={set('role')} pattern="[a-z][a-z0-9_-]{1,31}" required placeholder="writer" /></label>
          <label><span className="mb-1 block text-mute">{t('agents.label')}</span><input className={field} value={f.label} onChange={set('label')} maxLength={40} placeholder="Writer" /></label>
          <label><span className="mb-1 block text-mute">{t('agents.agent')}</span>
            <select className={field} value={f.agent} onChange={e => setF(x => ({ ...x, agent: e.target.value, model: '' }))} required>{installed.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}</select></label>
          <label><span className="mb-1 block text-mute">{t('agents.rank')}</span>
            <select className={field} value={f.rank} onChange={set('rank')}><option value="worker">{t('agents.rankWorker')}</option><option value="lead">{t('agents.rankLead')}</option></select></label>
        </div>
        <fieldset><legend className="mb-1 text-mute">{t('agents.persona')}</legend>
          <div className="mb-2 flex gap-4">
            <label className="flex items-center gap-1.5"><input type="radio" checked={f.mode === 'skill'} onChange={() => setF(x => ({ ...x, mode: 'skill' }))} />{t('agents.useSkill')}</label>
            <label className="flex items-center gap-1.5"><input type="radio" checked={f.mode === 'persona'} onChange={() => setF(x => ({ ...x, mode: 'persona' }))} />{t('agents.writePersona')}</label>
          </div>
          {f.mode === 'skill'
            ? <select aria-label={t('agents.useSkill')} className={field} value={f.skill} onChange={set('skill')} required>
                <option value="">—</option>{cat.skills.map(s => <option key={s.id} value={s.id}>{s.id}{s.description ? ` — ${s.description.slice(0, 60)}` : ''}</option>)}</select>
            : <textarea aria-label={t('agents.writePersona')} className={`${field} h-32 font-mono text-xs`} value={f.persona} onChange={set('persona')} required />}
        </fieldset>
        <div><label htmlFor="new-role-model" className="mb-1 block text-mute">{t('agents.model')}</label>
          <ModelSelect id="new-role-model" className={field} models={newModels} value={f.model} onChange={v => setF(x => ({ ...x, model: v }))} /></div>
        {err && <p role="alert" className="text-danger">{err}</p>}
        {ok && <p role="status" className="text-ok">{ok}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className={btnGhost} onClick={onClose}>{ok ? t('common.close') : t('common.cancel')}</button>
          {!ok && <button className={btnPrimary} disabled={busy || !installed.length}>{t('agents.create')}</button>}
        </div>
        {!installed.length && <p className="text-xs text-warn">{t('agents.installFirst')}</p>}
      </form>
    </Dialog>
  )
}
