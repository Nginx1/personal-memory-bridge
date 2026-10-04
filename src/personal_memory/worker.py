"""Bounded extraction worker. A failure stops this worker, without auto-requeue."""
import time


def watch(archive,backend,scope,interval,limit,emit):
    if interval<30 or not 1<=limit<=20:
        raise ValueError('Worker interval >=30 seconds and batch limit 1..20 required')
    while True:
        result=archive.push(backend,scope=scope,limit=limit)
        emit(result)
        time.sleep(interval)
