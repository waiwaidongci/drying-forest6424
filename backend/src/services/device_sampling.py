import hashlib,json,math
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select,func,delete
from sqlalchemy.exc import IntegrityError
from ..models.device import Device,SpecimenWindow,TemperatureReading,ReadingConflict,MaterializationBatch,GeneratedSample,SampleDispute
from ..models.source import Source
from ..utils.serialization import row,now
from .domain_validation import validate_record
from .sources import source_or_404
from .audit import log

FORMULA='LOCF_TIME_WEIGHTED_V1'
DEFAULT_GAP=300

def parse_ts(value,label='时刻'):
    if not isinstance(value,str) or not value.strip():raise HTTPException(422,label+'不能为空')
    try:dt=datetime.fromisoformat(value.strip().replace('Z','+00:00'))
    except ValueError:raise HTTPException(422,label+'需为ISO 8601时间：'+value)
    if dt.tzinfo is None:raise HTTPException(422,label+'需包含时区偏移，例如 +08:00 或 Z')
    return dt.astimezone(timezone.utc)

def canonical(value,label='时刻'):
    return parse_ts(value,label).isoformat(timespec='seconds').replace('+00:00','Z')

def reading_value(value):
    if isinstance(value,bool):raise HTTPException(422,'温度读数必须为数值')
    try:v=float(value)
    except (TypeError,ValueError):raise HTTPException(422,'温度读数必须为数值')
    if not math.isfinite(v):raise HTTPException(422,'温度读数必须是有限数值')
    return v

def device_or_404(db,id):
    d=db.get(Device,id)
    if not d:raise HTTPException(404,'未找到设备日志流')
    return d

def _lock_device(db,id):
    d=db.scalar(select(Device).where(Device.id==id).with_for_update())
    if not d:raise HTTPException(404,'未找到设备日志流')
    return d

def _device_dict(d):return row(d)

def list_devices(db):
    out=[]
    for d in db.scalars(select(Device).order_by(Device.id)):
        src=db.get(Source,d.source_id)
        out.append({**_device_dict(d),'source_name':src.name if src else '','source_key':src.key if src else '',
                    'window_count':db.scalar(select(func.count()).select_from(SpecimenWindow).where(SpecimenWindow.device_id==d.id)),
                    'reading_count':db.scalar(select(func.count()).select_from(TemperatureReading).where(TemperatureReading.device_id==d.id)),
                    'batch_count':db.scalar(select(func.count()).select_from(MaterializationBatch).where(MaterializationBatch.device_id==d.id,MaterializationBatch.status=='sealed'))})
    return out

def create_device(db,user,data):
    source_or_404(db,data.source_id)
    key=data.key.strip()
    if not key:raise HTTPException(422,'设备标识不能为空')
    if db.scalar(select(Device.id).where(Device.key==key)):raise HTTPException(409,'设备标识已存在：'+key)
    d=Device(source_id=data.source_id,key=key,name=data.name.strip(),gap_limit_seconds=int(data.gap_limit_seconds),created_at=now())
    db.add(d);db.flush();log(db,user,'device.created',d.id,key);db.commit();return _device_dict(d)

def advance_watermark(db,user,id,through):
    d=_lock_device(db,id)
    t=canonical(through,'报齐时刻')
    if d.reported_through and parse_ts(t)<parse_ts(d.reported_through):
        raise HTTPException(409,'报齐线只能前进；更早的迟到读数保留为争议，不改已封样本')
    d.reported_through=t
    log(db,user,'device.reported_through',d.id,t);db.commit()
    return _device_dict(d)

def validate_manual(values):
    payload={'moisture':values.get('moisture'),'thickness':values.get('thickness'),
             'temperature':1.0,'airflow':values.get('airflow'),'minutes':values.get('minutes')}
    try:clean=validate_record(payload)
    except ValueError as e:raise HTTPException(422,str(e))
    return {k:clean[k] for k in ('moisture','thickness','airflow','minutes')}

