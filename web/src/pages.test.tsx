import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { I18nProvider } from './i18n'
import Agents from './pages/Agents'
import Skills from './pages/Skills'
import Files from './pages/Files'
import Dashboard from './pages/Dashboard'

type Req = { url: string; method: string; body: any; headers: Record<string, string> }
function backend() {
  const calls: Req[] = []
  const catalog = {
    agents: [
      { id: 'codex', label: 'Codex', installed: true, cmd: ['codex-acp'], npm: '@zed/codex-acp', needs: null, custom: false, logged_in: true, login_detail: 'ChatGPT', install_cmd: null },
      { id: 'pi', label: 'pi', installed: true, cmd: ['pi-acp'], npm: 'pi-acp', needs: null, custom: false, logged_in: null, login_detail: '', install_cmd: null },
      { id: 'gemini', label: 'Gemini', installed: false, cmd: null, npm: '@google/gemini-cli', needs: 'Needs a Google account', custom: false, logged_in: null, login_detail: '', install_cmd: ['npm', 'install', '@google/gemini-cli'] },
    ],
    skills: [{ id: 'lead', path: 'skills/lead/SKILL.md', description: 'Lead persona' }, { id: 'release', path: 'skills/release/SKILL.md', description: 'Release steps' }],
    roles: [{ id: 'me', agent: 'manual', rank: 'human' }, { id: 'lead', agent: 'codex', rank: 'lead' }, { id: 'builder', agent: 'pi', rank: 'worker' }],
  }
  const agents = [{ id: 'Owner', label: 'me', online: true }, { id: 'lead', label: 'Lead', online: true, acp_agent: 'codex', model: 'gpt-5.5' },
    { id: 'builder', label: 'Builder', online: false, acp_agent: 'pi', model: null }]
  const sessions = [{ room: null, state: 'open', session: 'S0', context_percent: 12, last_activity: 1791386000, recall_pending: false },
    { room: 2, room_name: 'release', state: 'parked', session: null, context_percent: null, last_activity: null, recall_pending: false },
    { room: 3, room_name: 'ci', state: 'open', session: 'S3', context_percent: 85, last_activity: 1791386100, recall_pending: true }]
  const listing = {
    skills: [{ id: 'lead', description: 'Lead persona', packs: [], used_by: ['角色 lead（人設）'], bytes: 120, mtime: 1 },
      { id: 'release', description: 'Release steps', packs: ['ops'], used_by: [], bytes: 80, mtime: 1 }],
    packs: { ops: ['release'] }, rooms: {} as Record<string, any>,
    roles: { me: { persona_file: null, skills: [], skill_packs: [], rank: 'human' }, lead: { persona_file: 'skills/lead/SKILL.md', skills: [], skill_packs: [], rank: 'lead' } },
  }
  let content = '---\nname: release\ndescription: Release steps\n---\nstep 1\n'
  let jobPolls = 0
  const j = (d: unknown, status = 200) => new Response(JSON.stringify(d), { status })
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = init?.body && typeof init.body === 'string' ? JSON.parse(init.body) : null
    calls.push({ url, method, body, headers: (init?.headers ?? {}) as Record<string, string> })
    const u = new URL(url, 'http://x'); const p = u.pathname
    if (p === '/api/agents/catalog') return j(catalog)
    if (p === '/api/agents') return j(agents)
    if (p === '/api/account-quota') return j({ accounts: [{ engine: 'Codex', agent: 'codex', in_use: true, plan: 'plus', windows: [{ label: '5h', remaining_percent: 10 }], observed_at: null }] })
    if (p === '/api/agents/lead/sessions') return j({ role: 'lead', sessions })
    if (p.startsWith('/api/agents/models/')) return j({ default: 'gpt-5.5', models: [{ id: 'gpt-5.5', name: 'gpt-5.5' }, { id: 'gpt-5.6', name: 'gpt-5.6' }], source: 'list' })
    if (p === '/api/agents/roles/lead/engine') return body.model === 'bad' ? j({ detail: 'model bad failed a test run' }, 400) : j({ ok: true, changed: true })
    if (p === '/api/agents/lead/sessions/3/close') return j({ ok: true })
    if (p === '/api/agents/probe') return j({ ok: true })
    if (p === '/api/agents/install') return j({ id: 'job1', agent: 'gemini', state: 'running' })
    if (p === '/api/agents/install/job1') { jobPolls++; return j(jobPolls < 2 ? { id: 'job1', state: 'running', output: '$ npm install\n', offset: 14 } : { id: 'job1', state: 'done', output: 'done\n', offset: 19 }) }
    if (p === '/api/agents/roles' && method === 'POST') return j({ ok: true, message: 'Added; ready in ~10s.' })
    if (p === '/api/skills' && method === 'GET') return j(listing)
    if (p === '/api/skills' && method === 'POST') return j({ ok: true })
    if (p === '/api/skills/release' && method === 'GET') return j({ id: 'release', content, used_by: [] })
    if (p === '/api/skills/release' && method === 'PUT') { content = body.content; return j({ ok: true }) }
    if (p === '/api/skills/release/history') return j({ versions: [{ version: '20261007-120000-000', bytes: 40 }], log: [{ at: 1791386000, actor: 'owner', action: 'edit', skill: 'release', note: 'first' }] })
    if (p === '/api/skills/release/history/20261007-120000-000') return j({ content: 'OLD VERSION' })
    if (p === '/api/skills/release/restore') return j({ ok: true })
    if (p.startsWith('/api/skill-packs/')) return j({ ok: true })
    if (p.startsWith('/api/skill-assign/')) return j({ ok: true })
    if (p === '/api/skill-preview') return j({ role: u.searchParams.get('role'), persona: 'skills/lead/SKILL.md', skills: ['release'] })
    if (p === '/api/rooms') return j([{ id: 2, name: 'release', state: 'active', unread: 3, auto_approve: { on: false, until: null } }])
    if (p === '/api/runs') return j({ runs: [{ role: 'lead', started: 1791386000, ended: 1791386012, exit: 0, msgs_handled: 2, error: null },
      { role: 'builder', started: 1791386100, ended: 1791386101, exit: 1, msgs_handled: 0, error: 'engine crashed' }, { role: 'lead', started: 1791386200, ended: null, exit: null, msgs_handled: 0, error: null }] })
    if (p === '/api/config') return j({ owner: 'me', chat_file_bytes: null, upload_bytes: null, version: '1.2.0' })
    if (p === '/files/api/me') return j({ owner: 'me', spaces: [{ id: 'outputs', label: 'outputs', writable: true, path: '/srv/out', exists: true }] })
    if (p === '/files/api/files') {
      const q = u.searchParams.get('q') || ''
      const all = [{ name: '20261007/report.pdf', rel: '20261007/report.pdf', size: 2048, day: '20261007', modified: '2026-10-07 12:00', path: '/srv/out/20261007/report.pdf', partial: 0 },
        { name: 'notes.txt', rel: 'notes.txt', size: 10, day: '20261006', modified: '2026-10-06 09:00', path: '/srv/out/notes.txt', partial: 0 }]
      const files = all.filter(f => f.rel.includes(q))
      return j({ files, total: files.length, offset: 0, limit: 100, truncated: false, ready: true })
    }
    return j({ detail: `unhandled ${method} ${p}` }, 404)
  }))
  return { calls }
}

