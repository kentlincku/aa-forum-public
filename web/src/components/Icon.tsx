// Minimal stroke icons (no icon-font dependency).
const paths: Record<string, string> = {
  dashboard: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  rooms: 'M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z',
  agents: 'M12 4a4 4 0 1 1 0 8 4 4 0 0 1 0-8zM4 21c1-4 4-6 8-6s7 2 8 6',
  skills: 'M4 5h11l5 5v9a1 1 0 0 1-1 1H4zM8 13h8M8 17h5',
  files: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  moon: 'M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10z',
  out: 'M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 16l-4-4 4-4M6 12h10',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
}
export default function Icon({ name, className = 'size-[18px]' }: { name: keyof typeof paths | string; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className={className} fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round">
      <path d={paths[name]} />
    </svg>
  )
}
