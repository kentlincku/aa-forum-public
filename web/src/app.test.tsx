import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { I18nProvider, translate } from './i18n'
import { renderBody, timeOf } from './lib/format'
import Rooms from './pages/Rooms'
import Login from './pages/Login'
import en from './locales/en.json'
import zh from './locales/zh-TW.json'

// ---- fake backend (response shapes copied from a real instance) ----------------------------
type Req = { url: string; method: string; body: any }
function backend(opts: { many?: number } = {}) {
  const calls: Req[] = []
  let folders: string[] = ['proj']
  let reminder: any = { body: '', minutes: 30, enabled: false, next_due: null }
  const rooms: any[] = [
    { id: 2, name: 'release', state: 'active', unread: 0, pinned: 1, folder: '', auto_approve: { on: true, until: '2026-10-14' } },
    { id: 3, name: 'old-stuff', state: 'archived', unread: 0, pinned: 0, folder: '', auto_approve: { on: false, until: null } },
    { id: 5, name: 'filed-room', state: 'active', unread: 0, pinned: 0, folder: 'proj', notify: 1, auto_approve: { on: false, until: null } },
  ]
  const base = { reply_to: null, client_id: 'c', image: null, file: null, delivery: [], likes: [], read_by: [] as any[], mentions: [] as string[] }
  let msgs: any[] = [
    { ...base, id: 41, room: 2, author: 'Owner', body: 'hi @lead see `x.py`', mentions: ['lead'], created: 1791386780.92, notification_kind: 'chat',
      delivery: [{ recipient: 'lead', status: 'pending', attempts: 0, detail: null }] },
    { ...base, id: 42, room: 2, author: 'lead', body: '<img src=x onerror=alert(1)>', mentions: ['Owner'], created: 1791386791.8, notification_kind: 'task' },
    { ...base, id: 43, room: 2, author: '__system__', body: 'system note', created: 1791386792, notification_kind: 'chat' },
    { ...base, id: 44, room: 2, author: 'lead', body: 'with file', created: 1791386793, notification_kind: 'chat',
      file: { name: 'report.pdf', size: 2_400_000, url: '/api/rooms/2/messages/44/file' }, reply_to: 41 },
  ]
  if (opts.many) msgs = Array.from({ length: opts.many }, (_, i) => ({ ...base, id: i + 1, room: 2, author: 'lead', body: `m${i + 1}`, created: 1791386000 + i, notification_kind: 'chat' }))
  const members = [{ id: 'Owner', label: 'owner', active: 1, last_read: 0 }, { id: 'lead', label: 'Lead', active: 1, last_read: 0 },
    { id: 'reviewer', label: 'Reviewer', active: 1, last_read: 0 }]
  const agents = [{ id: 'lead', label: 'Lead', online: true, acp_agent: 'codex', model: 'gpt' }, { id: 'reviewer', label: 'Reviewer', online: false },
    { id: 'builder', label: 'Builder', online: true }]
  const engines = [
    { role: 'lead', per_room: true, overridden: false, acp_agent: 'codex', model: 'gpt', default_agent: 'codex', default_model: 'gpt' },
    { role: 'reviewer', per_room: false, overridden: false, acp_agent: null, model: null, default_agent: null, default_model: null },
  ]
  const j = (d: unknown, status = 200) => new Response(JSON.stringify(d), { status })
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ url, method, body })
    const u = new URL(url, 'http://x')
    if (u.pathname === '/login') return j({ detail: 'bad' }, 401)
    if (u.pathname === '/api/config') return j({ owner: 'o', chat_file_bytes: 1024 * 1024, upload_bytes: null, version: '1' })
    if (u.pathname === '/api/rooms' && method === 'GET') return j(rooms)
    if (u.pathname === '/api/rooms' && method === 'POST') { rooms.push({ ...rooms[0], id: 9, name: body.name, pinned: 0 }); return j({ id: 9 }) }
    if (u.pathname === '/api/agents') return j(agents)
    if (u.pathname === '/api/rooms/2/messages' && method === 'GET') {
      const after = Number(u.searchParams.get('after') || 0)
      const page = msgs.filter(m => m.id > after).slice(0, 500)
      return j({ messages: page, members, has_more: page.length === 500 })
    }
    if (u.pathname === '/api/rooms/3/messages') return j({ messages: [], members, has_more: false })
    if (u.pathname === '/api/rooms/2/messages' && method === 'POST') {
      const id = Math.max(...msgs.map(m => m.id)) + 1
      msgs.push({ ...base, id, room: 2, author: 'Owner', body: body.body, created: 1791387000, notification_kind: body.notification_kind, reply_to: body.reply_to })
      return j({ id })
    }
    if (u.pathname === '/api/rooms/2/confirm-read') { for (const id of body.messages) msgs.find(m => m.id === id).read_by.push({ id: 'Owner', label: 'owner' }); return j({ ok: true }) }
    if (/\/like$/.test(u.pathname)) { const m = msgs.find(m => m.id === Number(u.pathname.split('/')[5])); m.likes = body.liked ? [{ id: 'Owner', label: 'owner' }] : []; return j({ ok: true }) }
    if (u.pathname === '/api/rooms/2/approval' && method === 'GET') return j(rooms[0].auto_approve)
    if (u.pathname === '/api/rooms/2/approval') { rooms[0].auto_approve = { on: body.on, until: body.until }; return j({ ok: true }) }
    if (u.pathname === '/api/rooms/2/members') return j({ ok: true, added: true })
    if (u.pathname === '/api/rooms/2/engines') return j(engines)
    if (u.pathname === '/api/rooms/2/agents/lead/engine') {
      const e = engines[0]
      if (!body.agent && !body.model) Object.assign(e, { overridden: false, acp_agent: 'codex', model: 'gpt' })
      else Object.assign(e, { overridden: true, acp_agent: body.agent || 'codex', model: body.model || null })
      return j({ ok: true, state: e.overridden ? 'set' : 'cleared', ...e })
    }
    if (u.pathname === '/api/agents/catalog') return j({ agents: [{ id: 'codex', label: 'Codex', installed: true }, { id: 'pi', label: 'pi', installed: true }], roles: [] })
    if (u.pathname.startsWith('/api/agents/models/')) return j({ default: 'm-pi', models: [{ id: 'm-pi' }, { id: 'm-pi' }] })
    if (u.pathname === '/api/rooms/2/manage') { if (body.name) rooms[0].name = body.name; return j({ ok: true }) }
    if (u.pathname === '/api/folders' && method === 'GET') return j(folders)
    if (u.pathname === '/api/folders') { if (body.delete) folders = folders.filter(f => f !== body.name); else if (body.original) folders = folders.map(f => f === body.original ? body.name : f); else folders.push(body.name); return j({ ok: true }) }
    if (u.pathname === '/api/search') return j({ messages: [{ id: 42, room: 2, author: 'lead', body: 'global hit body', room_name: 'release' }], has_more: false })
    if (u.pathname === '/api/rooms/2/reminder' && method === 'GET') return j(reminder)
    if (u.pathname === '/api/rooms/2/reminder') { reminder = { ...body, next_due: 1791387600 }; return j({ ok: true, next_due: 1791387600, sent_initial: true }) }
    if (u.pathname === '/api/rooms/2/reminder/stop') { reminder = { ...reminder, enabled: 0 }; return j({ ok: true }) }
    if (u.pathname === '/api/rooms/2/reminder-files' && method === 'GET') return u.searchParams.get('name')
      ? j({ name: u.searchParams.get('name'), body: 'TEMPLATE BODY' }) : j({ directory: '/x/lib', files: ['template_standup.md'] })
    if (u.pathname === '/api/rooms/2/reminder-files') return j({ name: body.name + '.md' })
    if (u.pathname === '/api/rooms/2/freeze') { rooms[0].state = body.frozen ? 'frozen' : 'active'; return j({ ok: true, state: rooms[0].state }) }
    if (u.pathname === '/api/rooms/2/search') return j({ results: [{ id: 42, author: 'lead', created: 1, snippet: 'found it' }], has_more: false })
    if (method === 'POST') return j({ ok: true })
    return j({ detail: 'nope' }, 404)
  }))
  return { calls, posts: () => calls.filter(c => c.method === 'POST' && c.url === '/api/rooms/2/messages').map(c => c.body), msgs }
}

