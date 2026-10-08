"""AA Forum Mac 移植：原版功能逐項驗證（FastAPI TestClient，不需真的 agent）。

涵蓋：登入（首帳號免通行碼／通行碼重設／帳密錯誤）、建群、留言、@提及、回覆、引用與程式碼不觸發提及、
重試鍵冪等、圖片與檔案附件、按讚、讀取進度與逐則確認已讀、邀請（理由＋入群公告＋讀取起點）、
凍結／解凍（拒發言、取消待送）、封存／垃圾桶／恢復、改名、釘選、資料夾、全域與群內搜尋（含 @我）、
定位留言、機械提醒（首則、週期限制、存 .md、內建範本、停用）、群通知開關、
通知改走 mbox（含全文、無長度限制、提醒）、agents 名單＝roles.json、成員血量（headless）、
特務控制 status、額度 API、傳檔站。
"""
import contextlib
import base64
import io
import json
import os
import sys
import time
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp('zk')
    os.environ['AAF_SERVER_STATE'] = str(base / 'state')
    os.environ['AAF_REMINDER_LIBRARY'] = str(base / 'lib')
    os.environ['AAF_FILES_ROOT'] = str(base / 'outputs')
    os.environ['MBOX_HOME'] = str(base / 'mbox')
    os.environ['MBOX_ROLES'] = str(ROOT / 'tests' / 'fixtures' / 'roles.five.json')
    (base / 'outputs').mkdir()
    (base / 'outputs' / 'hello.txt').write_text('hello outputs 搜尋字')
    sys.path.insert(0, str(ROOT / 'server'))
    sys.path.insert(0, str(ROOT))
    for m in [m for m in list(sys.modules) if m in ('app', 'runtime', 'member_health',
                                                      'account_quota', 'files_portal', 'files_storage')]:
        del sys.modules[m]
    import runtime as rt
    from mbox.core import Store
    rt.MBOX_HOME.mkdir(parents=True, exist_ok=True)
    st = Store(rt.MBOX_HOME / 'mbox.sqlite3')
    for r in rt.agent_roles():
        st.add_agent(r, 'x', 'lead' if r == 'lead' else 'worker')
    import threading
    from http.server import ThreadingHTTPServer
    from mbox.broker import make_handler
    token = st.add_agent('server', 'system', 'human')
    tokens = rt.MBOX_HOME / 'tokens'
    tokens.mkdir()
    (tokens / 'server').write_text(token)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(st))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    previous_url = os.environ.get('MBOX_URL')
    os.environ['MBOX_URL'] = f'http://127.0.0.1:{server.server_port}'
    import app
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    client.__enter__()
    yield dict(app=app, c=client, rt=rt, st=st, base=base)
    client.__exit__(None, None, None)
    server.shutdown()
    server.server_close()
    thread.join()
    if previous_url is None:
        os.environ.pop('MBOX_URL', None)
    else:
        os.environ['MBOX_URL'] = previous_url
    st.db.close()


def H(tok):
    return {'Authorization': 'Bearer ' + tok}


@pytest.fixture(scope='module')
def owner(env):
    c = env['c']
    assert c.get('/api/setup-state').json() == {'has_accounts': False}
    r = c.post('/setup', json={'username': 'owner', 'password': 'pw-12345678'})
    assert r.status_code == 200, r.text
    r = c.post('/login', json={'username': 'owner', 'password': 'pw-12345678'})
    assert r.status_code == 200
    return r.json()['token']


def agent_tok(env, a):
    return env['app'].credentials(a)[a]


def deliver_all(env):
    import contextlib
    app = env['app']
    with contextlib.closing(app.connect()) as db, db:
        db.execute("UPDATE outbox SET next_try=0 WHERE status='pending'")
    for _ in range(50):
        app.deliver_once()
    with contextlib.closing(app.connect()) as db:
        stuck = db.execute("SELECT recipient,detail FROM outbox WHERE status='pending' AND attempts>0").fetchall()
    assert not stuck, [dict(r) for r in stuck]


def test_login_rules(env, owner):
    c = env['c']
    assert c.post('/login', json={'username': 'owner', 'password': 'wrong-pass'}).status_code == 401
    assert c.post('/login', json={'username': 'other', 'password': 'pw-12345678'}).status_code == 401
    # 已有帳號：重設要通行碼
    assert c.post('/setup', json={'username': 'owner', 'password': 'newpass-123'}).status_code == 401
    pc = env['app'].login_passcode()
    assert c.post('/setup', json={'username': 'owner', 'password': 'newpass-123', 'passcode': pc}).status_code == 200
    assert c.post('/login', json={'passcode': pc}).status_code == 200
    assert c.post('/login', json={'username': 'owner', 'password': 'newpass-123'}).json()['token'] == owner
    assert c.post('/setup', json={'username': 'owner', 'password': 'pw-12345678', 'passcode': pc}).status_code == 200
    assert c.get('/api/rooms').status_code == 401
    assert c.get('/', follow_redirects=False).headers['location'] == '/app/'
    for old, new in (('/login', '/app/'), ('/control', '/app/dashboard'), ('/agents', '/app/agents'), ('/skills', '/app/skills'), ('/files/', '/app/files')):
        r = c.get(old, follow_redirects=False)
        assert r.status_code in (307, 308) and r.headers['location'] == new, old


def test_agents_roster_from_roles_json(env, owner):
    people = env['c'].get('/api/agents', headers=H(owner)).json()
    ids = {p['id'] for p in people}
    assert {'lead', 'builder', 'reviewer', 'debugger', 'guardian', 'Owner'} <= ids
    labels = {p['id']: p['label'] for p in people}
    assert labels['lead'] == '組長 lead' and labels['Owner'] == 'owner'
    assert all(p['online'] for p in people if p['id'] != 'Owner')  # headless 角色視為可用


@pytest.fixture(scope='module')
def room(env, owner):
    r = env['c'].post('/api/rooms', headers=H(owner), json={'name': '移植驗證群', 'members': ['lead', 'builder']})
    assert r.status_code == 200
    return r.json()['id']


def test_post_mention_reply_and_mbox_delivery(env, owner, room):
    c, st = env['c'], env['st']
    long_body = '@lead 請看這段\n第二行\n' + '長' * 5000
    r = c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': long_body, 'client_id': 'k1'})
    mid = r.json()['id']
    # 冪等
    assert c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': long_body, 'client_id': 'k1'}).json()['id'] == mid
    assert c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': 'x', 'client_id': 'k1'}).status_code == 409
    # 引用與程式碼不觸發提及
    r2 = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
                json={'body': '> @builder 引用\n`@builder` 程式碼', 'client_id': 'k2', 'reply_to': mid}).json()['id']
    msgs = c.get(f'/api/rooms/{room}/messages', headers=H(owner)).json()['messages']
    by = {m['id']: m for m in msgs}
    assert by[mid]['mentions'] == ['lead'] and by[r2]['mentions'] == [] and by[r2]['reply_to'] == mid
    deliver_all(env)
    inbox = st.inbox({'id': 'lead'})
    assert inbox, 'lead 應收到 mbox 通知'
    text = inbox[-1]['body']
    assert '有人 @ 你' in text and '第二行' in text and '截斷' in text  # 全文（上限 4000 字）＋換行保留
    assert inbox[-1]['sender'] == 'server'
    assert f'aaf-chat post {room} --reply' in text and f'aaf-chat confirm-read {room}' in text
    assert 'env AAF_SERVER_STATE=' not in text and 'cli.py' not in text
    out = c.get(f'/api/rooms/{room}/messages', headers=H(owner)).json()['messages']
    assert all(d['status'] == 'queued' for m in out for d in m['delivery'])
    assert {m['id'] for m in st.inbox({'id': 'builder'}, unread_only=False)}  # builder 也收到（無 @ 的版本）


