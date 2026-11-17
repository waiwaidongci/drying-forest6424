"""设备日志采样验收脚本（无需外部数据库/Redis）。

运行：
    cd backend
    DATABASE_URL=sqlite:////tmp/device-verify.db SEED_DEMO=false ARTIFACT_DIR=/tmp/device-artifacts \
      python -m test-artifacts... （见 TESTING.md，脚本通过 python 直接执行）

覆盖：
1. 时间加权按试件工序窗计算，窗前前值沿用，结束后读数不补窗内缺口。
2. 窗前无值、断采超限 → 隔离。
3. 报齐线未覆盖工序结束 → 待报齐，禁止封窗；报齐线不可回退。
4. 同刻异值 → 阻止封窗；裁定后才可封窗。
5. 封窗后更早迟到值 → 保留争议，已封样本不变。
6. 幂等键重试不重复落样本；写入中断可恢复。
7. 研究只冻结完整批次；手录来源仍可研究。
8. 乱序补报重算受影响窗口。
"""
import os, sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
DB = os.environ.get('VERIFY_DB', '/tmp/device-verify.db')
os.environ['DATABASE_URL'] = 'sqlite:///' + DB
os.environ['SEED_DEMO'] = 'false'
os.environ['ARTIFACT_DIR'] = '/tmp/device-verify-artifacts'
if os.path.exists(DB): os.remove(DB)

from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from types import SimpleNamespace
import src.main as main_module
from src.config.database import Session
from src.models.user import User
from src.models.run import Run
from src.models.device import MaterializationBatch, GeneratedSample
from src.utils.security import hash_password
from src.services import runs as run_service

class StubQueue:
    def rpush(self, *a, **k): pass
    def ping(self): return True
run_service.queue = StubQueue()

failures = []
def check(name, condition, detail=''):
    print(('PASS ' if condition else 'FAIL ') + name + ('' if condition else ' :: ' + str(detail)))
    if not condition: failures.append(name)

