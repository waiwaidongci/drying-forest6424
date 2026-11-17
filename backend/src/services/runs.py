from sqlalchemy import select
from fastapi import HTTPException
from redis import Redis
from ..config.settings import REDIS_URL
from ..models.run import Run
from ..models.device import MaterializationBatch
from ..utils.serialization import row,now
from .sources import source_or_404,list_records
from .domain_validation import validate_parameters
from .device_sampling import sealed_research_records
from .audit import log
queue=Redis.from_url(REDIS_URL)
def get_run(db,id):
    r=db.get(Run,id)
    if not r:raise HTTPException(404,'未找到分析任务')
    return r
def _snapshot_for_batch(db,source,batch_id):
    batch=db.get(MaterializationBatch,batch_id)
    if not batch:raise HTTPException(404,'未找到设备样本批次')
    if batch.source_id!=source.id:raise HTTPException(422,'样本批次不属于所选来源')
    effective,quarantined,_=sealed_research_records(db,batch)
    if not effective:raise HTTPException(422,'该批次没有生效样本，无法执行研究')
    return {'source':row(source),'records':effective,'batch':{'id':batch.id,'fingerprint':batch.fingerprint,
            'summary':dict(batch.summary or {}),'sealed_at':batch.sealed_at,'formula':(batch.summary or {}).get('formula')},
            'quarantined':quarantined}
def create_run(db,user,data):
    s=source_or_404(db,data.source_id)
    if getattr(data,'batch_id',None):
        snapshot=_snapshot_for_batch(db,s,data.batch_id)
        count=len(snapshot['records'])
    else:
        records=list_records(db,s.id)
        if len(records)<3:raise HTTPException(422,'至少录入3条样本后才能执行')
        snapshot={'source':row(s),'records':records};count=len(records)
    try: params=validate_parameters(data.parameters)
    except ValueError as e:raise HTTPException(422,str(e))
    r=Run(source_id=s.id,name=data.name,parameters=params,snapshot=snapshot,created_at=now(),state='queued')
    db.add(r);db.flush();log(db,user,'run.queued',r.id,data.name+(' · 批次#'+str(data.batch_id) if getattr(data,'batch_id',None) else ''));db.commit()
    try:queue.rpush('analysis:queue',r.id)
    except Exception:
        r.state='failed';r.error='队列暂不可用，请重新创建任务';db.commit();raise HTTPException(503,r.error)
    return row(r)
def list_runs(db):
    out=[]
    for r in db.scalars(select(Run).order_by(Run.id.desc())):
        item={k:v for k,v in row(r).items() if k not in ('snapshot','result')}
        batch=(r.snapshot or {}).get('batch') or {}
        item['batch_id']=batch.get('id');item['batch_fingerprint']=batch.get('fingerprint')
        item['quarantined_count']=len((r.snapshot or {}).get('quarantined') or [])
        out.append(item)
    return out