def test_agent_cli_roundtrip(env, owner, room):
    """角色用原版 cli.py 回覆：read / post / confirm-read / like。"""
    import subprocess
    app = env['app']
    srv_env = dict(os.environ, AAF_SERVER_STATE=str(app.STATE))
    # 用 TestClient 代替真實 HTTP：直接以 token 呼叫等價 API
    c = env['c']; tok = agent_tok(env, 'lead')
    rooms = c.get('/api/rooms', headers=H(tok)).json()
    assert any(r['id'] == room for r in rooms)
    rr = c.post(f'/api/rooms/{room}/messages', headers=H(tok), json={'body': '收到，我來拆任務', 'client_id': 'a1', 'reply_to': 1})
    assert rr.status_code == 200
    last = c.get(f'/api/rooms/{room}/messages', headers=H(tok)).json()['messages'][-1]['id']
    assert c.post(f'/api/rooms/{room}/read', headers=H(tok), json={'through': last}).json()['ok']
    assert c.post(f'/api/rooms/{room}/confirm-read', headers=H(tok), json={'messages': [1]}).json()['confirmed'] == [1]
    assert c.post(f'/api/rooms/{room}/messages/1/like', headers=H(tok), json={'liked': True}).json()['liked']
    m1 = c.get(f'/api/rooms/{room}/messages', headers=H(owner)).json()['messages'][0]
    assert [x['id'] for x in m1['likes']] == ['lead'] and [x['id'] for x in m1['read_by']] == ['lead']
    # 非成員看不到
    assert c.get(f'/api/rooms/{room}/messages', headers=H(agent_tok(env, 'reviewer'))).status_code == 403


def test_image_and_file_attachments(env, owner, room):
    from PIL import Image
    c = env['c']
    buf = io.BytesIO(); Image.new('RGB', (8, 8), 'red').save(buf, format='PNG')
    r = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
               json={'body': '', 'client_id': 'img1', 'image': base64.b64encode(buf.getvalue()).decode()})
    mid = r.json()['id']
    img = c.get(f'/api/rooms/{room}/messages/{mid}/image', headers=H(owner))
    assert img.status_code == 200 and img.headers['content-type'] == 'image/png'
    r = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
               json={'body': '附檔', 'client_id': 'f1', 'file': {'name': '報告.txt', 'content': base64.b64encode('內容'.encode()).decode()}})
    fid = r.json()['id']
    f = c.get(f'/api/rooms/{room}/messages/{fid}/file', headers=H(owner))
    assert f.content == '內容'.encode()
    bad = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
                 json={'body': '', 'client_id': 'f2', 'file': {'name': '../x', 'content': ''}})
    assert bad.status_code == 422
    # 通知信要寫明附件與下載指令，不然 agent 不知道有檔（e2e C4 實測發現）
    r = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
               json={'body': '@lead 請讀附件', 'client_id': 'f3', 'file': {'name': 'note.txt', 'content': base64.b64encode(b'code ZEBRA').decode()}})
    nid = r.json()['id']
    deliver_all(env)
    text = [m for m in env['st'].inbox({'id': 'lead'}) if f'#{nid} ' in m['body']][-1]['body']
    assert f'📎 附件 note.txt' in text and f'aaf-chat download {room} {nid}' in text


def test_invite_freeze_manage(env, owner, room):
    c, st = env['c'], env['st']
    tok = agent_tok(env, 'lead')
    before = c.get(f'/api/rooms/{room}/messages', headers=H(owner)).json()['messages'][-1]['id']
    r = c.post(f'/api/rooms/{room}/members', headers=H(tok), json={'agent': 'reviewer', 'reason': '需要審查'})
    assert r.json()['added']
    reviewer_room = [x for x in c.get('/api/rooms', headers=H(agent_tok(env, 'reviewer'))).json() if x['id'] == room][0]
    assert reviewer_room['last_read'] == before  # 新人從入群告知開始收
    assert c.post(f'/api/rooms/{room}/members', headers=H(tok), json={'agent': 'reviewer', 'reason': '再邀'}).json()['added'] is False
    # 凍結：拒發言、提醒停用、公告
    assert c.post(f'/api/rooms/{room}/freeze', headers=H(tok), json={'frozen': True, 'reason': '討論結束'}).json()['changed']
    assert c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': 'x', 'client_id': 'fz'}).status_code == 409
    assert c.post(f'/api/rooms/{room}/messages/1/like', headers=H(owner), json={'liked': True}).status_code == 409
    assert c.get(f'/api/rooms/{room}/messages', headers=H(owner)).status_code == 200  # 仍可讀
    assert c.post(f'/api/rooms/{room}/freeze', headers=H(tok), json={'frozen': False, 'reason': '繼續'}).json()['state'] == 'active'
    # 管理：改名／釘選／資料夾／封存／垃圾桶／恢復（Owner 限定）
    assert c.post(f'/api/rooms/{room}/manage', headers=H(tok), json={'name': 'x'}).status_code == 403
    assert c.post(f'/api/rooms/{room}/manage', headers=H(owner), json={'name': '移植驗證群2'}).json()['ok']
    assert c.post(f'/api/rooms/{room}/manage', headers=H(owner), json={'pinned': True}).json()['pinned']
    assert c.post('/api/folders', headers=H(owner), json={'name': '專案'}).json()['ok']
    assert c.post(f'/api/rooms/{room}/manage', headers=H(owner), json={'folder': '專案'}).json()['folder'] == '專案'
    assert c.get('/api/folders', headers=H(owner)).json() == ['專案']
    assert c.post('/api/folders', headers=H(owner), json={'name': '專案B', 'original': '專案'}).json()['ok']
    r0 = [x for x in c.get('/api/rooms', headers=H(owner)).json() if x['id'] == room][0]
    assert r0['folder'] == '專案B' and r0['pinned'] == 1 and r0['name'] == '移植驗證群2'
    assert c.post(f'/api/rooms/{room}/manage', headers=H(owner), json={'action': 'archive'}).json()['state'] == 'archived'
    assert c.post(f'/api/rooms/{room}/manage', headers=H(owner), json={'action': 'delete'}).json()['state'] == 'deleted'
    assert c.get(f'/api/rooms/{room}/messages', headers=H(tok)).status_code == 404
    assert c.post(f'/api/rooms/{room}/manage', headers=H(owner), json={'action': 'restore'}).json()['state'] == 'active'
    assert c.post(f'/api/rooms/{room}/notify', headers=H(owner), json={'enabled': True}).json()['ok']