with TestClient(main_module.app) as client:
    with Session() as db:
        db.add(User(email='analyst@verify', password_hash=hash_password('pw'), role='analyst'))
        db.add(User(email='viewer@verify', password_hash=hash_password('pw'), role='viewer'))
        db.commit()
    token = client.post('/auth/login', json={'email': 'analyst@verify', 'password': 'pw'}).json()['token']
    viewer = client.post('/auth/login', json={'email': 'viewer@verify', 'password': 'pw'}).json()['token']
    H = {'Authorization': 'Bearer ' + token}
    VH = {'Authorization': 'Bearer ' + viewer}
    source = client.post('/sources', json={'name': '验收来源', 'filename': 'verify.csv', 'description': 'v'}, headers=H).json()
    sid = source['id']
    device = client.post('/devices', json={'source_id': sid, 'key': 'DEV-V', 'name': '验收箱', 'gap_limit_seconds': 300}, headers=H).json()
    did = device['id']

    windows = [
        {'reference': 'V-W1', 'started_at': '2026-11-01T09:00:00+08:00', 'ended_at': '2026-11-01T09:10:00+08:00',
         'moisture': 35, 'thickness': 4, 'airflow': 3, 'minutes': 80},
        {'reference': 'V-W2', 'started_at': '2026-11-02T09:00:00+08:00', 'ended_at': '2026-11-02T09:10:00+08:00',
         'moisture': 31, 'thickness': 4.5, 'airflow': 2.5, 'minutes': 90},
        {'reference': 'V-W3', 'started_at': '2026-11-02T11:00:00+08:00', 'ended_at': '2026-11-02T11:10:00+08:00',
         'moisture': 36, 'thickness': 3.5, 'airflow': 3.2, 'minutes': 70},
    ]
    check('登记工序窗', client.post(f'/devices/{did}/windows', json={'windows': windows}, headers=H).status_code == 200)

    # 乱序接收：先到 09:05、09:00、08:50，再加窗后读数
    readings = [
        {'observed_at': '2026-11-01T09:05:00+08:00', 'value': 80},
        {'observed_at': '2026-11-01T09:00:00+08:00', 'value': 70},
        {'observed_at': '2026-11-01T08:50:00+08:00', 'value': 60},
        {'observed_at': '2026-11-01T09:20:00+08:00', 'value': 999},   # 工序结束后，不得补窗内缺口
        {'observed_at': '2026-11-02T09:00:00+08:00', 'value': 50},
        {'observed_at': '2026-11-02T09:05:00+08:00', 'value': 52},
        {'observed_at': '2026-11-02T11:00:00+08:00', 'value': 65},
        {'observed_at': '2026-11-02T11:06:00+08:00', 'value': 67},
    ]
    r = client.post(f'/devices/{did}/readings', json={'readings': readings}, headers=H)
    check('乱序接收不报错', r.status_code == 200, r.text)
    r = client.post(f'/devices/{did}/seal', json={'idempotency_key': 'before-watermark'}, headers=H)
    check('未报齐禁止封窗', r.status_code == 409, r.text)
    client.post(f'/devices/{did}/watermark', json={'reported_through': '2026-11-02T12:30:00+08:00'}, headers=H)

    state = client.get(f'/devices/{did}', headers=H).json()
    by_ref = {w['reference']: w for w in state['windows']}
    check('W1 时间加权 = 75（60 4分钟 + 70 1分钟 + 80 5分钟）', abs(by_ref['V-W1']['weighted_temperature'] - 75.0) < 1e-9, by_ref['V-W1']['weighted_temperature'])
    check('W1 窗内点数只含窗内读数', by_ref['V-W1']['basis']['point_count_in_window'] == 1, by_ref['V-W1']['basis']['point_count_in_window'])
    check('W1 结束后读数不补缺口', by_ref['V-W1']['basis']['readings'][-1]['value'] == 80.0 and all(x['value'] != 999 for x in by_ref['V-W1']['basis']['segments']))
    check('W2 前值沿用', abs(by_ref['V-W2']['weighted_temperature'] - 51.0) < 1e-9, by_ref['V-W2']['weighted_temperature'])
    check('W3 断采超限隔离', by_ref['V-W3']['state'] == 'quarantined' and by_ref['V-W3']['reason'] == 'gap_exceeded', by_ref['V-W3'])
    check('报齐线不可回退', client.post(f'/devices/{did}/watermark', json={'reported_through': '2026-11-01T00:00:00Z'}, headers=H).status_code == 409)
    check('访客不能封窗', client.post(f'/devices/{did}/seal', json={'idempotency_key': 'x'}, headers=VH).status_code == 403)

    first = client.post(f'/devices/{did}/seal', json={'idempotency_key': 'batch-1'}, headers=H).json()
    check('封窗整批生效', first['status'] == 'sealed' and len(first['rows']) == 3, first.get('status'))
    check('封窗统计', first['summary']['effective'] == 2 and first['summary']['quarantined'] == 1, first['summary'])
    again = client.post(f'/devices/{did}/seal', json={'idempotency_key': 'batch-1'}, headers=H).json()
    check('幂等键重试不重复', again['id'] == first['id'] and len(again['rows']) == 3)

    r = client.post(f'/devices/{did}/readings', json={'readings': [{'observed_at': '2026-11-01T08:00:00+08:00', 'value': 55}]}, headers=H).json()
    check('封窗后更早迟到值保留争议', len(r.get('disputes',[])) == 1, r)
    check('迟到值不改已封统计', client.get(f'/batches/{first["id"]}', headers=H).json()['summary']['quarantined'] == 1)
    state = client.get(f'/devices/{did}', headers=H).json()
    sealed_w1 = next(w for w in state['windows'] if w['reference'] == 'V-W1')['sealed_sample']
    check('已封样本不被迟到值改写', abs(sealed_w1['weighted_temperature'] - 75.0) < 1e-9, sealed_w1)

    r = client.post(f'/devices/{did}/readings', json={'readings': [{'observed_at': '2026-11-01T09:05:00+08:00', 'value': 85}]}, headers=H).json()
    check('同刻异值记录冲突', r['conflicts'] == ['2026-11-01T01:05:00Z'], r)
    r = client.post(f'/devices/{did}/seal', json={'idempotency_key': 'batch-2'}, headers=H)
    check('同刻异值阻止封窗', r.status_code == 409, r.text)
    state = client.get(f'/devices/{did}', headers=H).json()
    conflict = next(c for c in state['conflicts'] if c['status'] == 'open')
    check('候选值完整', sorted(conflict['candidate_values']) == [80.0, 85.0], conflict['candidate_values'])
    client.post(f'/devices/{did}/conflicts/{conflict["id"]}/resolve', json={'value': 85}, headers=H)
    second = client.post(f'/devices/{did}/seal', json={'idempotency_key': 'batch-2'}, headers=H).json()
    new_w1 = next(x for x in second['rows'] if x['reference'] == 'V-W1')
    check('裁定后新批次采用裁定值', abs(new_w1['weighted_temperature'] - 77.5) < 1e-9, new_w1['weighted_temperature'])

    # 崩溃恢复：批次已标记写入但样本行缺失
    with Session() as db:
        batch = db.get(MaterializationBatch, second['id'])
        ids = [x.window_id for x in db.scalars(select(GeneratedSample).where(GeneratedSample.batch_id == batch.id))]
        db.execute(delete(GeneratedSample).where(GeneratedSample.batch_id == batch.id))
        batch.status = 'writing'; batch.summary = {'window_count': len(ids), 'window_ids': ids, 'phase': 'writing'}
        db.commit()
    detail = client.get(f'/batches/{second["id"]}', headers=H).json()
    check('不完整批次标记不完整', detail['complete'] is False and detail['rows'] == [])
    r = client.post('/runs', json={'source_id': sid, 'name': '不完整批次研究', 'parameters': {'trees': 20, 'depth': 3}, 'batch_id': second['id']}, headers=H)
    check('研究拒绝不完整批次', r.status_code == 409, r.text)
    resumed = client.post(f'/batches/{second["id"]}/resume', headers=H).json()
    check('恢复写入并封窗', resumed['status'] == 'sealed' and len(resumed['rows']) == 3, resumed.get('status'))
    resumed2 = client.post(f'/batches/{second["id"]}/resume', headers=H).json()
    check('重复恢复不重复落样本', len(resumed2['rows']) == 3)

    r = client.post('/runs', json={'source_id': sid, 'name': '批次研究', 'parameters': {'trees': 20, 'depth': 3}, 'batch_id': second['id']}, headers=H)
    check('完整批次可研究', r.status_code == 200, r.text)
    run = r.json()
    check('研究只冻结生效样本', len(run['snapshot']['records']) == 2 and len(run['snapshot']['quarantined']) == 1, (len(run['snapshot']['records']), len(run['snapshot']['quarantined'])))
    check('研究记录冻结时间加权温度', all(x['values']['temperature'] > 0 for x in run['snapshot']['records']))

    from src.worker import execute
    import src.worker as worker_module
    worker_module.queue = StubQueue()
    execute(run['id'])
    done = client.get(f'/runs/{run["id"]}', headers=H).json()
    check('样本不足时任务明确失败', done['state'] == 'failed' and '至少40条' in done['error'], (done['state'], done['error']))

    client.post(f'/sources/{sid}/records', json={'records': [
        {'reference': f'MANUAL-{i}', 'values': {'moisture': 30 + i, 'thickness': 4, 'temperature': 70, 'airflow': 3, 'minutes': 80 + i}}
        for i in range(3)
    ]}, headers=H)
    manual = client.post('/runs', json={'source_id': sid, 'name': '手录研究', 'parameters': {'trees': 20, 'depth': 3}}, headers=H)
    check('手录来源仍可研究', manual.status_code == 200 and len(manual.json()['snapshot']['records']) == 3, manual.text)

    r = client.post(f'/devices/{did}/windows', json={'windows': [
        {'reference': 'V-W4', 'started_at': '2026-11-03T09:00:00+08:00', 'ended_at': '2026-11-03T09:20:00+08:00',
         'moisture': 30, 'thickness': 4, 'airflow': 3, 'minutes': 80}]}, headers=H)
    state = client.get(f'/devices/{did}', headers=H).json()
    pending = next(w for w in state['windows'] if w['reference'] == 'V-W4')
    check('新工序窗待报齐', pending['state'] == 'pending' and pending['reason'] == 'not_reported_through', pending['state'])
    r = client.post(f'/devices/{did}/seal', json={'idempotency_key': 'batch-3'}, headers=H)
    check('待报齐阻止新批次封窗', r.status_code == 409, r.text)

    events = client.get('/audit', headers=H).json() if client.get('/audit', headers=H).status_code == 200 else []
    actions = {e['action'] for e in events}
    check('审计包含设备事件', {'device.created', 'readings.received', 'batch.sealed'} <= actions, sorted(actions)[:10])

print('\nRESULT:', 'ALL PASS' if not failures else f'{len(failures)} FAILURES -> {failures}')
sys.exit(0 if not failures else 1)
