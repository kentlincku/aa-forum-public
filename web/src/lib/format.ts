// Message rendering helpers. Output is React nodes only — never innerHTML.
import { createElement, Fragment, type ReactNode } from 'react'

export function renderBody(body: string): ReactNode[] {
  const out: ReactNode[] = []
  body.split('\n').forEach((line, li) => {
    if (li) out.push(createElement('br', { key: `br${li}` }))
    line.split(/(`[^`]+`|@[\w-]+)/g).forEach((part, pi) => {
      const key = `${li}.${pi}`
      if (!part) return
      if (part.startsWith('`') && part.endsWith('`') && part.length > 2) out.push(createElement('code', { key }, part.slice(1, -1)))
      else if (/^@[\w-]+$/.test(part)) out.push(createElement('span', { key, className: 'font-medium text-accent' }, part))
      else out.push(createElement(Fragment, { key }, part))
    })
  })
  return out
}

export function timeOf(created: number | string, lang?: string) {
  // Backend sends Unix seconds (float). Accept strings defensively; never throw while rendering.
  const d = typeof created === 'number' ? new Date(created * 1000) : new Date(String(created))
  return isNaN(+d) ? '' : d.toLocaleTimeString(lang ? [lang] : [], { hour: '2-digit', minute: '2-digit' })
}

const palette = ['#5e6ad2', '#d97706', '#0ea5e9', '#16a34a', '#a855f7', '#db2777', '#0d9488', '#ea580c']
export function colorFor(id: string) {
  let h = 0; for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return palette[h % palette.length]
}
