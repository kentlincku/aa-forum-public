"""#40-4：blocked 任務可由 assignee（或發起人）重新 claim；其他角色與 done／cancelled 不行。"""
import pytest

from mbox.core import MboxError, Store

Lead = {'id': 'lead', 'rank': 'lead'}
Builder = {'id': 'builder', 'rank': 'worker'}
Debugger = {'id': 'debugger', 'rank': 'worker'}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 't.db')
    for r, rank in (('lead', 'lead'), ('builder', 'worker'), ('debugger', 'worker')):
        s.add_agent(r, rank=rank)
    yield s
    s.db.close()


def blocked_task(store):
    tid = store.post_task(Lead, 'x', assignee='builder')['task_id']
    store.claim_task(Builder, tid)
    store.update_task(Builder, tid, 'blocked', 'need input')
    return tid


def state(store, tid):
    return store.db.execute('SELECT state, assignee FROM tasks WHERE id=?', (tid,)).fetchone()


def test_assignee_reclaims_blocked(store):
    tid = blocked_task(store)
    r = store.claim_task(Builder, tid)
    assert r['state'] == 'claimed' and tuple(state(store, tid)) == ('claimed', 'builder')
    store.update_task(Builder, tid, 'done', 'ok')  # 解除後可正常回報
    assert state(store, tid)['state'] == 'done'


def test_owner_reclaims_blocked(store):
    """reviewer #42 SHOULD 2：發起人接手時通知原 assignee；blocked 原因訊息保留。"""
    tid = blocked_task(store)
    reason_id = store.db.execute('SELECT result_msg_id FROM tasks WHERE id=?', (tid,)).fetchone()[0]
    r = store.claim_task(Lead, tid)
    assert tuple(state(store, tid)) == ('claimed', 'lead') and r['taken_over_from'] == 'builder'
    inbox = store.inbox(Builder, mark=False)
    assert any(f'[任務 #{tid}] 已由發起人 lead 接手' in m['body'] for m in inbox)
    assert store.db.execute('SELECT result_msg_id FROM tasks WHERE id=?', (tid,)).fetchone()[0] == reason_id


def test_assignee_reclaim_does_not_notify(store):
    tid = blocked_task(store)
    before = len(store.inbox(Builder, mark=False))
    assert store.claim_task(Builder, tid)['taken_over_from'] is None
    assert len(store.inbox(Builder, mark=False)) == before


def test_other_role_rejected(store):
    tid = blocked_task(store)
    with pytest.raises(MboxError) as e:
        store.claim_task(Debugger, tid)
    assert e.value.code == 403 and state(store, tid)['state'] == 'blocked'


@pytest.mark.parametrize('final', ['done', 'cancelled'])
def test_done_and_cancelled_not_reclaimable(store, final):
    tid = store.post_task(Lead, 'x', assignee='builder')['task_id']
    store.claim_task(Builder, tid)
    if final == 'done':
        store.update_task(Builder, tid, 'done', 'ok')
    else:
        store.update_task(Lead, tid, 'cancelled')
    for who in (Builder, Lead):
        with pytest.raises(MboxError) as e:
            store.claim_task(who, tid)
        assert e.value.code == 409
