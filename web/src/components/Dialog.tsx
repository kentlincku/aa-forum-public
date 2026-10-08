import { useEffect, useRef, type ReactNode } from 'react'

/** Native <dialog> modal. Closes on Escape / backdrop click; focus is trapped by the browser. */
export default function Dialog({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const d = ref.current; if (!d) return
    if (open && !d.open) { if (d.showModal) d.showModal(); else d.setAttribute('open', '') }
    if (!open && d.open) { if (d.close) d.close(); else d.removeAttribute('open') }
  }, [open])
  return (
    <dialog ref={ref} aria-label={title} onCancel={e => { e.preventDefault(); onClose() }}
      onClick={e => { if (e.target === ref.current) onClose() }}
      className="m-auto w-[min(440px,92vw)] rounded-xl border border-line bg-panel p-0 text-fg shadow-xl backdrop:bg-black/40">
      {open && <div className="p-5"><h2 className="mb-4 text-[15px] font-semibold">{title}</h2>{children}</div>}
    </dialog>
  )
}

export const field = 'w-full rounded-lg border border-line bg-bg px-3 py-2 outline-none focus:border-accent focus:ring-3 focus:ring-accent-soft'
export const btn = 'rounded-[7px] px-3.5 py-1.5 text-[13px] font-medium disabled:opacity-50'
export const btnPrimary = `${btn} bg-accent text-white hover:brightness-110`
export const btnGhost = `${btn} text-mute hover:bg-sub hover:text-fg`
export const btnDanger = `${btn} text-danger hover:bg-danger/10`