const wrap = (path = '/rooms/2') => render(
  <I18nProvider><MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/rooms/:roomId" element={<Rooms />} /><Route path="/rooms" element={<Rooms />} />
  </Routes></MemoryRouter></I18nProvider>)
const composer = () => screen.getByRole('textbox', { name: /^Message #/ })

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks()
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) { this.removeAttribute('open') }
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

// ---- pure helpers --------------------------------------------------------------------------
describe('i18n', () => {
  it('en and zh-TW have the same keys', () => expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort()))
  it('every literal t() key used in the source exists', () => {
    const src = import.meta.glob('./**/*.tsx', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
    const used = new Set<string>()
    for (const [file, text] of Object.entries(src)) {
      if (file.includes('.test.')) continue
      for (const m of text.matchAll(/\bt\('([a-z][\w.]*)'/g)) used.add(m[1])
    }
    expect(used.size).toBeGreaterThan(50)
    expect([...used].filter(k => !(k in en))).toEqual([])
  })
  it('interpolates and falls back to the key', () => {
    expect(translate('zh-TW', 'room.autoUntil', { date: '2026-10-14' })).toBe('自動核准到 2026-10-14')
    expect(translate('zh-TW', 'no.such.key')).toBe('no.such.key')
  })
})
describe('format', () => {
  it('timeOf handles Unix seconds and never throws', () => {
    expect(timeOf(1791386780.92)).toMatch(/\d{1,2}:\d{2}/)
    expect(timeOf('garbage')).toBe('')
  })
  it('renderBody never produces HTML from message text', () => {
    const { container } = render(<div>{renderBody('<b>x</b> @reviewer `a<b>`')}</div>)
    expect(container.querySelector('b')).toBeNull()
    expect(container.querySelector('code')?.textContent).toBe('a<b>')
  })
})

// ---- Rooms page ----------------------------------------------------------------------------
describe('Rooms: list and thread', () => {
  it('shows sections, the thread, badges, file chip, reply context, members', async () => {
    backend(); wrap()
    expect(await screen.findByRole('heading', { name: 'release' })).toBeInTheDocument()
    const list = screen.getByRole('complementary', { name: 'Rooms' })
    expect(within(list).getByText('Pinned')).toBeInTheDocument()
    expect(within(list).getByText('Archived')).toBeInTheDocument()
    expect(within(list).queryByText('old-stuff')).toBeNull()                          // archived is collapsed
    expect(screen.getByRole('button', { name: /Auto-approve until 2026-10-14/ })).toBeInTheDocument()
    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(document.querySelector('main img')).toBeNull()
    expect(screen.getByText('system note')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /report\.pdf/ })).toHaveTextContent('2.3 MB')
    expect(screen.getByRole('button', { name: /↳ #41 You: hi @lead/ })).toBeInTheDocument()
    expect(screen.getByText('1 notification(s) pending')).toBeInTheDocument()
    expect(screen.getByRole('complementary', { name: 'Members' })).toHaveTextContent('codex · gpt')
  })

  it('fetches every page (server returns 500 at a time)', async () => {
    const b = backend({ many: 1203 }); wrap()
    expect(await screen.findByText('m1203')).toBeInTheDocument()
    expect(screen.getByText('m1')).toBeInTheDocument()
    const gets = b.calls.filter(c => c.url.startsWith('/api/rooms/2/messages?'))
    expect(gets.map(c => c.url).slice(0, 3)).toEqual(['/api/rooms/2/messages?after=0', '/api/rooms/2/messages?after=500', '/api/rooms/2/messages?after=1000'])
  })

  it('read receipts are explicit: nothing is confirmed until the button is pressed', async () => {
    const b = backend(); wrap()
    await screen.findByText('<img src=x onerror=alert(1)>')
    expect(b.calls.some(c => c.url.endsWith('/confirm-read'))).toBe(false)
    await userEvent.click(screen.getByRole('button', { name: 'Mark message #42 as read' }))
    await waitFor(() => expect(b.calls.find(c => c.url.endsWith('/confirm-read'))?.body).toEqual({ messages: [42] }))
    expect(await screen.findAllByText(/✓ Read/)).not.toHaveLength(0)
  })

  it('likes and unlikes with target state (not toggle)', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    const row = screen.getByText('with file').closest('article')!
    await userEvent.click(within(row).getByRole('button', { name: /Like/ }))
    await waitFor(() => expect(b.calls.find(c => c.url.endsWith('/44/like'))?.body).toEqual({ liked: true }))
    await userEvent.click(await within(row).findByRole('button', { name: /Liked/ }))
    await waitFor(() => expect(b.calls.filter(c => c.url.endsWith('/44/like')).at(-1)?.body).toEqual({ liked: false }))
  })
})

describe('Rooms: per-room agent/model', () => {
  it('switches lead for this room only, shows the badge, and resets', async () => {
    const b = backend(); wrap()
    const members = await screen.findByRole('complementary', { name: 'Members' })
    await waitFor(() => expect(members).toHaveTextContent('codex · gpt'))
    await userEvent.click(within(members).getByRole('button', { name: /Lead/ }))
    const dlg = await screen.findByRole('dialog')
    // opens on what the room uses now (custom ID 'gpt'), not the agent's own default
    expect(within(dlg).getByRole('textbox', { name: 'Model ID' })).toHaveValue('gpt')
    await userEvent.selectOptions(within(dlg).getByLabelText('Agent'), 'pi')
    await waitFor(() => expect(within(dlg).getByRole('option', { name: 'm-pi' })).toBeInTheDocument())
    expect(within(dlg).getAllByRole('option', { name: 'm-pi' })).toHaveLength(1)        // duplicates from the agent are merged
    await userEvent.selectOptions(within(dlg).getByLabelText('Model'), 'm-pi')
    await userEvent.click(within(dlg).getByRole('button', { name: 'Apply to this room' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/rooms/2/agents/lead/engine')?.body).toEqual({ agent: 'pi', model: 'm-pi' }))
    await waitFor(() => expect(members).toHaveTextContent('pi · m-pi'))
    expect(within(members).getByText('this room')).toBeInTheDocument()
    await userEvent.click(within(members).getByRole('button', { name: /Lead/ }))
    await userEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Use default' }))
    await waitFor(() => expect(members).toHaveTextContent('codex · gpt'))
    expect(within(members).queryByText('this room')).toBeNull()
    // reviewer cannot switch per room: row is still clickable and explains why
    await userEvent.click(within(members).getByRole('button', { name: /Reviewer/ }))
    expect(within(await screen.findByRole('dialog')).getByText(/only be switched for all rooms/)).toBeInTheDocument()
  })
})

describe('Rooms: composer', () => {
  it('sends on Enter, not Shift+Enter, and clears', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    await userEvent.type(composer(), 'line1{Shift>}{Enter}{/Shift}line2')
    expect(b.posts()).toHaveLength(0)
    await userEvent.type(composer(), '{Enter}')
    await waitFor(() => expect(b.posts()).toHaveLength(1))
    expect(b.posts()[0]).toMatchObject({ body: 'line1\nline2', notification_kind: 'chat', reply_to: null })
    await waitFor(() => expect(composer()).toHaveValue(''))
  })

  it('reply + task mode are sent; Escape cancels a reply', async () => {
    const b = backend(); wrap()
    const row = (await screen.findByText('with file')).closest('article')!
    await userEvent.click(within(row).getByRole('button', { name: 'Reply' }))
    expect(screen.getByText(/Replying to #44 · Lead/)).toBeInTheDocument()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByText(/Replying to #44/)).toBeNull()
    await userEvent.click(within(row).getByRole('button', { name: 'Reply' }))
    await userEvent.click(screen.getByRole('checkbox', { name: /Task/ }))
    await userEvent.type(composer(), 'on it{Enter}')
    await waitFor(() => expect(b.posts()[0]).toMatchObject({ body: 'on it', reply_to: 44, notification_kind: 'task' }))
  })

  it('@ opens the member picker; arrows + Enter insert the id', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    await userEvent.type(composer(), 'hey @re')
    const lb = await screen.findByRole('listbox', { name: 'Mention' })
    expect(within(lb).getAllByRole('option').map(o => o.textContent)).toEqual(['@reviewerReviewer'])
    await userEvent.keyboard('{Enter}')
    expect(composer()).toHaveValue('hey @reviewer ')
    expect(b.posts()).toHaveLength(0)                                               // Enter picked, did not send
    await userEvent.type(composer(), '@')
    expect(within(await screen.findByRole('listbox')).getAllByRole('option')[0]).toHaveTextContent('@all')
  })

  it('attaches a file (base64) and an image; rejects oversized files', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    const input = document.querySelector('input[type=file]') as HTMLInputElement
    await userEvent.upload(input, new File(['hello'], 'notes.txt', { type: 'text/plain' }))
    expect(screen.getByText('notes.txt')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(b.posts()[0]?.file).toEqual({ name: 'notes.txt', content: btoa('hello') }))

    await userEvent.upload(input, new File([new Uint8Array(2 * 1024 * 1024)], 'big.bin'))   // limit from /api/config = 1 MB
    expect(await screen.findByRole('alert')).toHaveTextContent('Files must be under 1 MB.')

    URL.createObjectURL = vi.fn(() => 'blob:x'); URL.revokeObjectURL = vi.fn()
    const png = new File([new Uint8Array([137, 80, 78, 71])], 'shot.png', { type: 'image/png' })
    fireEvent.paste(composer(), { clipboardData: { files: [png] } })
    expect(await screen.findByText('shot.png')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(b.posts()[1]?.image).toBe(btoa(String.fromCharCode(137, 80, 78, 71))))
  })

  it('keeps the same client_id when retrying a failed send', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    const real = globalThis.fetch as any
    let fail = true
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (fail && init?.method === 'POST' && url === '/api/rooms/2/messages') { b.calls.push({ url, method: 'POST', body: JSON.parse(String(init.body)) }); fail = false; return new Response('{"detail":"boom"}', { status: 500 }) }
      return real(url, init)
    }))
    await userEvent.type(composer(), 'retry me{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't send: boom")
    expect(composer()).toHaveValue('retry me')
    await userEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(b.posts()).toHaveLength(2))
    expect(b.posts()[0].client_id).toBe(b.posts()[1].client_id)
  })
})

