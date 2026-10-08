import { useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent, type KeyboardEvent } from 'react'
import { fileToBase64, getConfig, sendMessage, type Member, type Message, type Outgoing, type Room } from '../../api'
import { useI18n } from '../../i18n'
import { fmtSize } from './Thread'
import { HUMAN } from './shared'
import { createComposeGuard, enterSends } from '../../lib/enter'

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp']
const MAX_IMAGE = 5 * 1024 * 1024
let chatFileLimit: number | null | undefined          // from /api/config; null = unlimited

type Attachment = { kind: 'image' | 'file'; file: File; preview?: string }

export default function Composer({ room, members, replyTo, clearReply, onSent, label }: {
  room: Room; members: Member[]; replyTo: Message | null; clearReply: () => void; onSent: () => void
  label: (id: string) => string
}) {
  const { t } = useI18n()
  const draftKey = `aaf-draft-${room.id}`
  const [text, setText] = useState(() => sessionStorage.getItem(draftKey) ?? '')
  const [att, setAtt] = useState<Attachment | null>(null)
  const [task, setTask] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [drag, setDrag] = useState(false)
  const [mention, setMention] = useState<{ q: string; at: number; sel: number } | null>(null)
  const box = useRef<HTMLTextAreaElement>(null)
  const pick = useRef<HTMLInputElement>(null)
  // One client_id per logical send; reused on retry so the server dedupes a double post.
  const attempt = useRef<string | null>(null)
  const ime = useRef(createComposeGuard()).current

  useEffect(() => { if (chatFileLimit === undefined) getConfig().then(c => { chatFileLimit = c.chat_file_bytes }).catch(() => {}) }, [])
  useEffect(() => { sessionStorage.setItem(draftKey, text); attempt.current = null }, [text, draftKey])
  useEffect(() => { attempt.current = null }, [att, replyTo, task])
  useEffect(() => { if (replyTo) box.current?.focus() }, [replyTo])
  useEffect(() => () => { if (att?.preview) URL.revokeObjectURL(att.preview) }, [att])

  const candidates = useMemo(() => {
    if (!mention) return []
    const q = mention.q.toLowerCase()
    const people = members.filter(m => m.id !== HUMAN && !m.id.startsWith('__') && m.active !== 0)
      .map(m => ({ id: m.id, label: m.label || m.id }))
    return [{ id: 'all', label: t('composer.mentionAll') }, ...people]
      .filter(p => p.id.toLowerCase().startsWith(q) || p.label.toLowerCase().includes(q)).slice(0, 8)
  }, [mention, members, t])

  function attach(file: File) {
    setErr('')
    if (IMAGE_TYPES.includes(file.type)) {
      if (file.size > MAX_IMAGE) return setErr(t('composer.imageTooBig', { mb: 5 }))
      setAtt({ kind: 'image', file, preview: URL.createObjectURL(file) })
    } else {
      if (chatFileLimit && file.size > chatFileLimit) return setErr(t('composer.fileTooBig', { mb: Math.floor(chatFileLimit / 1048576) }))
      setAtt({ kind: 'file', file })
    }
  }

  async function send() {
    const body = text.trim()
    if ((!body && !att) || busy) return
    setBusy(true); setErr('')
    attempt.current ??= crypto.randomUUID()
    try {
      const m: Outgoing = { body, client_id: attempt.current, reply_to: replyTo?.id ?? null, notification_kind: task ? 'task' : 'chat' }
      if (att?.kind === 'image') m.image = await fileToBase64(att.file)
      if (att?.kind === 'file') m.file = { name: att.file.name, content: await fileToBase64(att.file) }
      await sendMessage(room.id, m)
      attempt.current = null
      setText(''); setAtt(null); setTask(false); clearReply(); sessionStorage.removeItem(draftKey); onSent()
    } catch (x) { setErr(t('composer.failed', { reason: (x as Error).message })) }   // keep text + attempt id for retry
    finally { setBusy(false) }
  }

  function updateMention(value: string, caret: number) {
    const m = /(^|[\s(])@([\w-]*)$/.exec(value.slice(0, caret))
    setMention(m ? { q: m[2], at: caret - m[2].length - 1, sel: 0 } : null)
  }
  function insertMention(id: string) {
    if (!mention) return
    const caret = box.current?.selectionStart ?? text.length
    const next = text.slice(0, mention.at) + '@' + id + ' ' + text.slice(caret)
    setText(next); setMention(null)
    requestAnimationFrame(() => { const p = mention.at + id.length + 2; box.current?.setSelectionRange(p, p); box.current?.focus() })
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    const n = e.nativeEvent
    if (n.isComposing || n.keyCode === 229 || ime.justComposed) return
    if (mention && candidates.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); const d = e.key === 'ArrowDown' ? 1 : -1
        setMention(m => m && { ...m, sel: (m.sel + d + candidates.length) % candidates.length }); return
      }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); insertMention(candidates[mention.sel].id); return }
      if (e.key === 'Escape') { e.preventDefault(); setMention(null); return }
    }
    if (e.key === 'Escape' && replyTo) { clearReply(); return }
    if (enterSends(n, ime.justComposed)) { e.preventDefault(); send() }
  }
  function onPaste(e: ClipboardEvent) {
    const f = [...e.clipboardData.files][0]
    if (f) { e.preventDefault(); attach(f) }
  }
  function onDrop(e: DragEvent) {
    e.preventDefault(); setDrag(false)
    const f = e.dataTransfer.files[0]; if (f) attach(f)
  }

  return (
    <div onDragOver={e => { e.preventDefault(); setDrag(true) }} onDragLeave={() => setDrag(false)} onDrop={onDrop}
      className={`relative mx-5 mb-4.5 rounded-[10px] border bg-panel px-3 py-2.5 focus-within:border-accent focus-within:ring-3 focus-within:ring-accent-soft ${drag ? 'border-accent ring-3 ring-accent-soft' : 'border-line'}`}>
      {drag && <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center rounded-[10px] bg-accent-soft/90 text-sm font-medium text-accent">{t('composer.drop')}</div>}
      {replyTo && (
        <div className="mb-2 flex items-center gap-2 rounded-md bg-sub px-2.5 py-1.5 text-xs text-mute">
          <span className="truncate">↳ {t('composer.replyingTo', { id: replyTo.id, who: label(replyTo.author) })}: {replyTo.body.slice(0, 80)}</span>
          <button className="ml-auto flex-none hover:text-fg" onClick={clearReply} aria-label={t('common.cancel')}>✕</button>
        </div>
      )}
      {att && (
        <div className="mb-2 flex items-center gap-2.5 rounded-md border border-line p-1.5 text-xs">
          {att.preview ? <img src={att.preview} alt="" className="h-12 w-12 rounded object-cover" /> : <span className="px-1 text-base">📎</span>}
          <span className="truncate">{att.file.name}</span><span className="text-mute">{fmtSize(att.file.size)}</span>
          <button className="ml-auto px-1 text-mute hover:text-fg" onClick={() => setAtt(null)} aria-label={t('composer.removeAttachment')}>✕</button>
        </div>
      )}
      <textarea ref={box} aria-label={t('composer.placeholder', { room: room.name })} rows={2}
        className="max-h-60 min-h-11 w-full resize-none bg-transparent outline-none [field-sizing:content]"
        placeholder={t('composer.placeholder', { room: room.name })} value={text}
        onChange={e => { setText(e.target.value); updateMention(e.target.value, e.target.selectionStart) }}
        onKeyDown={onKey} onPaste={onPaste} onCompositionEnd={() => ime.onCompositionEnd()} onBlur={() => setTimeout(() => setMention(null), 150)} />
      {mention && candidates.length > 0 && (
        <ul role="listbox" aria-label={t('composer.mention')} className="absolute bottom-full left-3 z-20 mb-1 w-64 overflow-hidden rounded-lg border border-line bg-panel py-1 shadow-lg">
          {candidates.map((c, i) => (
            <li key={c.id} role="option" aria-selected={i === mention.sel}
              onMouseDown={e => { e.preventDefault(); insertMention(c.id) }}
              className={`flex cursor-pointer items-center gap-2 px-3 py-1.5 text-[13px] ${i === mention.sel ? 'bg-accent-soft text-accent' : 'hover:bg-sub'}`}>
              <b className="font-medium">@{c.id}</b><span className="truncate text-mute">{c.label}</span>
            </li>
          ))}
        </ul>
      )}
      {err && <p role="alert" className="mb-1 text-xs text-danger">{err}</p>}
      <div className="flex items-center gap-2 text-xs text-mute">
        <button className="rounded-md px-2 py-1 hover:bg-sub hover:text-fg" onClick={() => pick.current?.click()}>📎 {t('composer.attach')}</button>
        <input ref={pick} type="file" hidden onChange={e => { const f = e.target.files?.[0]; if (f) attach(f); e.target.value = '' }} />
        <label className="flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1 hover:bg-sub hover:text-fg" title={t('composer.taskHint')}>
          <input type="checkbox" checked={task} onChange={e => setTask(e.target.checked)} /> {t('composer.asTask')}
        </label>
        <span className="ml-auto hidden sm:inline">{t('composer.hint')}</span>
        <button className="rounded-[7px] bg-accent px-3.5 py-1.5 text-[13px] font-medium text-white hover:brightness-110 disabled:opacity-50"
          disabled={busy || (!text.trim() && !att)} onClick={send}>{busy ? t('composer.sending') : t('composer.send')}</button>
      </div>
    </div>
  )
}
