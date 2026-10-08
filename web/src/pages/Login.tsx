import { useEffect, useState, type FormEvent } from 'react'
import { ApiError, api, login, token } from '../api'
import { useI18n } from '../i18n'

type Mode = 'signin' | 'setup' | 'reset'

export default function Login({ onSignedIn }: { onSignedIn: () => void }) {
  const { t, lang, setLang } = useI18n()
  const [mode, setMode] = useState<Mode>('signin')
  const [osAuth, setOsAuth] = useState(false)
  const [username, setU] = useState('')
  const [password, setP] = useState('')
  const [password2, setP2] = useState('')
  const [passcode, setPc] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api<{ has_accounts: boolean; auth?: string }>('/api/setup-state')
      .then(s => { setOsAuth(s.auth === 'os'); if (!s.has_accounts) setMode('setup') })
      .catch(() => {})
  }, [])

  async function signIn(u: string, p: string) {
    const r = await login(u, p); token.set(r.token); onSignedIn()
  }
  async function submit(e: FormEvent) {
    e.preventDefault(); setErr('')
    if (mode !== 'signin' && password !== password2) return setErr(t('login.mismatch'))
    setBusy(true)
    try {
      if (mode === 'signin') await signIn(username, password)
      else {
        await api('/setup', { method: 'POST', body: JSON.stringify({ username, password, ...(mode === 'reset' ? { passcode } : {}) }) })
        await signIn(username, password)
      }
    } catch (x) {
      const status = x instanceof ApiError ? x.status : 0
      setErr(mode === 'signin' && status === 401 ? (osAuth ? t('login.failedOs') : t('login.failed')) : (x as Error).message)
    } finally { setBusy(false) }
  }
  const input = 'w-full rounded-lg border border-line bg-bg px-3 py-2 outline-none focus:border-accent focus:ring-3 focus:ring-accent-soft'
  const title = mode === 'setup' ? t('login.setupTitle') : mode === 'reset' ? t('login.resetTitle') : t('login.title')
  return (
    <div className="grid h-full place-items-center bg-bg p-6">
      <form onSubmit={submit} className="w-full max-w-sm rounded-xl border border-line bg-panel p-7 shadow-sm">
        <div className="mb-5 flex items-center gap-2.5">
          <div className="grid size-8 place-items-center rounded-lg bg-accent text-xs font-bold text-white">AA</div>
          <h1 className="text-[15px] font-semibold">{title}</h1>
          <button type="button" className="ml-auto rounded px-2 py-1 text-xs text-mute hover:bg-sub" onClick={() => setLang(lang === 'en' ? 'zh-TW' : 'en')}>{t('nav.language')}</button>
        </div>
        {mode === 'setup' && <p className="mb-4 text-[13px] text-mute">{t('login.setupHelp')}</p>}
        {mode === 'signin' && osAuth && <p className="mb-4 text-[13px] text-mute">{t('login.osHelp')}</p>}
        {mode === 'reset' && (
          <label className="mb-3 block text-[13px]"><span className="mb-1 block text-mute">{t('login.passcode')}</span>
            <input className={input} type="password" autoComplete="off" value={passcode} onChange={e => setPc(e.target.value)} required />
            <span className="mt-1 block text-xs text-mute">{t('login.passcodeHelp')}</span></label>
        )}
        <label className="mb-3 block text-[13px]"><span className="mb-1 block text-mute">{t('login.username')}</span>
          <input className={input} autoComplete="username" value={username} onChange={e => setU(e.target.value)} required /></label>
        <label className="mb-3 block text-[13px]"><span className="mb-1 block text-mute">{mode === 'signin' ? t('login.password') : t('login.newPassword')}</span>
          <input className={input} type="password" autoComplete={mode === 'signin' ? 'current-password' : 'new-password'} minLength={mode === 'signin' ? undefined : 8}
            value={password} onChange={e => setP(e.target.value)} required /></label>
        {mode !== 'signin' && (
          <label className="mb-3 block text-[13px]"><span className="mb-1 block text-mute">{t('login.confirmPassword')}</span>
            <input className={input} type="password" autoComplete="new-password" value={password2} onChange={e => setP2(e.target.value)} required /></label>
        )}
        {err && <p role="alert" className="mb-3 text-[13px] text-danger">{err}</p>}
        <button disabled={busy} className="mt-1 w-full rounded-lg bg-accent py-2 font-medium text-white hover:brightness-110 disabled:opacity-60">
          {mode === 'signin' ? t('login.submit') : t('login.create')}</button>
        {!osAuth && mode === 'signin' && <button type="button" className="mt-3 w-full text-center text-xs text-mute hover:text-fg" onClick={() => { setMode('reset'); setErr('') }}>{t('login.forgot')}</button>}
        {mode === 'reset' && <button type="button" className="mt-3 w-full text-center text-xs text-mute hover:text-fg" onClick={() => { setMode('signin'); setErr('') }}>{t('login.back')}</button>}
      </form>
    </div>
  )
}