describe('Rooms: create and settings', () => {
  it('creates a room with picked members and navigates to it', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    await userEvent.click(screen.getByRole('button', { name: /New room/ }))
    const dlg = screen.getByRole('dialog', { name: 'New room' })
    await userEvent.type(within(dlg).getByLabelText('Room name'), 'launch')
    await userEvent.click(within(dlg).getByRole('checkbox', { name: /Builder/ }))
    await userEvent.click(within(dlg).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/rooms' && c.method === 'POST')?.body).toEqual({ name: 'launch', members: ['builder'] }))
  })

  it('renames, sets auto-approve with a date, invites, and searches', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }))
    const dlg = screen.getByRole('dialog', { name: /settings/ })
    const name = within(dlg).getByLabelText('Room name')
    await userEvent.clear(name); await userEvent.type(name, 'release-2')
    await userEvent.click(within(dlg).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(b.calls.find(c => c.url.endsWith('/manage'))?.body).toEqual({ name: 'release-2' }))

    await userEvent.click(within(dlg).getByRole('tab', { name: 'Auto-approve' }))
    fireEvent.change(await within(dlg).findByLabelText('Until'), { target: { value: '2099-01-02' } })
    await userEvent.click(within(dlg).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(b.calls.filter(c => c.url.endsWith('/approval') && c.method === 'POST').at(-1)?.body).toEqual({ on: true, until: '2099-01-02' }))
    await userEvent.click(within(dlg).getByRole('button', { name: 'Turn off' }))
    await waitFor(() => expect(b.calls.filter(c => c.url.endsWith('/approval') && c.method === 'POST').at(-1)?.body).toEqual({ on: false, until: null }))

    await userEvent.click(within(dlg).getByRole('tab', { name: 'Members' }))
    await userEvent.type(within(dlg).getByLabelText(/Why does this room need them/), 'needs builds')
    await userEvent.click(within(dlg).getByRole('button', { name: 'Invite' }))
    await waitFor(() => expect(b.calls.find(c => c.url.endsWith('/members'))?.body).toEqual({ agent: 'builder', reason: 'needs builds' }))

    await userEvent.click(within(dlg).getByRole('tab', { name: 'Search' }))
    await userEvent.type(within(dlg).getByLabelText('Search this room'), 'found{Enter}')
    expect(await within(dlg).findByText('found it')).toBeInTheDocument()
  })

  it('reminder: load template, start, stop, save as file', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }))
    const dlg = screen.getByRole('dialog', { name: /settings/ })
    await userEvent.click(within(dlg).getByRole('tab', { name: 'Reminder' }))
    await userEvent.selectOptions(await within(dlg).findByRole('combobox', { name: 'Load a template or saved file' }), 'template_standup.md')
    await userEvent.click(within(dlg).getByRole('button', { name: 'Load' }))
    await waitFor(() => expect(within(dlg).getByRole('textbox', { name: 'Reminder text' })).toHaveValue('TEMPLATE BODY'))
    await userEvent.selectOptions(within(dlg).getByRole('combobox', { name: 'Every' }), '15')
    await userEvent.click(within(dlg).getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/rooms/2/reminder' && c.method === 'POST')?.body).toEqual({ body: 'TEMPLATE BODY', minutes: 15, enabled: true }))
    expect(await within(dlg).findByText(/On · every 15 min/)).toBeInTheDocument()
    await userEvent.click(within(dlg).getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(b.calls.some(c => c.url.endsWith('/reminder/stop'))).toBe(true))
    await userEvent.click(within(dlg).getByText('Save as a file for reuse'))
    await userEvent.type(within(dlg).getByLabelText('File name'), 'mine')
    await userEvent.click(within(dlg).getAllByRole('button', { name: 'Save' }).at(-1)!)
    expect(await within(dlg).findByText('Saved as mine.md.')).toBeInTheDocument()
  })

  it('folders group the sidebar; freeze asks for a reason and makes the room read-only', async () => {
    const b = backend(); wrap()
    await screen.findByText('with file')
    const list = screen.getByRole('complementary', { name: 'Rooms' })
    expect(within(list).getByText('proj')).toBeInTheDocument()
    expect(within(list).queryByText('filed-room')).toBeInTheDocument()                 // folder sections start open
    expect(within(list).queryByText('Other rooms')).toBeNull()                          // no unfiled rooms → no section
    await userEvent.click(within(list).getByRole('button', { name: 'Folders' }))
    const fd = screen.getByRole('dialog', { name: 'Folders' })
    await userEvent.type(within(fd).getByLabelText('New folder name'), 'ops')
    await userEvent.click(within(fd).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/folders' && c.method === 'POST')?.body).toEqual({ name: 'ops' }))
    await userEvent.click(within(fd).getByRole('button', { name: 'Close' }))

    vi.spyOn(window, 'prompt').mockReturnValue('waiting on vendor')
    await userEvent.click(screen.getByRole('button', { name: 'Settings' }))
    const dlg = screen.getByRole('dialog', { name: /settings/ })
    await userEvent.click(within(dlg).getByRole('button', { name: 'Freeze' }))
    await waitFor(() => expect(b.calls.find(c => c.url.endsWith('/freeze'))?.body).toEqual({ frozen: true, reason: 'waiting on vendor' }))
    expect(await screen.findByText(/This room is read-only/)).toBeInTheDocument()
  })

  it('sidebar search: Enter searches all messages and jumps to the hit', async () => {
    backend(); wrap()
    await screen.findByText('with file')
    await userEvent.type(screen.getByLabelText('Filter rooms'), 'hit{Enter}')
    const hits = await screen.findByRole('region', { name: 'Messages' })
    await userEvent.click(within(hits).getByText('global hit body'))
    await waitFor(() => expect(document.getElementById('msg-42')!.className).toContain('bg-accent-soft'))
  })

  it('archived room is read-only (no composer)', async () => {
    backend(); wrap('/rooms/3')
    expect(await screen.findByText(/This room is read-only/)).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /^Message #/ })).toBeNull()
  })
})