def test_search_and_location(env, owner, room):
    c = env['c']
    g = c.get('/api/search', headers=H(owner), params={'q': '請看這段'}).json()
    assert g['messages'] and g['messages'][0]['room'] == room
    rs = c.get(f'/api/rooms/{room}/search', headers=H(agent_tok(env, 'lead')), params={'mentioned': True}).json()
    assert rs['results'] and '@lead' in rs['results'][-1]['snippet']
    mid = g['messages'][0]['id']
    assert c.get(f'/api/messages/{mid}/location', headers=H(owner)).json()['room'] == room
    assert c.get(f'/api/messages/{mid}/location', headers=H(agent_tok(env, 'debugger'))).status_code == 404


def test_reminders(env, owner, room):
    c, app, st = env['c'], env['app'], env['st']
    tok = agent_tok(env, 'lead')
    assert c.post(f'/api/rooms/{room}/reminder', headers=H(owner), json={'body': 'x', 'minutes': 20, 'enabled': True}).status_code == 422
    files = c.get(f'/api/rooms/{room}/reminder-files', headers=H(owner)).json()
    assert {'template_standup.md', 'template_standup.zh-TW.md'} <= set(files['files'])
    tpl = c.get(f'/api/rooms/{room}/reminder-files', headers=H(owner), params={'name': 'template_standup.md'}).json()['body']
    assert 'Done when' in tpl
    assert c.post(f'/api/rooms/{room}/reminder-files', headers=H(owner), json={'name': '我的規範', 'body': tpl}).json()['name'] == '我的規範.md'
    assert c.post(f'/api/rooms/{room}/reminder-files', headers=H(owner), json={'name': '我的規範', 'body': tpl}).status_code == 409
    assert c.post(f'/api/rooms/{room}/reminder-files', headers=H(owner), json={'name': '../x', 'body': 'x'}).status_code == 422
    before = len(st.inbox({'id': 'builder'}, unread_only=False, mark=False))
    r = c.post(f'/api/rooms/{room}/reminder', headers=H(tok), json={'body': '■ 本房規範\n每輪回報進度\n@builder 注意', 'minutes': 10, 'enabled': True}).json()
    assert r['sent_initial'] and r['next_due'] % 600 == 0
    # 同設定重試不重發
    assert not c.post(f'/api/rooms/{room}/reminder', headers=H(tok), json={'body': '■ 本房規範\n每輪回報進度\n@builder 注意', 'minutes': 10, 'enabled': True}).json()['sent_initial']
    deliver_all(env)
    got = st.inbox({'id': 'builder'}, unread_only=False, mark=False)
    rem = [m for m in got[before:] if '機械提醒' in m['body']]
    assert rem and '每輪回報進度' in rem[-1]['body'] and '\n' in rem[-1]['body']  # 全文一則、不分段
    # 到期：把 next_due 撥回過去，worker 產生下一則（含統計）
    import contextlib
    with contextlib.closing(app.connect()) as db, db:
        db.execute('UPDATE reminders SET next_due=? WHERE room=?', (time.time() - 1, room))
    app.run_reminders()
    deliver_all(env)
    msgs = c.get(f'/api/rooms/{room}/messages', headers=H(owner)).json()['messages']
    assert sum('【機械提醒】' in m['body'] for m in msgs) == 2 and '統計區間' in msgs[-1]['body']
    assert c.post(f'/api/rooms/{room}/reminder/stop', headers=H(tok), json={}).json()['ok']
    assert c.get(f'/api/rooms/{room}/reminder', headers=H(owner)).json()['enabled'] == 0


def test_notify_toggle_cancels(env, owner, room):
    c, app = env['c'], env['app']
    assert c.post(f'/api/rooms/{room}/notify', headers=H(owner), json={'enabled': False}).json()['ok']
    mid = c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': '@lead 安靜', 'client_id': 'q1'}).json()['id']
    deliver_all(env)
    m = [x for x in c.get(f'/api/rooms/{room}/messages', headers=H(owner)).json()['messages'] if x['id'] == mid][0]
    assert {d['status'] for d in m['delivery']} == {'disabled'}
    assert c.post(f'/api/rooms/{room}/notify', headers=H(owner), json={'enabled': True}).json()['ok']


def test_health_control_quota_files(env, owner, room):
    c = env['c']
    h = c.get(f'/api/rooms/{room}/health', headers=H(owner)).json()['members']
    lead = [m for m in h if m['agent'] == 'lead'][0]
    assert 'headless' in lead['engine'] and lead['activity'] in ('idle', 'working')
    assert c.get(f'/api/rooms/{room}/agents/lead/control', headers=H(owner)).status_code == 404   # tmux room control removed
    q = c.get('/api/account-quota', headers=H(owner))
    # 列出的是「本機已安裝」的 agent（依環境而定）；只驗格式：每筆都有額度、說明或錯誤，不會空白
    accts = q.json()['accounts']
    assert q.status_code == 200 and all(a.get('windows') or a.get('note') or a.get('error') for a in accts)   # 沒裝任何 agent 時為空清單
    assert c.get('/api/account-quota', headers=H(agent_tok(env, 'lead'))).status_code == 403
    # 任務 #27：模型清單 API 僅 Owner
    import os
    hh = env['base'] / 'hermes27'
    hh.mkdir(exist_ok=True)
    (hh / 'config.yaml').write_text('model:\n  default: m1\n  provider: copilot\n')
    old = os.environ.get('HERMES_HOME')
    os.environ['HERMES_HOME'] = str(hh)
    try:
        mc = c.get('/api/model-catalog', headers=H(owner))
        assert mc.status_code == 200 and mc.json()['default'] == {'provider': 'copilot', 'model': 'm1'}
        assert c.get('/api/model-catalog', headers=H(agent_tok(env, 'lead'))).status_code == 403
    finally:
        os.environ.pop('HERMES_HOME') if old is None else os.environ.__setitem__('HERMES_HOME', old)
    assert c.get('/files/api/me', headers=H(owner)).status_code == 200


def test_notify_duplicate_contract_and_outbox_retry(env, owner, monkeypatch):
    import contextlib
    c, app, rt, st = env['c'], env['app'], env['rt'], env['st']
    key = 'notify-contract-retry'
    before = st.unread_count('builder')
    assert rt.notify_role('builder', 'retry contract', key) == 'queued'
    assert rt.notify_role('builder', 'retry contract', key) == 'duplicate'
    assert st.unread_count('builder') == before + 1

    room = c.post('/api/rooms', headers=H(owner),
                  json={'name': '重試驗證', 'members': ['builder']}).json()['id']
    mid = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
                 json={'body': 'retry delivery', 'client_id': 'retry-delivery'}).json()['id']
    with contextlib.closing(app.connect()) as db, db:
        db.execute('UPDATE rooms SET notify=1 WHERE id=?', (room,))
        db.execute("UPDATE outbox SET status='pending',next_try=0 WHERE message=?", (mid,))
    real_notify = rt.notify_role
    def accepted_then_failed(*args, **kwargs):
        assert real_notify(*args, **kwargs) == 'queued'
        raise RuntimeError('lost response after acceptance')
    before = st.unread_count('builder')
    monkeypatch.setattr(rt, 'notify_role', accepted_then_failed)
    app.deliver_once()
    with contextlib.closing(app.connect()) as db, db:
        row = db.execute('SELECT status,attempts FROM outbox WHERE message=?', (mid,)).fetchone()
        assert tuple(row) == ('pending', 1)
        db.execute('UPDATE outbox SET next_try=0 WHERE message=?', (mid,))
    monkeypatch.setattr(rt, 'notify_role', real_notify)
    app.deliver_once()
    with contextlib.closing(app.connect()) as db:
        row = db.execute('SELECT status,attempts,detail FROM outbox WHERE message=?', (mid,)).fetchone()
        assert tuple(row) == ('queued', 0, 'status=duplicate mbox')
    assert st.unread_count('builder') == before + 1


