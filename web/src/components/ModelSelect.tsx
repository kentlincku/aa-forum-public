import { useEffect, useState } from 'react'
import type { Models } from '../api'
import { useI18n } from '../i18n'

const CUSTOM = '__custom__'

/** Model picker: a real dropdown of the agent's models (deduplicated), "agent default", and "Custom…" for anything else.
 *  value '' means "use the agent's default". */
export default function ModelSelect({ id, models, value, onChange, className, loading }: {
  id: string; models: Models | null; value: string; onChange: (v: string) => void; className: string; loading?: boolean
}) {
  const { t } = useI18n()
  const list = [...new Map((models?.models ?? []).map(m => [m.id, m])).values()]
  const known = value === '' || list.some(m => m.id === value)
  const [custom, setCustom] = useState(!known)
  useEffect(() => { if (!known) setCustom(true) }, [known])
  const sel = custom ? CUSTOM : value
  return (
    <div className="grid gap-1.5">
      <select id={id} className={className} value={sel} disabled={loading}
        onChange={e => { const v = e.target.value; if (v === CUSTOM) { setCustom(true) } else { setCustom(false); onChange(v) } }}>
        <option value="">{models?.default ? t('agents.defaultModel', { m: models.default }) : t('agents.agentDefault')}</option>
        {list.map(m => <option key={m.id} value={m.id}>{m.name && m.name !== m.id ? `${m.name} (${m.id})` : m.id}</option>)}
        <option value={CUSTOM}>{t('model.custom')}</option>
      </select>
      {custom && <input aria-label={t('model.customId')} className={className} placeholder={t('model.customId')} value={value}
        onChange={e => onChange(e.target.value)} autoFocus />}
      {loading && <span className="text-xs text-mute">{t('model.loading')}</span>}
    </div>
  )
}