def add_windows(db,user,device_id,data):
    d=_lock_device(db,device_id);source_or_404(db,d.source_id)
    created=[];seen=set()
    for item in data.windows:
        ref=item.reference.strip()
        if not ref:raise HTTPException(422,'试件编号不能为空')
        if ref in seen:raise HTTPException(422,'批内试件编号重复：'+ref)
        seen.add(ref)
        if db.scalar(select(SpecimenWindow.id).where(SpecimenWindow.source_id==d.source_id,SpecimenWindow.reference==ref)):
            raise HTTPException(409,'试件编号已存在：'+ref)
        start=canonical(item.started_at,'工序开始时刻');end=canonical(item.ended_at,'工序结束时刻')
        if parse_ts(end)<=parse_ts(start):raise HTTPException(422,'工序结束必须晚于开始：'+ref)
        manual=validate_manual({'moisture':item.moisture,'thickness':item.thickness,'airflow':item.airflow,'minutes':item.minutes})
        w=SpecimenWindow(source_id=d.source_id,device_id=d.id,reference=ref,started_at=start,ended_at=end,
                         manual_values=manual,dirty=True,created_at=now())
        db.add(w);db.flush();created.append(w)
        log(db,user,'window.created',w.id,ref+' '+start+' → '+end)
    db.commit()
    return [row(w) for w in created]

def _open_conflicts(db,device_id):
    return list(db.scalars(select(ReadingConflict).where(ReadingConflict.device_id==device_id,ReadingConflict.status=='open').order_by(ReadingConflict.observed_at)))

def _candidates(values):return sorted({round(float(v),6) for v in values})

def _latest_carry(readings,start):
    carry=None
    for r in readings:
        if parse_ts(r.observed_at)<=start and (carry is None or parse_ts(r.observed_at)>parse_ts(carry.observed_at)):carry=r
    return carry

def _affects_window(window,t,readings):
    moment=parse_ts(t);start=parse_ts(window.started_at);end=parse_ts(window.ended_at)
    if moment>end:return False
    if moment>=start:return True
    carry=_latest_carry(readings,start)
    return carry is not None and carry.observed_at==t

def _late_relevance(window,t,basis):
    """封窗后到达的读数是否需要留争议。只有落在已封窗内、或晚于已封窗前值
    （会改变前值沿用）才影响该窗；早于已封前值的读数记在设备级争议里，不逐窗扩散。"""
    moment=parse_ts(t);start=parse_ts(window.started_at);end=parse_ts(window.ended_at)
    if moment>end:return False,''
    if moment>=start:return True,'落在已封工序窗内，保留争议；已封样本不变'
    carry=(basis or {}).get('carry_in') or {}
    carry_at=carry.get('observed_at')
    if carry_at and moment>parse_ts(carry_at):
        return True,'晚于已封窗前值，保留争议；已封样本不变'
    return False,''

def _record_dispute(db,window,batch_id,t,value,note,source_id=None):
    query=select(SampleDispute.id).where(SampleDispute.window_id==(window.id if window else None),
                                         SampleDispute.observed_at==t,SampleDispute.value==value)
    if not window:query=query.where(SampleDispute.batch_id==batch_id)
    if db.scalar(query):return None
    d=SampleDispute(source_id=window.source_id if window else source_id,window_id=window.id if window else None,
                    batch_id=batch_id,observed_at=t,value=value,late=True,status='open',note=note,created_at=now())
    db.add(d);return d

def ingest_readings(db,user,device_id,data):
    # 设备行锁把同一设备的补报串行化；并发撞到同刻唯一键时整批回滚，重试按幂等处理。
    try:
        return _ingest_readings(db,user,device_id,data)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409,'并发补报撞上同刻读数，本批未落库；请重试，重复值会按幂等处理')