describe('Login', () => {
  it('first run shows account setup; mismatched passwords are caught before calling the server', async () => {
    const calls: any[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init?.body && JSON.parse(String(init.body))])
      if (url === '/api/setup-state') return new Response('{"has_accounts":false}')
      if (url === '/setup') return new Response('{"ok":true}')
      if (url === '/login') return new Response('{"token":"tok"}')
      return new Response('{}', { status: 404 })
    }))
    const done = vi.fn()
    render(<I18nProvider><Login onSignedIn={done} /></I18nProvider>)
    expect(await screen.findByRole('heading', { name: 'Create your account' })).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText('Username'), 'me')
    await userEvent.type(screen.getByLabelText(/New password/), 'pw-123456')
    await userEvent.type(screen.getByLabelText('Confirm password'), 'pw-1234567')
    await userEvent.click(screen.getByRole('button', { name: 'Create and sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("The passwords don't match.")
    expect(calls.some(c => c[0] === '/setup')).toBe(false)
    await userEvent.clear(screen.getByLabelText('Confirm password')); await userEvent.type(screen.getByLabelText('Confirm password'), 'pw-123456')
    await userEvent.click(screen.getByRole('button', { name: 'Create and sign in' }))
    await waitFor(() => expect(done).toHaveBeenCalled())
    expect(calls.find(c => c[0] === '/setup')[1]).toEqual({ username: 'me', password: 'pw-123456' })
    expect(sessionStorage.getItem('aaf-token')).toBe('tok')
  })

  it('shows a translated error on 401', async () => {
    backend()
    render(<I18nProvider><Login onSignedIn={() => {}} /></I18nProvider>)
    await userEvent.type(screen.getByLabelText('Username'), 'u')
    await userEvent.type(screen.getByLabelText('Password'), 'p')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong username or password.')
  })
})