def test_messages_batched_relations_pagination_and_query_count(env, owner, monkeypatch):
    import contextlib
    c, app = env['c'], env['app']
    room = c.post('/api/rooms', headers=H(owner),
                  json={'name': '批次關聯驗證', 'members': ['builder', 'lead']}).json()['id']
    other_room = c.post('/api/rooms', headers=H(owner),
                        json={'name': '其他群', 'members': ['lead']}).json()['id']
    with contextlib.closing(app.connect()) as db, db:
        ids = []
        for i in range(501):
            mid = db.execute("""INSERT INTO messages(room,author,body,mentions,created,client_id)
                                VALUES(?, 'Owner', ?, '[]', 1, ?)""",
                             (room, f'message {i}', f'batch-relations-{i}')).lastrowid
            ids.append(mid)
        first, last = ids[0], ids[-1]
        db.execute('INSERT INTO images VALUES(?,?,?,?,?,?)', (first, 'image/png', 8, 9, 'image-digest', b'image'))
        db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (last, 'last.txt', 4, 'file-digest', b'last'))
        for agent in ('builder', 'lead'):
            db.execute('INSERT INTO likes VALUES(?,?,?)', (first, agent, 2))
            db.execute('INSERT INTO read_receipts VALUES(?,?,?)', (first, agent, 3))
            db.execute('INSERT INTO outbox(message,recipient,status,next_try) VALUES(?,?,?,?)',
                       (first, agent, 'queued', 0))
        db.execute("""INSERT INTO messages(room,author,body,mentions,created,client_id)
                      VALUES(?, 'Owner', 'other room', '[]', 1, 'batch-other-room')""", (other_room,))
    queries = []
    real_connect = app.connect
    def traced_connect():
        db = real_connect()
        db.set_trace_callback(lambda sql: queries.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
        return db
    monkeypatch.setattr(app, 'connect', traced_connect)
    result = c.get(f'/api/rooms/{room}/messages', headers=H(owner)).json()
    full_count = len(queries)
    assert len(result['messages']) == 500 and result['has_more']
    assert len({row['id'] for row in result['messages']}) == 500
    first_row = result['messages'][0]
    assert first_row['image'] == dict(mime='image/png', width=8, height=9,
                                    url=f'/api/rooms/{room}/messages/{first}/image')
    assert first_row['file'] is None
    assert [a['id'] for a in first_row['likes']] == ['builder', 'lead']
    assert [a['id'] for a in first_row['read_by']] == ['builder', 'lead']
    assert all(a['created'] == 3 for a in first_row['read_by'])
    assert [a['recipient'] for a in first_row['delivery']] == ['builder', 'lead']
    assert all(a['status'] == 'queued' for a in first_row['delivery'])
    assert result['messages'][1]['likes'] == [] and result['messages'][1]['read_by'] == []
    queries.clear()
    result = c.get(f'/api/rooms/{room}/messages?after={ids[-2]}', headers=H(owner)).json()
    assert len(queries) == full_count <= 12  # Query count stays constant for 1 vs 500 messages.
    assert not result['has_more'] and len(result['messages']) == 1
    assert result['messages'][0]['id'] == last
    assert result['messages'][0]['file'] == dict(name='last.txt', size=4,
                                              url=f'/api/rooms/{room}/messages/{last}/file')
    assert result['messages'][0]['image'] is None and result['messages'][0]['delivery'] == []
    result = c.get(f'/api/rooms/{room}/messages?after={last}', headers=H(owner)).json()
    assert result['messages'] == [] and not result['has_more']
    assert result['members']
    assert c.get(f'/api/rooms/{other_room}/messages', headers=H(agent_tok(env, 'builder'))).status_code == 403


def test_priority_classified_once_owner_mentions_task_and_digest(env, owner):
    import contextlib
    app, c = env['app'], env['c']
    room = c.post('/api/rooms', headers=H(owner), json={'name': 'priority', 'members': ['lead', 'builder']}).json()['id']
    cases = [(owner, {'body': 'owner without mention'}, 'must', 'sender=owner'),
             (agent_tok(env, 'lead'), {'body': '@builder please'}, 'must', 'mention=@builder'),
             (agent_tok(env, 'lead'), {'body': 'assignment', 'notification_kind': 'task'}, 'must', 'task'),
             (agent_tok(env, 'lead'), {'body': 'FYI'}, 'digest', 'default→digest')]
    for i, (token, body, priority, reason) in enumerate(cases):
        mid = c.post(f'/api/rooms/{room}/messages', headers=H(token), json=dict(body, client_id=f'priority-{i}')).json()['id']
        with contextlib.closing(app.connect()) as db:
            row = db.execute('SELECT priority,priority_reason FROM outbox WHERE message=? AND recipient=?', (mid,'builder')).fetchone()
            assert tuple(row) == (priority,reason)


def test_runs_api_owner_only(env, owner):
    env['st'].record_run_error('scan', 'OperationalError: SQLITE_BUSY')
    response = env['c'].get('/api/runs', headers=H(owner))
    assert response.status_code == 200
    assert response.json()['runs'][0]['error'] == 'OperationalError: SQLITE_BUSY'
    assert env['c'].get('/api/runs', headers=H(agent_tok(env, 'builder'))).status_code == 403


def test_broker_offline_outbox_pending_then_recovers(env, owner, monkeypatch):
    import contextlib
    import socket
    c, app, st = env['c'], env['app'], env['st']
    room = c.post('/api/rooms', headers=H(owner), json={'name': 'broker offline', 'members': ['builder']}).json()['id']
    mid = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
                 json={'body': 'owner without mention', 'client_id': 'broker-offline'}).json()['id']
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        unused_url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    broker_url = os.environ['MBOX_URL']
    before = st.unread_count('builder')
    with app.ROOM_DELIVERY_LOCK:
        with contextlib.closing(app.connect()) as db, db:
            db.execute('UPDATE rooms SET notify=1 WHERE id=?', (room,))
            db.execute("UPDATE outbox SET next_try=999999999999 WHERE message!=? AND status='pending'", (mid,))
            db.execute("UPDATE outbox SET status='pending',next_try=0 WHERE message=?", (mid,))
        monkeypatch.setenv('MBOX_URL', unused_url)
        app.deliver_once()
        with contextlib.closing(app.connect()) as db, db:
            row = db.execute('SELECT status,attempts,detail FROM outbox WHERE message=?', (mid,)).fetchone()
            assert row['status'] == 'pending' and row['attempts'] == 1 and 'URLError' in row['detail']
            db.execute('UPDATE outbox SET next_try=0 WHERE message=?', (mid,))
        assert st.unread_count('builder') == before
        monkeypatch.setenv('MBOX_URL', broker_url)
        app.deliver_once()
        with contextlib.closing(app.connect()) as db:
            assert db.execute('SELECT status FROM outbox WHERE message=?', (mid,)).fetchone()[0] == 'queued'
        assert st.unread_count('builder') == before+1
        row = st.inbox({'id': 'builder'}, mark=False)[-1]
        assert row['priority'] == 'must' and row['priority_reason'] == 'sender=owner'
        assert row['source_room'] == room and json.loads(row['source_messages']) == [mid]