def _ingest_readings(db,user,device_id,data):
    d=_lock_device(db,device_id)
    windows=list(db.scalars(select(SpecimenWindow).where(SpecimenWindow.device_id==d.id)))
    sealed={}
    if windows:
        for s in db.scalars(select(GeneratedSample).join(MaterializationBatch,MaterializationBatch.id==GeneratedSample.batch_id)
                            .where(GeneratedSample.device_id==d.id,MaterializationBatch.status=='sealed')
                            .order_by(GeneratedSample.batch_id.desc())):
            sealed.setdefault(s.window_id,s)
    existing={r.observed_at:r for r in db.scalars(select(TemperatureReading).where(TemperatureReading.device_id==d.id))}
    conflicts={c.observed_at:c for c in db.scalars(select(ReadingConflict).where(ReadingConflict.device_id==d.id))}
    added=[];duplicates=0;conflict_times=set();affected=set();disputes=[];pending={}
    def flag_conflict(t,v,candidate):
        conflict=conflicts.get(t)
        if conflict:
            conflict.values=_candidates(list(conflict.values)+[v]);conflict.status='open'
            conflict.chosen_value=None;conflict.resolved_at=None;conflict.resolved_by=None
        else:
            conflict=ReadingConflict(device_id=d.id,observed_at=t,values=_candidates([candidate,v]),status='open',created_at=now())
            db.add(conflict);conflicts[t]=conflict
        conflict_times.add(t)
        log(db,user,'reading.conflict',d.id,t+json.dumps(conflict.values,ensure_ascii=False))
    for item in data.readings:
        t=canonical(item.observed_at,'读数时刻');v=reading_value(item.value)
        current=existing.get(t)
        if current is not None:
            if abs(current.value-v)<1e-9:duplicates+=1;continue
            flag_conflict(t,v,current.value);continue
        # 同一请求内的同刻异值同样按冲突处理，绝不静默覆盖
        if t in pending:
            if abs(pending[t]-v)<1e-9:duplicates+=1;continue
            flag_conflict(t,v,pending[t]);continue
        late=bool(d.reported_through and parse_ts(t)<parse_ts(d.reported_through))
        reading=TemperatureReading(device_id=d.id,observed_at=t,value=v,received_at=now(),late=late)
        db.add(reading);existing[t]=reading;pending[t]=v;added.append(t)
    readings=sorted(existing.values(),key=lambda r:parse_ts(r.observed_at))
    changed=sorted(set(added)|conflict_times,key=parse_ts)
    for w in windows:
        if any(_affects_window(w,t,readings) for t in changed):w.dirty=True;affected.add(w.reference)
    for t in changed:
        touched=False
        for w in windows:
            frozen=sealed.get(w.id)
            if not frozen:continue
            relevant,note=_late_relevance(w,t,frozen.basis)
            if not relevant:continue
            touched=True
            if t in conflict_times:
                values=list(conflicts[t].values if t in conflicts else [])
            else:
                reading=existing.get(t);values=[reading.value] if reading else []
            for v in values:
                dispute=_record_dispute(db,w,frozen.batch_id,t,float(v),note)
                if dispute:
                    db.flush();disputes.append(row(dispute))
        if not touched:
            # 落在已封批次时间跨度内、但不改变任何窗口数值的迟到值，设备级留一条争议。
            moment=parse_ts(t)
            covered=[w for w in windows if sealed.get(w.id) and moment<=parse_ts(w.ended_at)]
            if covered and t not in conflict_times:
                reading=existing.get(t)
                if reading:
                    first=min(covered,key=lambda w:parse_ts(w.started_at))
                    dispute=_record_dispute(db,None,sealed[first.id].batch_id,t,float(reading.value),
                                            '落在已封批次时间跨度内但不改变任何窗口数值；已留痕，已封样本不变',source_id=d.source_id)
                    if dispute:
                        db.flush();disputes.append(row(dispute))
    log(db,user,'readings.received',d.id,json.dumps({'added':len(added),'duplicates':duplicates,'conflicts':len(conflict_times),'disputes':len(disputes)},ensure_ascii=False))
    db.commit()
    return {'added':len(added),'duplicates':duplicates,'conflicts':sorted(conflict_times),'disputes':disputes,
            'affected_windows':sorted(affected),'reported_through':d.reported_through}

