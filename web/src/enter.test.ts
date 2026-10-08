import { describe, expect, it } from 'vitest'
import { createComposeGuard, enterSends } from './lib/enter'

// Same cases the old page was tested with.
const ENTER_CASES: [Parameters<typeof enterSends>[0], boolean][] = [
  [{ key: 'Enter' }, true],
  [{ key: 'Enter', shiftKey: true }, false],            // newline
  [{ key: 'Enter', ctrlKey: true }, true],
  [{ key: 'Enter', metaKey: true }, true],
  [{ key: 'Enter', isComposing: true }, false],         // picking an IME candidate
  [{ key: 'Enter', keyCode: 229 }, false],              // older Safari IME signal
  [{ key: 'Enter', altKey: true }, false],
  [{ key: 'a' }, false],
]

describe('enterSends', () => {
  it.each(ENTER_CASES)('%o → %s', (e, want) => expect(enterSends(e, false)).toBe(want))

  // ('k', keyCode, isComposing) | ('ce') compositionend | ('tick') let timers run
  type Step = ['k', number, boolean] | ['ce'] | ['tick']
  const SEQ: Record<string, [Step[], number]> = {
    'chrome: keydown(229,composing) → compositionend': [[['k', 229, true], ['ce']], 0],
    'safari: compositionend → keydown(229)': [[['ce'], ['k', 229, false]], 0],
    'safari-variant: compositionend → keydown(13)': [[['ce'], ['k', 13, false]], 0],
    'safari-variant, then Enter on the next tick': [[['ce'], ['k', 13, false], ['tick'], ['k', 13, false]], 1],
    'plain Enter': [[['k', 13, false]], 1],
  }
  it.each(Object.entries(SEQ))('%s', async (_, [steps, want]) => {
    const g = createComposeGuard()
    let sent = 0
    for (const s of steps) {
      if (s[0] === 'ce') g.onCompositionEnd()
      else if (s[0] === 'tick') await new Promise(r => setTimeout(r, 0))
      else if (enterSends({ key: 'Enter', keyCode: s[1], isComposing: s[2] }, g.justComposed)) sent++
    }
    expect(sent).toBe(want)
  })
})
