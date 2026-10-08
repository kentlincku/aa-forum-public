import { useEffect } from 'react'

export const HUMAN = 'Owner'

/** Poll while the tab is visible; refresh immediately when it becomes visible again. */
export function usePoll(fn: () => void, ms: number, deps: unknown[]) {
  useEffect(() => {
    fn()
    const id = setInterval(() => { if (!document.hidden) fn() }, ms)
    const onShow = () => { if (!document.hidden) fn() }
    document.addEventListener('visibilitychange', onShow)
    return () => { clearInterval(id); document.removeEventListener('visibilitychange', onShow) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
}