def resolve_conflict(db,user,id,value):
    c=db.get(ReadingConflict,id)
    if not c:raise HTTPException(404,'未找到同刻异值冲突')
    _lock_device(db,c.device_id)
    db.refresh(c)
    if c.status!='open':raise HTTPException(409,'该冲突已裁定')
    v=reading_value(value)
    if not any(abs(float(x)-v)<1e-9 for x in c.values):raise HTTPException(422,'裁定值必须来自冲突候选值')
    reading=db.scalar(select(TemperatureReading).where(TemperatureReading.device_id==c.device_id,TemperatureReading.observed_at==c.observed_at))
    if reading:reading.value=v
    else:db.add(TemperatureReading(device_id=c.device_id,observed_at=c.observed_at,value=v,received_at=now(),late=True))
    db.flush()
    c.status='resolved';c.chosen_value=v;c.resolved_at=now();c.resolved_by=getattr(user,'email',str(user))
    readings=sorted(db.scalars(select(TemperatureReading).where(TemperatureReading.device_id==c.device_id)),key=lambda r:parse_ts(r.observed_at))
    windows=list(db.scalars(select(SpecimenWindow).where(SpecimenWindow.device_id==c.device_id)))
    sealed={}
    for s in db.scalars(select(GeneratedSample).join(MaterializationBatch,MaterializationBatch.id==GeneratedSample.batch_id)
                        .where(GeneratedSample.device_id==c.device_id,MaterializationBatch.status=='sealed')):
        sealed.setdefault(s.window_id,s)
    for w in windows:
        if not _affects_window(w,c.observed_at,readings):continue
        w.dirty=True
        frozen=sealed.get(w.id)
        if frozen:_record_dispute(db,w,frozen.batch_id,c.observed_at,v,'冲突裁定发生在封窗后；保留争议，不改已封样本')
    log(db,user,'reading.conflict_resolved',c.id,c.observed_at+' → '+str(v));db.commit()
    return row(c)

def resolve_dispute(db,user,id,note=''):
    d=db.get(SampleDispute,id)
    if not d:raise HTTPException(404,'未找到迟到争议')
    if d.status!='open':raise HTTPException(409,'该争议已处理')
    d.status='resolved';d.note=(note or '已复核，保留原封样本')[:2000];d.resolved_at=now();d.resolved_by=getattr(user,'email',str(user))
    log(db,user,'dispute.resolved',d.id,d.observed_at);db.commit();return row(d)

def _quarantine(reason,text,basis):
    return {'state':'quarantined','reason':reason,'reason_text':text,'weighted_temperature':None,
            'coverage_seconds':None,'basis':basis}

