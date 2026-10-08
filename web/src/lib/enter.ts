// Enter-to-send that is safe with Chinese/Japanese IMEs (ported from the old chat page, incl. its Safari cases).
//  - isComposing / keyCode 229: the Enter is confirming an IME candidate, not sending.
//  - Safari may fire compositionend first and then keydown(Enter, 13, isComposing=false) in the same tick:
//    ignore Enter for one tick after compositionend.
export type KeyLike = { key: string; isComposing?: boolean; keyCode?: number; shiftKey?: boolean; altKey?: boolean; ctrlKey?: boolean; metaKey?: boolean }

export function enterSends(e: KeyLike, justComposed: boolean) {
  return e.key === 'Enter' && !e.isComposing && e.keyCode !== 229 && !e.shiftKey && !e.altKey && !justComposed
}

/** Tracks "a composition just ended in this tick". */
export function createComposeGuard() {
  let just = false
  return {
    onCompositionEnd() { just = true; setTimeout(() => { just = false }, 0) },
    get justComposed() { return just },
  }
}