def test_server_has_no_mbox_store_dependency():
    import ast
    for filename in ('runtime.py','app.py','member_health.py'):
        tree = ast.parse((ROOT / 'server' / filename).read_text())
        assert not any(isinstance(node, ast.ImportFrom) and node.module == 'mbox.core' for node in ast.walk(tree))
    source = (ROOT / 'server/runtime.py').read_text()
    assert 'mbox_store' not in source and 'ensure_mbox_identity' not in source


def test_accepted_response_lost_new_message_before_retry(env, owner, monkeypatch):
    import contextlib
    app, rt, st, c = (env[k] for k in ('app', 'rt', 'st', 'c'))
    with app.ROOM_DELIVERY_LOCK:
        room = c.post('/api/rooms', headers=H(owner),
                      json={'name': 'synthetic retry batch', 'members': ['builder']}).json()['id']
        first = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
                       json={'body': 'synthetic first', 'client_id': 'debugger-first'}).json()['id']
        with contextlib.closing(app.connect()) as db, db:
            db.execute('UPDATE rooms SET notify=1 WHERE id=?', (room,))
            db.execute("UPDATE outbox SET next_try=999999999999 WHERE status='pending' AND message!=?", (first,))
            db.execute("UPDATE outbox SET next_try=0 WHERE message=?", (first,))
        original = rt.notify_role
        attempts = []
        def lose_response(*args, **kwargs):
            attempts.append(dict(kwargs))
            assert original(*args, **kwargs) == 'queued'
            raise ConnectionError('synthetic response lost after broker acceptance')
        monkeypatch.setattr(rt, 'notify_role', lose_response)
        app.deliver_once()
        with contextlib.closing(app.connect()) as db:
            assert tuple(db.execute('SELECT status,attempts FROM outbox WHERE message=?', (first,)).fetchone()) == ('pending', 1)
        second = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
                        json={'body': 'synthetic second', 'client_id': 'debugger-second'}).json()['id']
        with contextlib.closing(app.connect()) as db, db:
            db.execute('UPDATE outbox SET next_try=0 WHERE message IN (?,?)', (first, second))
        app.init()  # Reopen/init persisted state before retry, as after restart.
        def retry(*args, **kwargs):
            attempts.append(dict(kwargs))
            return original(*args, **kwargs)
        monkeypatch.setattr(rt, 'notify_role', retry)
        app.deliver_once()
        assert attempts[0] == attempts[1]  # Full payload, classification and key are immutable.
        assert attempts[2]['source_messages'] == [second]
        assert attempts[0]['idem_key'] != attempts[2]['idem_key']
        rows = [r for r in st.inbox({'id': 'builder'}, mark=False) if r['source_room'] == room]
        sources = [json.loads(r['source_messages']) for r in rows]
        print('Accepted broker batches:', sources)
        with contextlib.closing(app.connect()) as db:
            states = [tuple(r) for r in db.execute('SELECT message,status FROM outbox WHERE message IN (?,?)', (first, second))]
        print('Recovered outbox:', states)
        assert all(status == 'queued' for _, status in states)
        assert sum(first in batch for batch in sources) == 1, 'first message delivered in overlapping accepted batches'
        assert sum(second in batch for batch in sources) == 1


def test_must_retry_due_precedes_earlier_digest_and_preserves_frozen_batch(env, owner, monkeypatch):
    import contextlib
    c, app, rt = env['c'], env['app'], env['rt']
    clock = [1000.0]
    monkeypatch.setattr(app.time, 'time', lambda: clock[0])
    original = rt.notify_role
    with app.ROOM_DELIVERY_LOCK:
        room = c.post('/api/rooms', headers=H(owner),
                      json={'name': 'must retry ordering', 'members': ['lead', 'builder']}).json()['id']
        digest = c.post(f'/api/rooms/{room}/messages', headers=H(agent_tok(env, 'lead')),
                        json={'body': 'earlier FYI', 'client_id': 'must-order-digest'}).json()['id']
        must = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
                      json={'body': 'urgent owner', 'client_id': 'must-order-must'}).json()['id']
        with contextlib.closing(app.connect()) as db, db:
            db.execute('UPDATE rooms SET notify=1 WHERE id=?', (room,))
            db.execute("UPDATE outbox SET next_try=999999999999 WHERE status='pending'")
            db.execute("UPDATE outbox SET status='pending',next_try=0 WHERE message=? AND recipient='builder'", (must,))
        frozen = []
        def lost_response(**payload):
            frozen.append(payload)
            assert original(**payload) == 'queued'
            raise ConnectionError('accepted response lost')
        monkeypatch.setattr(rt, 'notify_role', lost_response)
        app.deliver_once()
        with contextlib.closing(app.connect()) as db, db:
            row = db.execute("SELECT status,next_try,batch_id FROM outbox WHERE message=? AND recipient='builder'", (must,)).fetchone()
            assert row['status'] == 'pending' and row['batch_id'] == frozen[0]['idem_key']
            assert row['next_try'] > clock[0]
            # The retry has reached its backoff deadline, but digest was due earlier.
            clock[0] = row['next_try']
            db.execute("UPDATE outbox SET status='pending',next_try=0 WHERE message=? AND recipient='builder'", (digest,))
        calls = []
        def retry(**payload):
            calls.append(payload)
            if payload['priority'] == 'digest':
                raise ConnectionError('digest delivery still unavailable')
            return original(**payload)
        monkeypatch.setattr(rt, 'notify_role', retry)
        app.deliver_once()
        assert [payload['priority'] for payload in calls] == ['must', 'digest']
        assert calls[0] == frozen[0]
        assert calls[1]['source_messages'] == [digest]
        assert calls[1]['idem_key'] != calls[0]['idem_key']
        with contextlib.closing(app.connect()) as db:
            assert tuple(db.execute("SELECT status,detail FROM outbox WHERE message=? AND recipient='builder'", (must,)).fetchone()) == ('queued', 'status=duplicate mbox')
            assert db.execute("SELECT status FROM outbox WHERE message=? AND recipient='builder'", (digest,)).fetchone()[0] == 'pending'


# ── S3：系統公告與熱重載名單（/api/system/*，__system__ 憑證） ──

