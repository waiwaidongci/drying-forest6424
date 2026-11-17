from pydantic import BaseModel,Field
class DeviceInput(BaseModel):
    source_id:int
    key:str=Field(min_length=1,max_length=120)
    name:str=Field(min_length=2,max_length=160)
    gap_limit_seconds:int=Field(default=300,ge=1,le=86400)
class WindowInput(BaseModel):
    reference:str=Field(min_length=1,max_length=120)
    started_at:str
    ended_at:str
    moisture:float
    thickness:float
    airflow:float
    minutes:float
class WindowBatch(BaseModel):
    windows:list[WindowInput]=Field(min_length=1,max_length=500)
class ReadingInput(BaseModel):
    observed_at:str
    value:float
class ReadingBatch(BaseModel):
    readings:list[ReadingInput]=Field(min_length=1,max_length=5000)
class WatermarkInput(BaseModel):
    reported_through:str
class ConflictResolution(BaseModel):
    value:float
class DisputeResolution(BaseModel):
    note:str=Field(default='',max_length=2000)
class SealInput(BaseModel):
    idempotency_key:str=Field(min_length=1,max_length=160)
