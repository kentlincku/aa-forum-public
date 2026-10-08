import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { blob, fileDownloadUrl, filesMe, listFiles, uploadFile, type FileList, type Space } from '../api'
import { useI18n } from '../i18n'
import { fmtSize } from './rooms/Thread'

export default function Files() {
  const { t } = useI18n()
  const [spaces, setSpaces] = useState<Space[] | null>(null)
  const [space, setSpace] = useState('')
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const [list, setList] = useState<FileList | null>(null)
  const [offset, setOffset] = useState(0)
  const [err, setErr] = useState('')
  const [drag, setDrag] = useState(false)
  const [uploads, setUploads] = useState<{ name: string; p: number; state: 'up' | 'ok' | 'err'; msg?: string }[]>([])
  const pick = useRef<HTMLInputElement>(null)

  useEffect(() => { filesMe().then(d => { setSpaces(d.spaces); setSpace(d.spaces[0]?.id ?? '') }).catch(e => setErr(e.message)) }, [])
  const load = useCallback(() => {
    if (!space) return
    listFiles(space, query, offset).then(setList).catch(e => setErr(e.message))
  }, [space, query, offset])
  useEffect(load, [load])
  useEffect(() => { const id = setTimeout(() => { setOffset(0); setQuery(q.trim()) }, 300); return () => clearTimeout(id) }, [q])
  // index still building → poll until ready
  useEffect(() => { if (list && !list.ready) { const id = setTimeout(load, 2000); return () => clearTimeout(id) } }, [list, load])

  const cur = spaces?.find(s => s.id === space)
  async function upload(files: File[]) {
    if (!cur?.writable) return
    for (const f of files) {
      const i = uploads.length
      setUploads(u => [...u, { name: f.name, p: 0, state: 'up' }])
      try {
        await uploadFile(space, f, p => setUploads(u => u.map((x, j) => j === i ? { ...x, p } : x)))
        setUploads(u => u.map((x, j) => j === i ? { ...x, p: 1, state: 'ok' } : x))
      } catch (e) { setUploads(u => u.map((x, j) => j === i ? { ...x, state: 'err', msg: (e as Error).message } : x)) }
    }
    setTimeout(load, 500)
  }
  async function download(rel: string) {
    try {
      const b = await blob(fileDownloadUrl(space, rel)); const u = URL.createObjectURL(b)
      const a = document.createElement('a'); a.href = u; a.download = rel.split('/').pop() || 'download'; a.click()
      setTimeout(() => URL.revokeObjectURL(u), 10_000)
    } catch (e) { setErr((e as Error).message) }
  }
  function onDrop(e: DragEvent) { e.preventDefault(); setDrag(false); upload([...e.dataTransfer.files]) }

  return (
    <main className="min-w-0 flex-1 overflow-auto" onDragOver={e => { if (cur?.writable) { e.preventDefault(); setDrag(true) } }} onDragLeave={() => setDrag(false)} onDrop={onDrop}>
      <div className="mx-auto max-w-5xl p-6">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <h1 className="text-lg font-semibold">{t('files.title')}</h1>
          {spaces && spaces.length > 1 && (
            <div role="tablist" className="flex rounded-lg border border-line p-0.5">
              {spaces.map(s => <button key={s.id} role="tab" aria-selected={s.id === space} onClick={() => { setSpace(s.id); setOffset(0) }}
                className={`rounded-md px-3 py-1 text-[13px] ${s.id === space ? 'bg-sub font-medium' : 'text-mute hover:text-fg'}`}>{s.label}</button>)}
            </div>
          )}
          <input aria-label={t('files.search')} placeholder={t('files.search')} value={q} onChange={e => setQ(e.target.value)}
            className="ml-auto w-64 rounded-lg border border-line bg-panel px-3 py-1.5 text-[13px] outline-none focus:border-accent" />
          {cur?.writable && <>
            <button className="rounded-[7px] bg-accent px-3.5 py-1.5 text-[13px] font-medium text-white hover:brightness-110" onClick={() => pick.current?.click()}>{t('files.upload')}</button>
            <input ref={pick} type="file" multiple hidden onChange={e => { upload([...(e.target.files ?? [])]); e.target.value = '' }} />
          </>}
        </div>
        {cur && <p className="mb-3 text-xs text-mute">{cur.path} {cur.writable ? '' : `· ${t('files.readOnly')}`} {!cur.exists && `· ${t('files.missing')}`}</p>}
        {err && <p role="alert" className="mb-3 text-[13px] text-danger">{err}</p>}
        {uploads.length > 0 && (
          <ul className="mb-3 space-y-1 rounded-xl border border-line bg-panel p-3 text-[13px]">
            {uploads.map((u, i) => (
              <li key={i} className="flex items-center gap-3"><span className="truncate">{u.name}</span>
                {u.state === 'up' && <div className="ml-auto h-1 w-32 overflow-hidden rounded bg-sub"><i className="block h-full bg-accent" style={{ width: `${Math.round(u.p * 100)}%` }} /></div>}
                {u.state === 'ok' && <span className="ml-auto text-ok">✓</span>}
                {u.state === 'err' && <span className="ml-auto text-danger">{u.msg}</span>}</li>
            ))}
          </ul>
        )}
        <div className={`relative overflow-hidden rounded-xl border bg-panel ${drag ? 'border-accent ring-3 ring-accent-soft' : 'border-line'}`}>
          {drag && <div className="pointer-events-none absolute inset-0 z-10 grid place-items-center bg-accent-soft/90 font-medium text-accent">{t('files.drop')}</div>}
          {!list ? <p className="p-4 text-[13px] text-mute">{t('common.loading')}</p> : (
            <table className="w-full text-[13px]">
              <thead className="bg-sub text-left text-xs text-mute"><tr><th className="px-3 py-2 font-medium">{t('files.name')}</th><th className="px-3 py-2 text-right font-medium">{t('files.size')}</th><th className="px-3 py-2 font-medium">{t('files.modified')}</th></tr></thead>
              <tbody>
                {list.files.length === 0 && <tr><td colSpan={3} className="px-3 py-6 text-center text-mute">{query ? t('files.noMatch') : t('files.empty')}</td></tr>}
                {list.files.map(f => (
                  <tr key={f.rel} className="border-t border-line hover:bg-sub">
                    <td className="px-3 py-1.5"><button className="text-left hover:text-accent hover:underline" onClick={() => download(f.rel)} title={f.path}>{f.rel}</button>{!!f.partial && <span className="ml-2 text-xs text-warn">{t('files.partial')}</span>}</td>
                    <td className="px-3 py-1.5 text-right text-mute">{fmtSize(f.size)}</td>
                    <td className="px-3 py-1.5 text-mute">{f.modified}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {list && (
          <div className="mt-3 flex items-center gap-3 text-xs text-mute">
            <span>{t('files.count', { shown: list.files.length ? `${list.offset + 1}–${list.offset + list.files.length}` : '0', total: list.total })}</span>
            {!list.ready && <span>{t('files.indexing')}</span>}
            <div className="ml-auto flex gap-1">
              <button className="rounded px-2 py-1 hover:bg-sub disabled:opacity-40" disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - list.limit))}>{t('files.prev')}</button>
              <button className="rounded px-2 py-1 hover:bg-sub disabled:opacity-40" disabled={!list.truncated} onClick={() => setOffset(o => o + list.limit)}>{t('files.next')}</button>
            </div>
          </div>
        )}
      </div>
    </main>
  )
}