def test_system_endpoints_auth_announce_roster(env, owner, room, monkeypatch):
    import contextlib
    app, c, rt = env['app'], env['c'], env['rt']
    sys_tok = app.credentials()['__system__']
    # 一般 API 不認 __system__，系統 API 不認 Owner
    assert c.get('/api/rooms', headers=H(sys_tok)).status_code == 401
    assert c.post('/api/system/announce', headers=H(owner), json={'body': 'x', 'key': 'k'}).status_code == 401
    # 公告：貼到使用中的群、不排通知、冪等
    r = c.post('/api/system/announce', headers=H(sys_tok), json={'body': '【系統】測試告警', 'key': 'doctor-t1'})
    assert r.status_code == 200 and room in r.json()['rooms']
    assert c.post('/api/system/announce', headers=H(sys_tok), json={'body': '【系統】測試告警', 'key': 'doctor-t1'}).json()['rooms'] == []
    with contextlib.closing(app.connect()) as db:
        mid = db.execute("SELECT id FROM messages WHERE author='__system__' AND body LIKE '%測試告警%'").fetchone()[0]
        assert db.execute('SELECT COUNT(*) FROM outbox WHERE message=?', (mid,)).fetchone()[0] == 0
    # 名冊熱重載：新增 newbie
    roles = dict(rt.load_roles())
    roles['newbie'] = {'runtime': 'pi', 'rank': 'worker', 'driver': 'pi'}
    monkeypatch.setattr(rt, 'load_roles', lambda: roles)
    r = c.post('/api/system/roster', headers=H(sys_tok), json={'added': ['newbie'], 'removed': []})
    assert r.status_code == 200 and [room, 'newbie'] in r.json()['joined']
    assert c.post('/api/system/roster', headers=H(sys_tok), json={'added': ['newbie'], 'removed': []}).json()['joined'] == []
    assert 'newbie' in {p['id'] for p in c.get('/api/agents', headers=H(owner)).json()}
    # 移除 newbie：標停用、歷史與成員保留、不再排通知
    del roles['newbie']
    r = c.post('/api/system/roster', headers=H(sys_tok), json={'added': [], 'removed': ['newbie']})
    assert [room, 'newbie'] in r.json()['retired']
    people = {p['id']: p for p in c.get('/api/agents', headers=H(owner)).json()}
    assert people['newbie']['active'] == 0 and people['newbie']['online'] is False
    with contextlib.closing(app.connect()) as db:
        assert db.execute("SELECT 1 FROM members WHERE room=? AND agent='newbie'", (room,)).fetchone()
    c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': '@newbie 還在嗎', 'client_id': 's3-retired'})
    with contextlib.closing(app.connect()) as db:
        assert not db.execute("SELECT 1 FROM outbox WHERE recipient='newbie' AND status='pending'").fetchone()
    # 加回：恢復 active
    roles['newbie'] = {'runtime': 'pi', 'rank': 'worker', 'driver': 'pi'}
    c.post('/api/system/roster', headers=H(sys_tok), json={'added': ['newbie'], 'removed': []})
    assert {p['id']: p for p in c.get('/api/agents', headers=H(owner)).json()}['newbie']['active'] == 1


def test_pc4_chat_post_hooks(env, owner, room, tmp_path, monkeypatch):
    """P4：AA Forum 發文經過同一套檢查掛點；角色被擋、使用者（human）身分正確帶入。"""
    from mbox import hooks
    d = tmp_path / 'hooks'
    d.mkdir()
    (d / 'a.py').write_text(
        "def pre(e):\n"
        "    if e['type']=='chat.post' and 'BLOCKME' in e['body']:\n"
        "        return f\"擋 {e['sender']}/{e['rank']}/{e['room']}\"\n"
        "def post(e):\n"
        "    return '記得寫大意' if e['type']=='chat.post' and e['sender']=='lead' else None\n")
    monkeypatch.setenv('AAF_HOOKS', str(d))
    hooks._cache.clear()
    c = env['c']
    tok = agent_tok(env, 'lead')
    r = c.post(f'/api/rooms/{room}/messages', headers=H(tok), json={'body': 'BLOCKME', 'client_id': 'pc4-1'})
    assert r.status_code == 422 and f'擋 lead/None/{room}' in r.text
    r = c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': 'BLOCKME', 'client_id': 'pc4-2'})
    assert r.status_code == 422 and '/human/' in r.text and '擋 Owner' not in r.text
    r = c.post(f'/api/rooms/{room}/messages', headers=H(tok), json={'body': '正常', 'client_id': 'pc4-3'})
    assert r.status_code == 200
    assert hooks.take_notes('lead') == ['記得寫大意']


def test_upload_limits_configurable(env, owner, room, monkeypatch):
    """上傳上限：/api/config 回報；群聊附檔超過上限回 413；0＝不限。"""
    import base64
    app = env['app']; c = env['c']
    cfg = c.get('/api/config', headers=H(owner)).json()
    assert cfg['chat_file_bytes'] == app.MAX_FILE_BYTES and 'upload_bytes' in cfg
    monkeypatch.setattr(app, 'MAX_FILE_BYTES', 10)
    big = base64.b64encode(b'x' * 11).decode()
    r = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
               json={'client_id': 'lim1', 'file': {'name': 'a.bin', 'content': big}})
    assert r.status_code == 413 and '0 MB' in r.text
    monkeypatch.setattr(app, 'MAX_FILE_BYTES', None)
    r = c.post(f'/api/rooms/{room}/messages', headers=H(owner),
               json={'client_id': 'lim2', 'file': {'name': 'a.bin', 'content': big}})
    assert r.status_code == 200


def test_limit_setting_parse(env, monkeypatch, tmp_path):
    app = env['app']
    monkeypatch.setenv('AAF_CHAT_FILE_MB', '0')
    assert app._limit_mb('AAF_CHAT_FILE_MB', 100) is None
    monkeypatch.setenv('AAF_CHAT_FILE_MB', '250')
    assert app._limit_mb('AAF_CHAT_FILE_MB', 100) == 250 * 1024 * 1024
    monkeypatch.setenv('AAF_CHAT_FILE_MB', 'abc')
    assert app._limit_mb('AAF_CHAT_FILE_MB', 100) == 100 * 1024 * 1024


def test_transcript_export(env, owner, room, tmp_path, monkeypatch):
    """逐字稿：每日一檔＋_index；只放留言與系統公告；冪等；凍結群也匯出、刪除群不匯出。"""
    import contextlib, os, transcript
    app = env['app']; c = env['c']
    c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': '逐字稿第一則', 'client_id': 'tr1'})
    mid = c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': '逐字稿第二則', 'client_id': 'tr2'}).json()['id']
    c.post(f'/api/rooms/{room}/messages', headers=H(owner), json={'body': '回覆', 'client_id': 'tr3', 'reply_to': mid})
    c.post(f'/api/rooms/{room}/messages/{mid}/like', headers=H(owner), json={'liked': True})
    out = tmp_path / 'tr'
    monkeypatch.setenv('AAF_TRANSCRIPT_DIR', str(out))
    monkeypatch.setenv('AAF_TZ', 'Asia/Taipei')
    r = app.export_transcripts()
    day_files = sorted((out / str(room)).glob('20*.md'))
    assert day_files and (out / str(room) / '_index.md').exists()
    text = '\n'.join(f.read_text() for f in day_files)
    assert '逐字稿第一則' in text and f'↩ #{mid}' in text and 'Asia/Taipei' in text
    assert '👍' not in text and 'like' not in text.lower()
    idx = (out / str(room) / '_index.md').read_text()
    assert '| 日期 | 則數 | 編號範圍 |' in idx
    # 冪等：再跑一次不寫任何檔
    assert app.export_transcripts()['written'] == []
    # 刪除的群不匯出
    with contextlib.closing(app.connect()) as db, db:
        rid = db.execute("INSERT INTO rooms(name,notify,created,state) VALUES('刪除群',0,0,'deleted')").lastrowid
        db.execute("INSERT INTO messages(room,author,body,mentions,created,client_id) VALUES(?,?,?,?,?,?)",
                   (rid, 'Owner', 'x', '[]', time.time(), 'del1'))
    app.export_transcripts()
    assert not (out / str(rid)).exists()


