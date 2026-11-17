import random
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from sqlalchemy import select
from src.config.database import Session
from src.config.settings import CONFIG
from src.models.source import Source
from src.models.device import Device,SpecimenWindow,TemperatureReading,ReadingConflict,SampleDispute,MaterializationBatch
from src.services.device_sampling import seal_batch
from src.services.domain_validation import validate_record

SEED_USER=SimpleNamespace(email='seed@'+CONFIG['slug']+'.demo')
BASE=datetime(2026,11,2,1,0,tzinfo=timezone.utc)
GAP_LIMIT=600

def iso(dt):return dt.isoformat(timespec='seconds').replace('+00:00','Z')

def _readings_for(rng,start,end,dense):
    """采样疏密不同：密采 2 分钟一点，稀采 8 分钟一点；均在断采上限内。"""
    step=120 if dense else 480
    out=[];moment=start
    while moment<=end:
        out.append((moment,round(rng.gauss(72 if dense else 66,1.4),2)));moment+=timedelta(seconds=step)
    return out

def seed_device(db,rng):
    source=db.scalar(select(Source).where(Source.key==CONFIG['code']+'-202611-01'))
    if not source:return None
    if db.scalar(select(Device.id).where(Device.key=='DEV-HX-01')):return None
    device=Device(source_id=source.id,key='DEV-HX-01',name='一号热风干燥箱 · 温度日志',gap_limit_seconds=GAP_LIMIT,
                  reported_through=None,created_at=iso(BASE))
    db.add(device);db.flush()
    windows=[]
    for index in range(48):
        day=index//16;slot=index%16
        start=BASE+timedelta(days=day,minutes=slot*30)
        end=start+timedelta(minutes=20)
        reference=f'HX-202611-01-D{day+1}-{slot+1:03}'
        moisture=round(rng.uniform(22,58),2);thickness=round(rng.uniform(1.2,7.6),2);airflow=round(rng.uniform(1.1,4.8),2)
        minutes=round(max(5,12+1.6*moisture+8*thickness-.45*70-3*airflow+0.1*moisture*thickness+rng.gauss(0,3)),2)
        manual=validate_record({'moisture':moisture,'thickness':thickness,'temperature':1.0,'airflow':airflow,'minutes':minutes})
        manual.pop('temperature')
        windows.append((SpecimenWindow(source_id=source.id,device_id=device.id,reference=reference,started_at=iso(start),
                                       ended_at=iso(end),manual_values=manual,dirty=True,created_at=iso(start)),start,end,index%3!=0))
    # 每个窗口开始前 5 分钟有一个读数，作为窗内时间加权的起点
    readings=[(start-timedelta(minutes=5),round(rng.gauss(70,1.2),2)) for _,start,_,_ in windows]
    for window,start,end,dense in windows:readings.extend(_readings_for(rng,start,end,dense))
    first=windows[0]
    readings=[r for r in readings if r[0]!=first[1]-timedelta(minutes=5) and r[0]>first[1]]
    thin=windows[5]
    readings=[r for r in readings if not (thin[1]+timedelta(minutes=2)<r[0]<=thin[1]+timedelta(minutes=12))]
    for window,_,_,_ in windows:db.add(window)
    for moment,value in readings:
        db.add(TemperatureReading(device_id=device.id,observed_at=iso(moment),value=value,received_at=iso(moment),late=False))
    db.flush()
    # 同刻异值：先留冲突记录，再裁定为原值，保留可追溯依据
    window=windows[1]
    reading=db.scalar(select(TemperatureReading).where(TemperatureReading.device_id==device.id,
                       TemperatureReading.observed_at>iso(window[1]),TemperatureReading.observed_at<=iso(window[2]))
                       .order_by(TemperatureReading.observed_at))
    if reading:
        db.add(ReadingConflict(device_id=device.id,observed_at=reading.observed_at,
                               values=[reading.value,round(reading.value+1.5,2)],status='resolved',chosen_value=reading.value,
                               created_at=reading.observed_at,resolved_at=reading.observed_at,resolved_by=SEED_USER.email))
    # 设备确认已报齐至全部窗口结束之后，才允许封窗
    device.reported_through=iso(windows[-1][2]+timedelta(minutes=30))
    db.commit()
    seal_batch(db,SEED_USER,device.id,'seed-batch-1')
    # 封窗后新增一窗：设备尚未报齐到这里，保持“待报齐”
    pending_start=windows[-1][2]+timedelta(minutes=60)
    pending_window=SpecimenWindow(source_id=source.id,device_id=device.id,reference='HX-202611-01-D4-001',
        started_at=iso(pending_start),ended_at=iso(pending_start+timedelta(minutes=20)),
        manual_values={'moisture':34.2,'thickness':4.4,'airflow':3.0,'minutes':81.5},dirty=True,created_at=iso(pending_start))
    db.add(pending_window);db.flush()
    db.add(TemperatureReading(device_id=device.id,observed_at=iso(pending_start-timedelta(minutes=5)),value=69.4,
                              received_at=iso(pending_start-timedelta(minutes=5)),late=False))
    # 封窗后又到一条更早迟到值：保留争议，不改已封样本
    late_moment=windows[2][1]+timedelta(minutes=3)
    late_value=round(rng.gauss(88,1),2)
    db.add(TemperatureReading(device_id=device.id,observed_at=iso(late_moment),value=late_value,
                              received_at=iso(windows[-1][2]+timedelta(minutes=40)),late=True))
    db.flush()
    batch=db.scalar(select(MaterializationBatch).where(MaterializationBatch.idempotency_key=='seed-batch-1'))
    db.add(SampleDispute(source_id=source.id,window_id=windows[2][0].id,batch_id=batch.id if batch else None,
                         observed_at=iso(late_moment),value=late_value,late=True,status='open',
                         note='封窗后收到更早迟到值，保留争议；已封样本不变',
                         created_at=iso(windows[-1][2]+timedelta(minutes=41))))
    db.commit()
    return device.id

def seed():
    with Session() as db:
        return seed_device(db,random.Random(4201))