def compute_window(window,readings,gap_limit,watermark,device_key=''):
    start=parse_ts(window.started_at);end=parse_ts(window.ended_at)
    ordered=sorted(readings,key=lambda r:parse_ts(r.observed_at))
    before_end=[r for r in ordered if parse_ts(r.observed_at)<=end]
    base={'formula':FORMULA,'device_key':device_key,'reference':window.reference,
          'started_at':window.started_at,'ended_at':window.ended_at,
          'gap_limit_seconds':gap_limit,'reported_through':watermark,'carry_in':None,
          'readings':[],'segments':[],'max_gap_seconds':None,
          'coverage_seconds':(end-start).total_seconds(),'window_seconds':(end-start).total_seconds(),
          'excluded_after_end':len(ordered)-len(before_end),'point_count_in_window':0,
          'sampling_density_per_hour':None}
    carry=_latest_carry(before_end,start)
    if not carry:
        inside=[r for r in before_end if start<=parse_ts(r.observed_at)<=end]
        base['point_count_in_window']=len(inside)
        base['sampling_density_per_hour']=round(len(inside)/((end-start).total_seconds()/3600),4) if end>start else None
        base['readings']=[{'observed_at':r.observed_at,'value':r.value,'late':r.late} for r in before_end]
        return _quarantine('no_prior_reading','窗前无值：工序开始前没有可用读数，不能向前取值；窗内读数不足以覆盖整窗',base)
    in_window=[r for r in before_end if start<parse_ts(r.observed_at)<=end]
    base['carry_in']={'observed_at':carry.observed_at,'value':carry.value,'late':carry.late}
    base['readings']=[{'observed_at':carry.observed_at,'value':carry.value,'late':carry.late}]+[{'observed_at':r.observed_at,'value':r.value,'late':r.late} for r in in_window]
    base['point_count_in_window']=len(in_window)
    base['sampling_density_per_hour']=round(len(in_window)/((end-start).total_seconds()/3600),4) if end>start else None
    anchors=[(start,carry.value)]+[(parse_ts(r.observed_at),r.value) for r in in_window]
    weighted_sum=0.0;max_gap=0.0;segments=[]
    for index,(moment,value) in enumerate(anchors):
        boundary=anchors[index+1][0] if index+1<len(anchors) else end
        if boundary<=moment:continue
        seconds=(boundary-moment).total_seconds()
        weighted_sum+=value*seconds;max_gap=max(max_gap,seconds)
        segments.append({'from':moment.isoformat(timespec='seconds').replace('+00:00','Z'),
                         'to':boundary.isoformat(timespec='seconds').replace('+00:00','Z'),
                         'seconds':seconds,'value':value})
    base['segments']=segments;base['max_gap_seconds']=max_gap
    weighted=weighted_sum/(end-start).total_seconds()
    if max_gap>gap_limit:
        base['candidate_temperature']=round(weighted,6)
        return _quarantine('gap_exceeded',f'断采超限：窗内最大间隔 {max_gap:g} 秒，超过上限 {gap_limit} 秒',base)
    return {'state':'effective','reason':None,
            'reason_text':'时间加权生效：工序开始沿用窗前前值，窗内读数按LOCF覆盖至工序结束（不使用结束后读数补缺口）',
            'weighted_temperature':round(weighted,6),'coverage_seconds':base['coverage_seconds'],'basis':base}

def _window_dict(w):return {**row(w),'manual_values':dict(w.manual_values or {})}

def _sealed_by_window(db,window_ids):
    if not window_ids:return {}
    rows=db.scalars(select(GeneratedSample).join(MaterializationBatch,MaterializationBatch.id==GeneratedSample.batch_id)
                    .where(GeneratedSample.window_id.in_(window_ids),MaterializationBatch.status=='sealed')
                    .order_by(GeneratedSample.batch_id.desc())).all()
    out={}
    for s in rows:out.setdefault(s.window_id,s)
    return out

def _sealed_sample_dict(s):
    return {'batch_id':s.batch_id,'values':s.values,'state':s.state,'reason':s.reason,'reason_text':s.reason_text,
            'weighted_temperature':s.weighted_temperature,'coverage_seconds':s.coverage_seconds,'basis':s.basis}

def _batch_dict(b):return {**row(b),'summary':dict(b.summary or {})}

def _batch_rows(db,batch_id):
    return list(db.scalars(select(GeneratedSample).where(GeneratedSample.batch_id==batch_id).order_by(GeneratedSample.id)))