def test_transcript_scheduler():
    import transcript
    s = transcript.Scheduler(120)
    assert s.due(now=10_000) and not s.due(now=10_000 + 60) and s.due(now=10_000 + 7200)
    assert not transcript.Scheduler(0).due(now=1)


def test_room_auto_approval(env, owner):
    """SPEC-1.1 §1：只有使用者能開關；成員可查；到期含當日、過期視為關；開關寫群內公告；不重複公告。"""
    import app as zk_app
    c = env['c']
    rid = c.post('/api/rooms', headers=H(owner), json={'name': '自動核准群', 'members': ['lead']}).json()['id']
    lead = H(agent_tok(env, 'lead'))
    assert c.get(f'/api/rooms/{rid}/approval', headers=lead).json()['on'] is False
    # agent 不能開
    assert c.post(f'/api/rooms/{rid}/approval', headers=lead, json={'on': True}).status_code == 403
    # 過去日期被拒
    assert c.post(f'/api/rooms/{rid}/approval', headers=H(owner), json={'on': True, 'until': '2000-01-01'}).status_code == 422
    today = zk_app._today()
    r = c.post(f'/api/rooms/{rid}/approval', headers=H(owner), json={'on': True, 'until': today, 'reason': '衝刺週'})
    assert r.status_code == 200 and r.json()['on'] and r.json()['changed'], r.text
    st = c.get(f'/api/rooms/{rid}/approval', headers=lead).json()
    assert st['on'] and st['until'] == today and st['by'] == 'Owner'       # 含當日
    msgs = c.get(f'/api/rooms/{rid}/messages', headers=H(owner)).json()['messages']
    assert any('已開啟本群自動核准' in m['body'] and today in m['body'] and '衝刺週' in m['body'] for m in msgs)
    # 相同設定再送：不重複公告
    n = len(msgs)
    assert c.post(f'/api/rooms/{rid}/approval', headers=H(owner), json={'on': True, 'until': today}).json()['changed'] is False
    assert len(c.get(f'/api/rooms/{rid}/messages', headers=H(owner)).json()['messages']) == n
    # 房間列表帶標記
    row = next(r for r in c.get('/api/rooms', headers=H(owner)).json() if r['id'] == rid)
    assert row['auto_approve'] == {'on': True, 'until': today}
    # 過期：把 until 改成昨天（直接寫 DB 模擬時間經過）
    with contextlib.closing(zk_app.connect()) as db, db:
        db.execute("UPDATE room_approval SET until='2000-01-01' WHERE room=?", (rid,))
    st = c.get(f'/api/rooms/{rid}/approval', headers=lead).json()
    assert st['on'] is False and st['expired'] is True
    # 不設到期、再關閉
    assert c.post(f'/api/rooms/{rid}/approval', headers=H(owner), json={'on': True}).json()['until'] is None
    assert c.post(f'/api/rooms/{rid}/approval', headers=H(owner), json={'on': False}).json()['on'] is False
    msgs = c.get(f'/api/rooms/{rid}/messages', headers=H(owner)).json()['messages']
    assert any('不設到期' in m['body'] for m in msgs) and any('已關閉本群自動核准' in m['body'] for m in msgs)
    # 非成員查不到
    assert c.get(f'/api/rooms/{rid}/approval', headers=H(agent_tok(env, 'builder'))).status_code == 403


def test_skills_api_permissions_and_flow(env, owner, tmp_path, monkeypatch):
    """SPEC-1.1 §3：agent 可讀不可寫；使用者可新增／編輯／包／指派；使用中擋刪；頁面可開。"""
    import shutil as _sh
    roles = tmp_path / 'roles.json'
    _sh.copy(ROOT / 'tests' / 'fixtures' / 'roles.five.json', roles)
    home = tmp_path / 'home'
    (home / 'skills').mkdir(parents=True)
    monkeypatch.setenv('MBOX_ROLES', str(roles))
    monkeypatch.setenv('AAF_HOME', str(home))
    c = env['c']
    lead = H(agent_tok(env, 'lead'))
    body = '---\nname: s1\ndescription: 測試\n---\n內容一\n'
    assert c.post('/api/skills', headers=lead, json={'id': 's1', 'content': body}).status_code == 403
    assert c.post('/api/skills', headers=H(owner), json={'id': 's1', 'content': body}).status_code == 200
    assert c.get('/api/skills/s1', headers=lead).json()['content'] == body           # agent 可讀
    assert c.put('/api/skills/s1', headers=lead, json={'content': 'x'}).status_code == 403
    assert c.put('/api/skills/s1', headers=H(owner), json={'content': body + '二\n', 'note': 'n'}).json()['changed']
    assert len(c.get('/api/skills/s1/history', headers=H(owner)).json()['versions']) == 1
    assert c.put('/api/skill-packs/基本', headers=H(owner), json={'skills': ['s1']}).status_code == 200
    assert c.put('/api/skill-assign/role/builder', headers=H(owner), json={'packs': ['基本']}).status_code == 200
    r = c.delete('/api/skills/s1', headers=H(owner))
    assert r.status_code == 409 and '包 基本' in r.json()['detail']
    pv = c.get('/api/skill-preview?role=builder', headers=H(owner)).json()
    assert pv['skills'] == ['s1']
    assert json.loads(roles.read_text())['roles']['builder']['skill_packs'] == ['基本']
    assert c.put('/api/skill-assign/room/3', headers=lead, json={'skills': ['s1']}).status_code == 403
    assert c.get('/skills', follow_redirects=False).headers['location'] == '/app/skills'


def test_agent_sessions_api(env, owner):
    """SPEC-1.1 §2：控制台列出角色各群工作階段；只有使用者可看；可關閉群工作階段（不在跑時）。"""
    c, rt = env['c'], env['rt']
    d0 = rt.MBOX_HOME / 'roles' / 'lead'
    d2 = d0 / 'rooms' / '2'
    d2.mkdir(parents=True, exist_ok=True)
    (d2 / 'acp_session').write_text('S-room2')
    (d2 / 'acp_usage.json').write_text(json.dumps({'used': 3000, 'size': 10000}))
    (d2 / 'turns.log').write_text('x')
    assert c.get('/api/agents/lead/sessions', headers=H(agent_tok(env, 'lead'))).status_code == 403
    r = c.get('/api/agents/lead/sessions', headers=H(owner)).json()
    by = {s['room']: s for s in r['sessions']}
    assert None in by and 2 in by
    assert by[2]['session'] == 'S-room2' and by[2]['context_percent'] == 30 and by[2]['state'] == 'closed'
    assert by[None]['room_name'] == '（不屬於任何群）'
    assert c.get('/api/agents/ghost/sessions', headers=H(owner)).status_code == 404
    r = c.post('/api/agents/lead/sessions/2/close', headers=H(owner))
    assert r.status_code == 200, r.text
    assert not (d2 / 'acp_session').exists() and (d2 / 'acp_host.parked').exists()
    assert c.post('/api/agents/lead/sessions/9/close', headers=H(owner)).status_code == 404