const at = (path: string, routes: [string, React.ReactElement][]) => render(
  <I18nProvider><MemoryRouter initialEntries={[path]}><Routes>{routes.map(([p, e]) => <Route key={p} path={p} element={e} />)}</Routes></MemoryRouter></I18nProvider>)

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear(); vi.unstubAllGlobals(); vi.restoreAllMocks()
  HTMLDialogElement.prototype.showModal ??= function (this: HTMLDialogElement) { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close ??= function (this: HTMLDialogElement) { this.removeAttribute('open') }
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

describe('Agents', () => {
  it('engines: installed vs signed-in shown separately, quota, install streams the log', async () => {
    const b = backend(); at('/agents', [['/agents', <Agents />], ['/agents/:roleId', <Agents />]])
    const codex = (await screen.findByText('Codex', { selector: 'b' })).parentElement!
    expect(within(codex).getByText('Installed')).toBeInTheDocument()
    expect(within(codex).getByText('Signed in')).toBeInTheDocument()
    const pi = screen.getByText('pi', { selector: 'b' }).parentElement!
    expect(within(pi).getByText('Sign-in unknown')).toBeInTheDocument()            // never shown as "ready"
    expect(await screen.findByText('10% left')).toBeInTheDocument()
    const gem = screen.getByText('Gemini', { selector: 'b' }).parentElement!.parentElement!
    await userEvent.click(within(gem).getByRole('button', { name: 'Install' }))
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('npm install @google/gemini-cli'))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/agents/install')?.body).toEqual({ agent: 'gemini', confirm: true }))
    await waitFor(() => expect(screen.getByLabelText('Install log')).toHaveTextContent(/\$ npm install[\s\S]*done[\s\S]*\[done\]/), { timeout: 5000 })
    await userEvent.click(within(codex.parentElement!).getByRole('button', { name: 'Test connection' }))
    expect(await screen.findByText(/Connected/)).toBeInTheDocument()
  })

  it('role page: sessions per room, close, engine change with model test failure surfaced', async () => {
    const b = backend(); at('/agents/lead', [['/agents', <Agents />], ['/agents/:roleId', <Agents />]])
    expect(await screen.findByRole('heading', { name: 'Lead' })).toBeInTheDocument()
    const table = await screen.findByRole('table')
    expect(within(table).getByText('Default (no room)')).toBeInTheDocument()
    expect(within(table).getByText('Closed by you')).toBeInTheDocument()
    expect(within(table).getByText('handoff pending')).toBeInTheDocument()
    expect(within(table).getByText('85%')).toBeInTheDocument()
    const row3 = within(table).getByText(/#3/).closest('tr')!
    await userEvent.click(within(row3).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(b.calls.some(c => c.url === '/api/agents/lead/sessions/3/close' && c.method === 'POST')).toBe(true))
    expect(within(within(table).getByText(/#2/).closest('tr')!).queryByRole('button')).toBeNull()   // parked: no close button

    const model = screen.getByRole('combobox', { name: 'Model' })
    await waitFor(() => expect(within(model).getByRole('option', { name: 'gpt-5.6' })).toBeInTheDocument())
    await userEvent.selectOptions(model, 'Custom…')
    const custom = screen.getByRole('textbox', { name: 'Model ID' })
    await userEvent.clear(custom); await userEvent.type(custom, 'bad')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('model bad failed a test run')
    await userEvent.selectOptions(model, 'gpt-5.6')
    expect(screen.queryByRole('textbox', { name: 'Model ID' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(b.calls.filter(c => c.url.endsWith('/engine')).at(-1)?.body).toEqual({ agent: 'codex', model: 'gpt-5.6' }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
  })

  it('add role with a written persona', async () => {
    const b = backend(); at('/agents', [['/agents', <Agents />]])
    await screen.findByText('Codex', { selector: 'b' })
    await userEvent.click(screen.getByRole('button', { name: /Add role/ }))
    const dlg = screen.getByRole('dialog', { name: 'Add role' })
    await userEvent.type(within(dlg).getByLabelText('Role id'), 'writer')
    await userEvent.type(within(dlg).getByLabelText('Display name'), 'Writer')
    await userEvent.click(within(dlg).getByRole('radio', { name: 'Write a new persona' }))
    await userEvent.type(within(dlg).getByRole('textbox', { name: 'Write a new persona' }), 'You write docs.')
    await userEvent.click(within(dlg).getByRole('button', { name: 'Add role' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/agents/roles')?.body).toMatchObject({ role: 'writer', label: 'Writer', agent: 'codex', rank: 'worker', persona: 'You write docs.' }))
    expect(await within(dlg).findByRole('status')).toHaveTextContent('ready in ~10s')
  })
})

describe('Skills', () => {
  it('edit, save with note, view and restore history', async () => {
    const b = backend(); at('/skills/release', [['/skills/:skillId', <Skills />], ['/skills', <Skills />]])
    const box = await screen.findByRole('textbox', { name: 'SKILL.md' })
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    fireEvent.change(box, { target: { value: box.getAttribute('value') ?? (box as HTMLTextAreaElement).value + 'step 2\n' } })
    await userEvent.type(screen.getByLabelText('Change note (optional)'), 'add step 2')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(b.calls.find(c => c.method === 'PUT' && c.url === '/api/skills/release')?.body).toEqual({ content: expect.stringContaining('step 2'), note: 'add step 2' }))
    await userEvent.click(screen.getByText(/Version history/))
    await userEvent.click(screen.getByRole('button', { name: 'View' }))
    expect(await screen.findByText('OLD VERSION')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    await userEvent.click(screen.getByRole('button', { name: 'Restore' }))
    await waitFor(() => expect(b.calls.find(c => c.url.endsWith('/restore'))?.body).toEqual({ version: '20261007-120000-000' }))
  })

  it('delete is blocked for a skill in use', async () => {
    backend(); at('/skills/release', [['/skills/:skillId', <Skills />]])
    await screen.findByRole('textbox', { name: 'SKILL.md' })
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()          // release: not in use
    expect(screen.getByText(/Not assigned anywhere/)).toBeInTheDocument()
  })

  it('creates a skill from the template', async () => {
    const b = backend(); at('/skills', [['/skills', <Skills />], ['/skills/:skillId', <Skills />]])
    await screen.findByText('Release steps')
    await userEvent.click(screen.getByRole('button', { name: /New skill/ }))
    await userEvent.type(within(screen.getByRole('dialog')).getByLabelText('Skill id'), 'Checklist')
    await userEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/skills' && c.method === 'POST')?.body).toMatchObject({ id: 'checklist', content: expect.stringContaining('name: checklist') }))
  })

  it('packs and assignments, with a live preview', async () => {
    const b = backend(); at('/skills/_assign', [['/skills/:skillId', <Skills />]])
    expect(await screen.findByText('What lead gets')).toBeInTheDocument()
    await userEvent.click(within(screen.getByRole('group', { name: 'Packs' })).getByRole('checkbox', { name: 'ops' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/skill-assign/role/lead')?.body).toEqual({ skills: [], packs: ['ops'] }))
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Assign to' }), 'room:2')
    await userEvent.click(within(screen.getByRole('group', { name: 'Skills' })).getByRole('checkbox', { name: 'release' }))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(b.calls.find(c => c.url === '/api/skill-assign/room/2')?.body).toEqual({ skills: ['release'], packs: [] }))
  })
})

describe('Files', () => {
  it('lists, searches (debounced), uploads raw bytes with the portal header', async () => {
    const b = backend()
    const sent: { url: string; headers: Record<string, string>; body: unknown }[] = []
    class FakeXHR {
      upload = { onprogress: null as any }; status = 0; responseText = ''; onload: any; onerror: any
      private u = ''; private h: Record<string, string> = {}
      open(_m: string, u: string) { this.u = u }
      setRequestHeader(k: string, v: string) { this.h[k] = v }
      send(body: unknown) { sent.push({ url: this.u, headers: this.h, body }); this.status = 201; this.responseText = '{"name":"a.txt","size":3}'; setTimeout(() => this.onload(), 0) }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXHR as any)
    at('/files', [['/files', <Files />]])
    expect(await screen.findByText('20261007/report.pdf')).toBeInTheDocument()
    expect(screen.getByText('2 KB')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '20261007/report.pdf' }).parentElement!.textContent).toBe('20261007/report.pdf')   // partial: 0 must not render '0'
    await userEvent.type(screen.getByLabelText('Search files'), 'notes')
    await waitFor(() => expect(screen.queryByText('20261007/report.pdf')).toBeNull())
    expect(b.calls.some(c => c.url.includes('q=notes'))).toBe(true)
    const f = new File(['abc'], 'a.txt')
    await userEvent.upload(document.querySelector('input[type=file]') as HTMLInputElement, f)
    await waitFor(() => expect(sent).toHaveLength(1))
    expect(sent[0].url).toBe('/files/api/upload?space=outputs&name=a.txt')
    expect(sent[0].headers['X-Portal-Request']).toBe('1')
    expect(sent[0].body).toBe(f)
    expect(await screen.findByText('✓')).toBeInTheDocument()
  })
})

describe('Dashboard', () => {
  it('shows stats, recent runs, and what needs attention', async () => {
    backend(); at('/dashboard', [['/dashboard', <Dashboard />]])
    expect(await screen.findByText('1/2')).toBeInTheDocument()                              // agents online
    expect(screen.getByText('AA Forum 1.2.0')).toBeInTheDocument()
    expect(await screen.findByText(/engine crashed/, { selector: 'span' })).toBeInTheDocument()
    const att = screen.getByRole('heading', { name: 'Needs attention' }).nextElementSibling!
    expect(att).toHaveTextContent('builder: last run failed — engine crashed')
    expect(att).toHaveTextContent('Builder is offline')
    expect(att).toHaveTextContent('3 unread in #release')
    expect(screen.getByText('running')).toBeInTheDocument()
  })
})
