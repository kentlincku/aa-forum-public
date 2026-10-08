import { createContext, useCallback, useContext, useState, type ReactNode } from 'react'
import en from './locales/en.json'
import zh from './locales/zh-TW.json'

export type Lang = 'en' | 'zh-TW'
const dicts: Record<Lang, Record<string, string>> = { en, 'zh-TW': zh }
type Ctx = { lang: Lang; setLang: (l: Lang) => void; t: (key: string, vars?: Record<string, string | number>) => string }
const I18n = createContext<Ctx | null>(null)

export function translate(lang: Lang, key: string, vars?: Record<string, string | number>) {
  let s = dicts[lang][key] ?? dicts.en[key] ?? key
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, String(v))
  return s
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => (localStorage.getItem('aaf-lang') as Lang) || 'en')
  const setLang = useCallback((l: Lang) => { localStorage.setItem('aaf-lang', l); document.documentElement.lang = l; setLangState(l) }, [])
  const t = useCallback((key: string, vars?: Record<string, string | number>) => translate(lang, key, vars), [lang])
  return <I18n.Provider value={{ lang, setLang, t }}>{children}</I18n.Provider>
}

export function useI18n() {
  const c = useContext(I18n)
  if (!c) throw new Error('useI18n outside I18nProvider')
  return c
}