def test_os_auth_mode(env, monkeypatch):
    """AAF_AUTH=os：用 OS 帳號登入；帳號必須是服務執行身分；setup 停用；密碼只經 stdin。"""
    app, c = env['app'], env['c']
    monkeypatch.setattr(app, 'AUTH_MODE', 'os')
    monkeypatch.setattr(app, 'os_user', lambda: 'svcuser')
    calls = []
    def fake_run(argv, **kw):
        calls.append((argv, kw.get('input')))
        ok = json.loads(kw['input'])['password'] == 'right-pw'
        return type('R', (), {'returncode': 0 if ok else 1})()
    monkeypatch.setattr(app.subprocess, 'run', fake_run)
    app.OS_AUTH_ATTEMPTS.clear()
    assert c.get('/api/setup-state').json() == {'has_accounts': True, 'auth': 'os'}
    assert c.post('/setup', json={'username': 'svcuser', 'password': 'x' * 10}).status_code == 403
    assert c.post('/login', json={'username': 'svcuser', 'password': 'wrong'}).status_code == 401
    r = c.post('/login', json={'username': 'svcuser', 'password': 'right-pw'})
    assert r.status_code == 200 and r.json()['token']
    n = len(calls)
    assert c.post('/login', json={'username': 'someone-else', 'password': 'right-pw'}).status_code == 401
    assert len(calls) == n                                         # 別的帳號不會送去驗證
    assert all('right-pw' not in ' '.join(a) for a, _ in calls)    # 密碼不在命令列
    app.OS_AUTH_ATTEMPTS[:] = [__import__('time').monotonic()] * 20
    assert c.post('/login', json={'username': 'svcuser', 'password': 'right-pw'}).status_code == 429
    app.OS_AUTH_ATTEMPTS.clear()


def test_web_app_serves_build_and_spa_fallback(env, tmp_path, monkeypatch):
    """新版前端 /app/：有建置就給靜態檔與單頁 fallback；不能跳出 dist；沒建置回 404。"""
    app, c = env['app'], env['c']
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<div id=root>INDEX</div>')
    (dist / 'assets' / 'a.js').write_text('JS')
    (tmp_path / 'secret.txt').write_text('SECRET')
    monkeypatch.setattr(app, 'WEB_DIST', dist)
    assert c.get('/app', follow_redirects=False).status_code == 307
    r = c.get('/app/'); assert r.status_code == 200 and 'INDEX' in r.text
    r = c.get('/app/assets/a.js'); assert r.text == 'JS' and 'immutable' in r.headers['cache-control']
    r = c.get('/app/rooms/3'); assert r.status_code == 200 and 'INDEX' in r.text
    assert c.get('/app/assets/missing.js').status_code == 404
    assert c.get('/app/nope.png').status_code == 404
    for p in ('/app/../secret.txt', '/app/%2e%2e/secret.txt', '/app/..%2Fsecret.txt', '/app/assets/..%2F..%2Fsecret.txt'):
        assert 'SECRET' not in c.get(p).text
    monkeypatch.setattr(app, 'WEB_DIST', tmp_path / 'none')
    r = c.get('/app/'); assert r.status_code == 404 and 'npm run build' in r.text


def test_room_engine_override_per_room(env, owner, monkeypatch):
    """SPEC-1.1 §6：同一角色在兩個群各自用不同 agent／模型，互不影響；只限擁有者；不支援每群 session 的角色 409。"""
    import drivers
    from drivers import acp_catalog
    c, rt, app = env['c'], env['rt'], env['app']
    roles = dict(rt.load_roles())
    roles['lead'] = dict(roles['lead'], driver='acp', acp_agent='codex', model='m-default')
    roles['builder'] = dict(roles['builder'], driver='command', command='true')
    monkeypatch.setattr(rt, 'load_roles', lambda: roles)
    monkeypatch.setattr(acp_catalog, 'all_agents', lambda: {'codex': {}, 'pi': {}})
    monkeypatch.setattr(acp_catalog, 'resolve_cmd', lambda a: ['fake-' + a])
    checked = []
    monkeypatch.setattr(acp_catalog, 'check_model', lambda a, m: checked.append((a, m)) or ({'ok': m != 'broken'}))
    mk = lambda n: c.post('/api/rooms', headers=H(owner), json={'name': n, 'members': ['lead', 'builder']}).json()['id']
    r1, r2 = mk('engine-a'), mk('engine-b')
    url = lambda r, role='lead': f'/api/rooms/{r}/agents/{role}/engine'

    assert c.post(url(r1), headers=H(agent_tok(env, 'lead')), json={'agent': 'pi'}).status_code == 403
    assert c.post(url(r1), headers=H(owner), json={'agent': 'ghost'}).status_code == 400
    bad = c.post(url(r1), headers=H(owner), json={'agent': 'pi', 'model': 'broken'})
    assert bad.status_code == 400 and '未切換' in bad.json()['detail']
    assert not (rt.MBOX_HOME / 'roles/lead/rooms' / str(r1) / 'override.json').exists()
    assert c.post(url(r1, 'builder'), headers=H(owner), json={'model': 'x'}).status_code == 409

    r = c.post(url(r1), headers=H(owner), json={'agent': 'pi', 'model': 'm-pi'})
    assert r.status_code == 200, r.text
    assert r.json()['overridden'] and r.json()['acp_agent'] == 'pi' and ('pi', 'm-pi') in checked
    assert c.post(url(r2), headers=H(owner), json={'model': 'm-two'}).json()['acp_agent'] == 'codex'

    eng = {r: {e['role']: e for e in c.get(f'/api/rooms/{r}/engines', headers=H(owner)).json()} for r in (r1, r2)}
    assert (eng[r1]['lead']['acp_agent'], eng[r1]['lead']['model']) == ('pi', 'm-pi')
    assert (eng[r2]['lead']['acp_agent'], eng[r2]['lead']['model']) == ('codex', 'm-two')
    assert eng[r1]['builder']['per_room'] is False

    # 實際叫醒用的設定：各群各自、預設 session 不受影響
    a1 = drivers.make('lead', roles['lead'], rt.MBOX_HOME, lane=r1)
    a2 = drivers.make('lead', roles['lead'], rt.MBOX_HOME, lane=r2)
    a0 = drivers.make('lead', roles['lead'], rt.MBOX_HOME)
    assert (a1.cfg['acp_agent'], a1.cfg['model']) == ('pi', 'm-pi')
    assert (a2.cfg['acp_agent'], a2.cfg['model']) == ('codex', 'm-two')
    assert (a0.cfg['acp_agent'], a0.cfg['model']) == ('codex', 'm-default')
    assert a1.state_dir != a2.state_dir

    # 恢復預設
    r = c.post(url(r1), headers=H(owner), json={})
    assert r.json()['state'] == 'cleared' and not r.json()['overridden']
    a1 = drivers.make('lead', roles['lead'], rt.MBOX_HOME, lane=r1)
    assert (a1.cfg['acp_agent'], a1.cfg['model']) == ('codex', 'm-default')