def batch_detail(db,id):
    b=db.get(MaterializationBatch,id)
    if not b:raise HTTPException(404,'未找到样本批次')
    src=db.get(Source,b.source_id);dev=db.get(Device,b.device_id)
    rows=_batch_rows(db,b.id)
    expected=(b.summary or {}).get('window_count')
    return {**_batch_dict(b),'source_name':src.name if src else '','source_key':src.key if src else '',
            'device_key':dev.key if dev else '','complete':b.status=='sealed' and expected==len(rows),
            'rows':[{**row(r),'basis':dict(r.basis or {})} for r in rows]}

def list_batches(db,source_id=None,device_id=None,sealed_only=False):
    query=select(MaterializationBatch).order_by(MaterializationBatch.id.desc())
    if source_id:query=query.where(MaterializationBatch.source_id==source_id)
    if device_id:query=query.where(MaterializationBatch.device_id==device_id)
    if sealed_only:query=query.where(MaterializationBatch.status=='sealed')
    out=[]
    for b in db.scalars(query):
        src=db.get(Source,b.source_id);dev=db.get(Device,b.device_id)
        row_count=db.scalar(select(func.count()).select_from(GeneratedSample).where(GeneratedSample.batch_id==b.id))
        out.append({**_batch_dict(b),'source_name':src.name if src else '','device_key':dev.key if dev else '',
                    'row_count':row_count,'complete':b.status=='sealed' and (b.summary or {}).get('window_count')==row_count})
    return out

def device_state(db,id):
    d=device_or_404(db,id);src=db.get(Source,d.source_id)
    windows=list(db.scalars(select(SpecimenWindow).where(SpecimenWindow.device_id==d.id).order_by(SpecimenWindow.started_at)))
    readings=list(db.scalars(select(TemperatureReading).where(TemperatureReading.device_id==d.id).order_by(TemperatureReading.observed_at)))
    conflicts=list(db.scalars(select(ReadingConflict).where(ReadingConflict.device_id==d.id).order_by(ReadingConflict.observed_at.desc())))
    open_conflicts=[c for c in conflicts if c.status=='open']
    sealed=_sealed_by_window(db,[w.id for w in windows])
    previews=[]
    for w in windows:
        evaluation=compute_window(w,readings,d.gap_limit_seconds,d.reported_through,d.key)
        state=evaluation['state'];reason=evaluation.get('reason');reason_text=evaluation.get('reason_text')
        if not d.reported_through or parse_ts(w.ended_at)>parse_ts(d.reported_through):
            state='pending';reason='not_reported_through';reason_text='待报齐：设备尚未确认报齐至工序结束时刻'
        elif any(_affects_window(w,c.observed_at,readings) for c in open_conflicts):
            state='conflict';reason='same_instant_conflict';reason_text='同刻异值待裁定，阻止封窗'
        frozen=sealed.get(w.id)
        previews.append({**_window_dict(w),'state':state,'reason':reason,'reason_text':reason_text,
                         'weighted_temperature':evaluation.get('weighted_temperature'),
                         'coverage_seconds':evaluation.get('coverage_seconds'),'basis':evaluation['basis'],
                         'sealed_sample':_sealed_sample_dict(frozen) if frozen else None})
    window_ids=[w.id for w in windows]
    disputes=list(db.scalars(select(SampleDispute).where(SampleDispute.window_id.in_(window_ids)).order_by(SampleDispute.created_at.desc()).limit(100))) if window_ids else []
    batches=list_batches(db,device_id=d.id)
    return {'device':{**_device_dict(d),'source_name':src.name if src else '','source_key':src.key if src else ''},
            'windows':previews,'readings':[row(r) for r in readings],
            'conflicts':[{**row(c),'candidate_values':list(c.values or [])} for c in conflicts],
            'disputes':[row(x) for x in disputes],'batches':batches,
            'counts':{'windows':len(windows),'readings':len(readings),'late':sum(1 for r in readings if r.late),
                      'pending':sum(1 for p in previews if p['state']=='pending'),
                      'conflicts':len(open_conflicts),'disputes':sum(1 for x in disputes if x.status=='open'),
                      'effective_preview':sum(1 for p in previews if p['state']=='effective'),
                      'quarantined_preview':sum(1 for p in previews if p['state']=='quarantined'),
                      'dirty':sum(1 for w in windows if w.dirty),
                      'sealed_batches':sum(1 for b in batches if b['status']=='sealed')}}

