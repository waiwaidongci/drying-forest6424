from ..services import device_sampling
class DeviceController:
    list=staticmethod(device_sampling.list_devices)
    create=staticmethod(device_sampling.create_device)
    state=staticmethod(device_sampling.device_state)
    add_windows=staticmethod(device_sampling.add_windows)
    ingest=staticmethod(device_sampling.ingest_readings)
    watermark=staticmethod(device_sampling.advance_watermark)
    resolve_conflict=staticmethod(device_sampling.resolve_conflict)
    resolve_dispute=staticmethod(device_sampling.resolve_dispute)
    seal=staticmethod(device_sampling.seal_batch)
    resume=staticmethod(device_sampling.resume_batch)
    batches=staticmethod(device_sampling.list_batches)
    batch=lambda db,id:device_sampling.batch_detail(db,id)
