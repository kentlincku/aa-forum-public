import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'
import { getConfig, listAgents, listRooms, recentRuns, type Agent, type Config, type Room, type Run } from '../api'
import { colorFor, timeOf } from '../lib/format'
import { useI18n } from '../i18n'
import { usePoll } from './rooms/shared'

export default function Dashboard() {
  const { t, lang } = useI18n()
  const [agents, setAgents] = useState<Agent[] | null>(null)
  const [rooms, setRooms] = useState<Room[] | null>(null)
  const [runs, setRuns] = useState<Run[] | null>(null)
  const [cfg, setCfg] = useState<Config | null>(null)
  const [runErr, setRunErr] = useState('')
  const load = useCallback(() => {
    listAgents().then(setAgents).catch(() => {})
    listRooms().then(setRooms).catch(() => {})
    recentRuns(30).then(d => { setRuns(d.runs); setRunErr('') }).catch(e => setRunErr(e.message))
    getConfig().then(setCfg).catch(() => {})
  }, [])
  usePoll(load, 10000, [load])

  const team = (agents ?? []).filter(a => a.id !== 'Owner')
  const active = (rooms ?? []).filter(r => r.state === 'active')
  const unread = active.reduce((n, r) => n + r.unread, 0)
  const failed = (runs ?? []).filter(r => r.ended && (r.exit ?? 0) !== 0)
  const running = (runs ?? []).filter(r => !r.ended)
  const alerts: { text: string; to: string }[] = [
    ...failed.slice(0, 5).map(r => ({ text: t('dash.runFailed', { role: r.role, err: (r.error || `exit ${r.exit}`).slice(0, 80) }), to: `/agents/${r.role}` })),
    ...team.filter(a => !a.online).map(a => ({ text: t('dash.offline', { role: a.label || a.id }), to: `/agents/${a.id}` })),
    ...active.filter(r => r.unread > 0).map(r => ({ text: t('dash.unread', { n: r.unread, room: r.name }), to: `/rooms/${r.id}` })),
  ]
  const fmt = (s: number | null) => s == null ? '—' : timeOf(s, lang)
  const dur = (r: Run) => r.ended ? `${Math.max(0, Math.round(r.ended - r.started))}s` : '…'

  const Stat = ({ k, v, to }: { k: string; v: string | number; to?: string }) => {
    const body = <><div className="text-2xl font-semibold tabular-nums">{v}</div><div className="text-xs text-mute">{k}</div></>
    return to ? <Link to={to} className="rounded-xl border border-line bg-panel p-4 hover:border-accent">{body}</Link> : <div className="rounded-xl border border-line bg-panel p-4">{body}</div>
  }
  return (
    <main className="min-w-0 flex-1 overflow-auto">
      <div className="mx-auto max-w-5xl p-6">
        <div className="mb-5 flex items-baseline gap-3"><h1 className="text-lg font-semibold">{t('nav.dashboard')}</h1>{cfg?.version && <span className="text-xs text-mute">AA Forum {cfg.version}</span>}</div>
        <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat k={t('dash.agentsOnline')} v={agents ? `${team.filter(a => a.online).length}/${team.length}` : '…'} to="/agents" />
          <Stat k={t('dash.activeRooms')} v={rooms ? active.length : '…'} to="/rooms" />
          <Stat k={t('dash.unreadTotal')} v={rooms ? unread : '…'} to="/rooms" />
          <Stat k={t('dash.running')} v={runs ? running.length : '…'} />
        </div>
        <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
          <section>
            <h2 className="mb-2.5 text-[13px] font-semibold">{t('dash.recentRuns')}</h2>
            {runErr ? <p role="alert" className="text-[13px] text-danger">{runErr}</p> : !runs ? <p className="text-[13px] text-mute">{t('common.loading')}</p> : runs.length === 0 ? <p className="text-[13px] text-mute">{t('dash.noRuns')}</p> : (
              <div className="overflow-hidden rounded-xl border border-line bg-panel">
                <table className="w-full text-[13px]">
                  <thead className="bg-sub text-left text-xs text-mute"><tr>
                    <th className="px-3 py-2 font-medium">{t('dash.role')}</th><th className="px-3 py-2 font-medium">{t('dash.started')}</th>
                    <th className="px-3 py-2 text-right font-medium">{t('dash.duration')}</th><th className="px-3 py-2 text-right font-medium">{t('dash.msgs')}</th><th className="px-3 py-2 font-medium">{t('dash.result')}</th></tr></thead>
                  <tbody>
                    {runs.map((r, i) => (
                      <tr key={i} className="border-t border-line">
                        <td className="px-3 py-1.5"><Link to={`/agents/${r.role}`} className="flex items-center gap-2 hover:text-accent"><span className="size-2 rounded-full" style={{ background: colorFor(r.role) }} />{r.role}</Link></td>
                        <td className="px-3 py-1.5 text-mute">{fmt(r.started)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums text-mute">{dur(r)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{r.msgs_handled}</td>
                        <td className="px-3 py-1.5">{!r.ended ? <span className="text-accent">{t('dash.inProgress')}</span> : (r.exit ?? 0) === 0 ? <span className="text-ok">✓</span>
                          : <span className="text-danger" title={r.error ?? ''}>✕ {(r.error || `exit ${r.exit}`).slice(0, 40)}</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
          <section>
            <h2 className="mb-2.5 text-[13px] font-semibold">{t('dash.attention')}</h2>
            {alerts.length === 0 ? <p className="rounded-xl border border-line bg-panel p-4 text-[13px] text-mute">{t('dash.allGood')}</p> : (
              <ul className="overflow-hidden rounded-xl border border-line bg-panel text-[13px]">
                {alerts.map((a, i) => <li key={i} className="border-b border-line last:border-0"><Link to={a.to} className="block px-3.5 py-2 hover:bg-sub">{a.text}</Link></li>)}
              </ul>
            )}
            <h2 className="mb-2.5 mt-6 text-[13px] font-semibold">{t('dash.team')}</h2>
            <ul className="overflow-hidden rounded-xl border border-line bg-panel text-[13px]">
              {team.map(a => (
                <li key={a.id} className="border-b border-line last:border-0"><Link to={`/agents/${a.id}`} className="flex items-center gap-2.5 px-3.5 py-2 hover:bg-sub">
                  <span className={`size-2 rounded-full ${a.online ? 'bg-ok' : 'bg-off'}`} /><span>{a.label || a.id}</span>
                  <span className="ml-auto truncate text-xs text-mute">{[a.acp_agent, a.model].filter(Boolean).join(' · ')}</span></Link></li>
              ))}
            </ul>
          </section>
        </div>
      </div>
    </main>
  )
}