def _validate_sealable(db,d,windows):
    if not windows:raise HTTPException(422,'该设备还没有试件工序窗，无法封窗')
    if not d.reported_through:raise HTTPException(409,'设备尚未确认报齐时刻，不能封窗')
    pending=[w.reference for w in windows if parse_ts(w.ended_at)>parse_ts(d.reported_through)]
    if pending:raise HTTPException(409,'待报齐：设备报齐线未覆盖 '+('、'.join(pending[:8]))+(' 等试件' if len(pending)>8 else ''))
    readings=list(db.scalars(select(TemperatureReading).where(TemperatureReading.device_id==d.id)))
    blocking=[c for c in _open_conflicts(db,d.id) if any(_affects_window(w,c.observed_at,readings) for w in windows)]
    if blocking:
        raise HTTPException(409,'同刻异值阻止封窗：'+('、'.join(c.observed_at for c in blocking[:6])))
def _fingerprint(d,windows,readings):
    payload={'formula':FORMULA,'device':d.key,'gap_limit_seconds':d.gap_limit_seconds,'reported_through':d.reported_through,
             'windows':[{'reference':w.reference,'started_at':w.started_at,'ended_at':w.ended_at,'manual':w.manual_values} for w in sorted(windows,key=lambda x:x.reference)],
             'readings':[{'observed_at':r.observed_at,'value':r.value} for r in sorted(readings,key=lambda x:x.observed_at)]}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def _batch_windows(db,batch,d):
    """批次在创建时固定自己的工序窗集合：恢复写入或重试只处理该集合，
    之后新登记的工序窗属于下一批次。"""
    ids=(batch.summary or {}).get('window_ids')
    if ids:
        rows=list(db.scalars(select(SpecimenWindow).where(SpecimenWindow.id.in_(ids)).order_by(SpecimenWindow.started_at)))
        if len(rows)!=len(set(ids)):raise HTTPException(409,'批次工序窗不完整，请重新封窗')
        return rows
    return list(db.scalars(select(SpecimenWindow).where(SpecimenWindow.device_id==d.id).order_by(SpecimenWindow.started_at)))

def _seal_transaction(db,user,batch,d):
    # 重新锁定设备行与批次行：补报、推进报齐线与封窗在此串行，整批样本一次提交。
    _lock_device(db,d.id)
    batch=db.scalar(select(MaterializationBatch).where(MaterializationBatch.id==batch.id).with_for_update())
    if not batch:raise HTTPException(404,'未找到样本批次')
    if batch.status=='sealed':return batch_detail(db,batch.id)
    windows=_batch_windows(db,batch,d)
    _validate_sealable(db,d,windows)
    readings=list(db.scalars(select(TemperatureReading).where(TemperatureReading.device_id==d.id).order_by(TemperatureReading.observed_at)))
    evaluated=[]
    for w in windows:
        evaluation=compute_window(w,readings,d.gap_limit_seconds,d.reported_through,d.key)
        values=None
        if evaluation['state']=='effective':
            values=dict(w.manual_values or {});values['temperature']=evaluation['weighted_temperature']
            try:values=validate_record(values)
            except ValueError as e:evaluation=_quarantine('invalid_manual_values',str(e),evaluation['basis'])
        evaluated.append((w,evaluation,values))
    db.execute(delete(GeneratedSample).where(GeneratedSample.batch_id==batch.id));db.flush()
    for w,evaluation,values in evaluated:
        db.add(GeneratedSample(batch_id=batch.id,device_id=d.id,source_id=w.source_id,window_id=w.id,reference=w.reference,
                               values=values,basis=evaluation['basis'],state=evaluation['state'],
                               reason=evaluation.get('reason'),reason_text=evaluation.get('reason_text'),
                               weighted_temperature=evaluation.get('weighted_temperature'),
                               coverage_seconds=evaluation.get('coverage_seconds')))
    effective=sum(1 for _,e,_ in evaluated if e['state']=='effective')
    batch.fingerprint=_fingerprint(d,windows,readings)
    batch.summary={'window_count':len(windows),'window_ids':[w.id for w in windows],
                   'effective':effective,'quarantined':len(evaluated)-effective,
                   'reported_through':d.reported_through,'gap_limit_seconds':d.gap_limit_seconds,
                   'formula':FORMULA,'reading_count':len(readings)}
    batch.row_count=len(evaluated)
    batch.status='sealed';batch.sealed_at=now()
    for w in windows:w.dirty=False
    db.flush();log(db,user,'batch.sealed',batch.id,json.dumps(batch.summary,ensure_ascii=False))
    db.commit()
    return batch_detail(db,batch.id)

