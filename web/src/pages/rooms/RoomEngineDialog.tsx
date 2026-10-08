import { useEffect, useState } from 'react'
import { agentCatalog, agentModels, setRoomEngine, type CatalogAgent, type Models, type RoomEngine } from '../../api'
import Dialog from '../../components/Dialog'
import ModelSelect from '../../components/ModelSelect'
import { useI18n } from '../../i18n'

const field = 'w-full rounded-lg border border-line bg-bg px-2.5 py-1.5 outline-none focus:border-accent'

/** Switch a role's agent/model for this room only (SPEC-1.1 §6). Owner only; the server test-runs a model before saving. */
export default function RoomEngineDialog({ room, engine, label, onClose, onSaved }: {
  room: number; engine: RoomEngine; label: string; onClose: () => void; onSaved: () => void
}) {
  const { t } = useI18n()
  const [agents, setAgents] = useState<CatalogAgent[]>([])
  const [agent, setAgent] = useState(engine.acp_agent || engine.default_agent || '')
  // Start from what this room actually uses (room override, else the role's model), never the agent's own default:
  // otherwise pressing Apply would silently move the room to a different (possibly paid) model.
  const [model, setModel] = useState(engine.model || '')
  const [models, setModels] = useState<Models | null>(null)
  const [loadingModels, setLoadingModels] = useState(false)
  const [msg, setMsg] = useState(''); const [busy, setBusy] = useState(false)
  useEffect(() => { agentCatalog().then(c => setAgents(c.agents.filter(a => a.installed))).catch(() => {}) }, [])
  useEffect(() => {
    if (!agent) return
    setLoadingModels(true); setModels(null)
    agentModels(agent).then(setModels).catch(() => setModels(null)).finally(() => setLoadingModels(false))
  }, [agent])

  async function apply(a: string, m: string) {
    setBusy(true); setMsg('')
    try { await setRoomEngine(room, engine.role, a, m); onSaved(); onClose() }
    catch (e) { setMsg((e as Error).message) } finally { setBusy(false) }
  }
  const def = [engine.default_agent, engine.default_model].filter(Boolean).join(' · ') || '—'
  return (
    <Dialog open title={t('engine.title', { name: label })} onClose={onClose}>
      {!engine.per_room ? <p className="text-[13px] text-mute">{t('engine.notPerRoom')}</p> : (
        <div className="grid gap-3 text-[13px]">
          <p className="text-mute">{t('engine.help')}</p>
          <label><span className="mb-1 block text-mute">{t('agents.agent')}</span>
            <select className={field} value={agent} onChange={e => {
              const a = e.target.value; setAgent(a)
              setModel(a === (engine.acp_agent || engine.default_agent) ? engine.model || '' : a === engine.default_agent ? engine.default_model || '' : '')
            }}>
              {!agents.some(a => a.id === agent) && agent && <option value={agent}>{agent}</option>}
              {agents.map(a => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select></label>
          <label htmlFor={`room-model-${engine.role}`}><span className="mb-1 block text-mute">{t('agents.model')}</span></label>
          <ModelSelect id={`room-model-${engine.role}`} className={field} models={models} value={model} onChange={setModel} loading={loadingModels} />
          <p className="text-xs text-mute">{t('engine.default', { d: def })}</p>
          {msg && <p role="alert" className="text-danger">{msg}</p>}
          <div className="flex justify-end gap-2">
            {engine.overridden && <button className="rounded-lg border border-line px-3 py-1.5 hover:bg-sub" disabled={busy} onClick={() => apply('', '')}>{t('engine.reset')}</button>}
            <button className="rounded-lg bg-accent px-3 py-1.5 font-medium text-white disabled:opacity-60" disabled={busy || !agent}
              onClick={() => {
                const sameAsDefault = agent === engine.default_agent && model.trim() === (engine.default_model || '')
                return sameAsDefault ? apply('', '') : apply(agent === engine.default_agent ? '' : agent, model.trim())
              }}>{busy ? t('agents.checking') : t('engine.apply')}</button>
          </div>
        </div>
      )}
    </Dialog>
  )
}
