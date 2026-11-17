from sqlalchemy import Column,Integer,String,JSON,Text,Float,Boolean,ForeignKey,UniqueConstraint
from ..config.database import Base
from ..config.settings import CONFIG

class Device(Base):
    __tablename__='device_streams'
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    key=Column(String(120),unique=True,nullable=False)
    name=Column(String(160),nullable=False)
    gap_limit_seconds=Column(Integer,nullable=False,default=300)
    reported_through=Column(String(40),nullable=True)
    created_at=Column(String(40),nullable=False)

class SpecimenWindow(Base):
    __tablename__='specimen_windows'
    __table_args__=(UniqueConstraint('source_id','reference',name='uq_window_reference'),)
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    device_id=Column(Integer,ForeignKey('device_streams.id'),nullable=False)
    reference=Column(String(120),nullable=False)
    started_at=Column(String(40),nullable=False)
    ended_at=Column(String(40),nullable=False)
    manual_values=Column(JSON,nullable=False)
    dirty=Column(Boolean,nullable=False,default=True)
    created_at=Column(String(40),nullable=False)

class TemperatureReading(Base):
    __tablename__='temperature_readings'
    __table_args__=(UniqueConstraint('device_id','observed_at',name='uq_reading_instant'),)
    id=Column(Integer,primary_key=True)
    device_id=Column(Integer,ForeignKey('device_streams.id'),nullable=False)
    observed_at=Column(String(40),nullable=False,index=True)
    value=Column(Float,nullable=False)
    received_at=Column(String(40),nullable=False)
    late=Column(Boolean,nullable=False,default=False)

class ReadingConflict(Base):
    __tablename__='reading_conflicts'
    __table_args__=(UniqueConstraint('device_id','observed_at',name='uq_reading_conflict'),)
    id=Column(Integer,primary_key=True)
    device_id=Column(Integer,ForeignKey('device_streams.id'),nullable=False)
    observed_at=Column(String(40),nullable=False)
    values=Column(JSON,nullable=False)
    status=Column(String(20),nullable=False,default='open')
    chosen_value=Column(Float,nullable=True)
    created_at=Column(String(40),nullable=False)
    resolved_at=Column(String(40),nullable=True)
    resolved_by=Column(String(120),nullable=True)

class MaterializationBatch(Base):
    __tablename__='sample_batches'
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,ForeignKey(CONFIG['source_table']+'.id'),nullable=False)
    device_id=Column(Integer,ForeignKey('device_streams.id'),nullable=False)
    status=Column(String(20),nullable=False,default='writing')
    idempotency_key=Column(String(160),unique=True,nullable=True)
    fingerprint=Column(String(64),nullable=False,default='')
    summary=Column(JSON,nullable=False,default=dict)
    row_count=Column(Integer,nullable=False,default=0)
    created_at=Column(String(40),nullable=False)
    sealed_at=Column(String(40),nullable=True)
    created_by=Column(String(120),nullable=False)

class GeneratedSample(Base):
    __tablename__='generated_samples'
    __table_args__=(UniqueConstraint('batch_id','reference',name='uq_batch_reference'),)
    id=Column(Integer,primary_key=True)
    batch_id=Column(Integer,ForeignKey('sample_batches.id'),nullable=False)
    device_id=Column(Integer,ForeignKey('device_streams.id'),nullable=False)
    source_id=Column(Integer,nullable=False)
    window_id=Column(Integer,ForeignKey('specimen_windows.id'),nullable=False)
    reference=Column(String(120),nullable=False)
    values=Column(JSON,nullable=True)
    basis=Column(JSON,nullable=False)
    state=Column(String(20),nullable=False)
    reason=Column(String(80),nullable=True)
    reason_text=Column(Text,nullable=True)
    weighted_temperature=Column(Float,nullable=True)
    coverage_seconds=Column(Float,nullable=True)

class SampleDispute(Base):
    __tablename__='sample_disputes'
    __table_args__=(UniqueConstraint('window_id','observed_at','value',name='uq_window_dispute'),)
    id=Column(Integer,primary_key=True)
    source_id=Column(Integer,nullable=False)
    window_id=Column(Integer,ForeignKey('specimen_windows.id'),nullable=True)
    batch_id=Column(Integer,nullable=True)
    observed_at=Column(String(40),nullable=False)
    value=Column(Float,nullable=False)
    late=Column(Boolean,nullable=False,default=False)
    status=Column(String(20),nullable=False,default='open')
    note=Column(Text,default='')
    created_at=Column(String(40),nullable=False)
    resolved_at=Column(String(40),nullable=True)
    resolved_by=Column(String(120),nullable=True)
