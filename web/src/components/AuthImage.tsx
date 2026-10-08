import { useEffect, useState } from 'react'
import { blob } from '../api'

/** <img> for protected URLs: authorized fetch → object URL, revoked on unmount. */
export default function AuthImage({ src, alt, className }: { src: string; alt: string; className?: string }) {
  const [url, setUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let u: string | null = null, live = true
    blob(src).then(b => { if (live) { u = URL.createObjectURL(b); setUrl(u) } }).catch(() => live && setFailed(true))
    return () => { live = false; if (u) URL.revokeObjectURL(u) }
  }, [src])
  if (failed) return <span className="text-xs text-mute">[image unavailable]</span>
  if (!url) return <span className="block h-24 w-40 animate-pulse rounded-lg bg-sub" />
  return <a href={url} target="_blank" rel="noreferrer"><img src={url} alt={alt} className={className} /></a>
}