def seal_batch(db,user,device_id,idempotency_key=None):
    d=_lock_device(db,device_id)
    key=(idempotency_key or '').strip()
    if not key:
        windows=list(db.scalars(select(SpecimenWindow).where(SpecimenWindow.device_id==d.id)))
        _validate_sealable(db,d,windows)
        readings=list(db.scalars(select(TemperatureReading).where(TemperatureReading.device_id==d.id).order_by(TemperatureReading.observed_at)))
        key='auto-'+hashlib.sha256((d.key+'|'+_fingerprint(d,windows,readings)).encode()).hexdigest()[:48]
    existing=db.scalar(select(MaterializationBatch).where(MaterializationBatch.idempotency_key==key))
    if existing:
        if existing.device_id!=d.id:raise HTTPException(409,'该幂等键已用于其他设备')
        return _seal_transaction(db,user,existing,d)
    windows=list(db.scalars(select(SpecimenWindow).where(SpecimenWindow.device_id==d.id)))
    _validate_sealable(db,d,windows)
    batch=MaterializationBatch(source_id=d.source_id,device_id=d.id,status='writing',idempotency_key=key,
                               fingerprint='',summary={'window_count':len(windows),'window_ids':[w.id for w in windows],'phase':'writing'},
                               created_at=now(),created_by=getattr(user,'email',str(user)))
    db.add(batch)
    try:db.commit()
    except IntegrityError:
        db.rollback()
        batch=db.scalar(select(MaterializationBatch).where(MaterializationBatch.idempotency_key==key))
        if not batch:raise
        if batch.device_id!=d.id:raise HTTPException(409,'该幂等键已用于其他设备')
    return _seal_transaction(db,user,batch,d)

def resume_batch(db,user,id):
    b=db.get(MaterializationBatch,id)
    if not b:raise HTTPException(404,'未找到样本批次')
    if b.status=='sealed':return batch_detail(db,id)
    d=_lock_device(db,b.device_id)
    return _seal_transaction(db,user,b,d)

def sealed_research_records(db,batch):
    if batch.status!='sealed':raise HTTPException(409,'研究只冻结完成封窗的完整批次')
    rows=_batch_rows(db,batch.id)
    expected=(batch.summary or {}).get('window_count')
    if expected is None or len(rows)!=expected:
        raise HTTPException(409,'研究只冻结完整批次：该批次样本行不完整，请恢复写入后重试')
    effective=[{'id':r.id,'source_id':r.source_id,'reference':r.reference,'values':r.values,
                'window_id':r.window_id,'batch_id':r.batch_id} for r in rows if r.state=='effective' and r.values]
    quarantined=[{'reference':r.reference,'window_id':r.window_id,'state':r.state,'reason':r.reason,
                  'reason_text':r.reason_text,'basis':r.basis} for r in rows if r.state!='effective']
    return effective,quarantined,rows
