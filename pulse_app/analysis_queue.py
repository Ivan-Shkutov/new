"""Bounded admission for the model shared with other services.

Only pending futures can be cancelled here. An active remote generation is not
claimed to stop when a browser closes or a client times out.
"""
from collections import deque
from concurrent.futures import Future
import os
import threading
import time


def configuration(environ=None):
    environ = os.environ if environ is None else environ
    result = {}
    for field, name, default, low, high in (
        ('workers', 'PULSE_AI_WORKERS', 1, 1, 4),
        ('queued', 'PULSE_AI_QUEUE', 1, 0, 8),
        ('wait_seconds', 'PULSE_AI_QUEUE_WAIT', 60, 1, 600),
    ):
        try: value = int(environ.get(name, str(default)))
        except (ValueError, TypeError): raise ValueError(f'{name}: required integer {low}..{high}') from None
        if not low <= value <= high: raise ValueError(f'{name}: required integer {low}..{high}')
        result[field] = value
    return result


class AnalysisExecutor:
    def __init__(self, workers=1, queued=1, wait_seconds=60, clock=time.monotonic):
        self.settings = {'workers': workers, 'queued': queued, 'wait_seconds': wait_seconds}
        self.clock = clock
        self.slots = threading.BoundedSemaphore(workers + queued)
        self.pending = {}
        self.ready = deque()
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        self.workers = [threading.Thread(target=self._work, name=f'pulse-investigation-{i}', daemon=True)
                        for i in range(workers)]
        for worker in self.workers: worker.start()

    def acquire(self, blocking=False):
        return self.slots.acquire(blocking=blocking)

    def release(self):
        self.slots.release()

    def submit(self, fn, *args, session_id='', on_expired=None, **kwargs):
        future = Future()
        def completed(done):
            with self.condition:
                self.pending.pop(done, None)
                try: self.ready.remove(done)
                except ValueError: pass
            # Every admitted request returns exactly one slot on every outcome.
            self.release()
            if done.cancelled():
                if on_expired: on_expired()
        with self.condition:
            if self.closed: raise RuntimeError('Analysis executor has shut down')
            self.pending[future] = (session_id, self.clock() + self.settings['wait_seconds'], fn, args, kwargs)
            future.add_done_callback(completed)
            self.ready.append(future)
            self.condition.notify()
        return future

    def _work(self):
        while True:
            with self.condition:
                while not self.ready and not self.closed: self.condition.wait()
                if not self.ready: return
                future = self.ready.popleft()
                entry = self.pending.get(future)
                if entry is None: continue
                if self.clock() >= entry[1]:
                    future.cancel()
                    future = entry = None
                    continue
                if not future.set_running_or_notify_cancel(): continue
            try:
                result = entry[2](*entry[3], **entry[4])
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
                result = None
            finally:
                # Do not retain the previous incident while an idle worker waits.
                future = entry = None

    def expire(self):
        now = self.clock()
        with self.condition:
            overdue = [future for future, entry in self.pending.items() if now >= entry[1]]
        for future in overdue: future.cancel()

    def cancel_session(self, session_id):
        with self.condition:
            pending = [future for future, entry in self.pending.items() if entry[0] == session_id]
        for future in pending: future.cancel()

    def shutdown(self, wait=True, cancel_futures=False):
        with self.condition:
            self.closed = True
            pending = list(self.ready) if cancel_futures else []
            for future in pending: future.cancel()
            self.condition.notify_all()
        if wait:
            for worker in self.workers: worker.join()
