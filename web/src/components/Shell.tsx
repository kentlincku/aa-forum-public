import { NavLink, Outlet } from 'react-router-dom'
import { token } from '../api'
import { useI18n } from '../i18n'
import Icon from './Icon'

const items = ['dashboard', 'rooms', 'agents', 'skills', 'files'] as const

export function toggleTheme() {
  const d = document.documentElement
  d.dataset.theme = d.dataset.theme === 'dark' ? 'light' : 'dark'
  localStorage.setItem('aaf-theme', d.dataset.theme)
}

export default function Shell() {
  const { t, lang, setLang } = useI18n()
  const rail = 'grid size-9 place-items-center rounded-lg text-mute hover:bg-sub hover:text-fg'
  return (
    <div className="flex h-full overflow-hidden">
      <nav aria-label="Main" className="flex w-14 flex-none flex-col items-center gap-1.5 border-r border-line bg-panel py-3">
        <div className="mb-2.5 grid size-[30px] place-items-center rounded-lg bg-accent text-xs font-bold tracking-tight text-white">AA</div>
        {items.map(k => (
          <NavLink key={k} to={`/${k}`} title={t(`nav.${k}`)} aria-label={t(`nav.${k}`)}
            className={({ isActive }) => `${rail} ${isActive ? '!bg-accent-soft !text-accent' : ''}`}>
            <Icon name={k} />
          </NavLink>
        ))}
        <div className="flex-1" />
        <button className={`${rail} text-[11px] font-semibold`} title={t('nav.language')} onClick={() => setLang(lang === 'en' ? 'zh-TW' : 'en')}>
          {lang === 'en' ? '文' : 'EN'}
        </button>
        <button className={rail} title={t('nav.theme')} aria-label={t('nav.theme')} onClick={toggleTheme}><Icon name="moon" /></button>
        <button className={rail} title={t('nav.signout')} aria-label={t('nav.signout')}
          onClick={() => { token.clear(); window.dispatchEvent(new Event('aaf-signed-out')) }}><Icon name="out" /></button>
      </nav>
      <div className="flex min-w-0 flex-1"><Outlet /></div>
    </div>
  )
}
