"""Bounded volatile conversations. No transcript, evidence or jobs are written to disk."""
import copy
import threading
import time
import uuid


class SessionGone(Exception):
    pass


class Sessions:
    def __init__(self, ttl=1800, capacity=32):
        self.ttl, self.capacity = ttl, capacity
        self.items = {}
        self.lock = threading.RLock()

    def prune(self):
        cutoff = time.monotonic() - self.ttl
        for key in list(self.items):
            if self.items[key]['touched'] < cutoff:
                del self.items[key]

    def create(self, cid, mode, engine='cluster'):
        with self.lock:
            self.prune()
            if len(self.items) >= self.capacity:
                raise ValueError('Слишком много диалогов. Закройте лишние или дождитесь истечения 30 минут.')
            sid = uuid.uuid4().hex
            self.items[sid] = dict(cluster_id=cid, mode=mode, engine=engine, touched=time.monotonic(), jobs={})
            return sid

    def get(self, sid):
        with self.lock:
            self.prune()
            if sid not in self.items:
                raise SessionGone()
            self.items[sid]['touched'] = time.monotonic()
            return copy.deepcopy(self.items[sid])

    def add(self, sid, job):
        with self.lock:
            session = self.get(sid)
            if any(j['status'] in ('queued', 'running') for j in session['jobs'].values()):
                raise ValueError('Дождитесь предыдущего ответа.')
            if session.get('scenario_target'):
                # Fixed actions never use earlier answers as history. Keep only
                # a small polling window, so repeated checks do not hit a chat
                # transcript limit or retain an ever-growing evidence journal.
                for key in list(self.items[sid]['jobs'])[:-2]:
                    del self.items[sid]['jobs'][key]
            elif len(session['jobs']) >= 20:
                raise ValueError('Достигнут лимит 20 сообщений. Начните новый диалог.')
            self.items[sid]['jobs'][job['id']] = job

    def bind_scenario(self, sid, target, generation):
        """Bind a volatile diagnostic session to one immutable object identity."""
        with self.lock:
            session = self.get(sid)
            previous = session.get('scenario_target')
            def identity(value):
                return tuple(value.get(k, '') for k in ('cluster_id', 'kind', 'namespace', 'name', 'uid', 'metric'))
            if previous is not None and (identity(previous) != identity(target) or session.get('scenario_generation') != generation):
                raise ValueError('Объект или подключение изменилось. Откройте диагностику заново.')
            if previous is None:
                self.items[sid]['scenario_target'] = copy.deepcopy(target)
                self.items[sid]['scenario_generation'] = generation
            return copy.deepcopy(self.items[sid]['scenario_target'])

    def update(self, sid, jid, fields):
        with self.lock:
            self.prune()
            if sid not in self.items:
                raise SessionGone()
            self.items[sid]['touched'] = time.monotonic()
            if jid not in self.items[sid]['jobs']:
                raise SessionGone()
            self.items[sid]['jobs'][jid].update(fields)

    def job(self, sid, jid):
        session = self.get(sid)
        if jid not in session['jobs']:
            raise SessionGone()
        return session['jobs'][jid]

    def delete(self, sid):
        with self.lock:
            self.items.pop(sid, None)
