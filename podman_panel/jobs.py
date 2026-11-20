"""Background jobs: a sequence of commands run as one account, with captured output."""
import itertools
import os
import signal
import threading
import time

MAX_OUTPUT = 2_000_000
_ids = itertools.count(1)


class Job:
    def __init__(self, title, account, target, steps, on_done=None, serial=None):
        self.id = next(_ids)
        self.title, self.account, self.target = title, account, target
        self.steps = steps  # list of (argv, cwd or None)
        self.status = "queued"
        self.output = ""
        self.started = time.time()
        self.ended = None
        self.proc = None
        self.on_done = on_done
        self.serial = serial  # a lock shared by jobs that must run one after another
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            self.output += text
            if len(self.output) > MAX_OUTPUT:
                self.output = "…(truncated)…\n" + self.output[-MAX_OUTPUT // 2:]

    def run(self, acc):
        if self.serial:
            with self.serial:
                if self.status != "cancelled":
                    self._run(acc)
                else:
                    self.ended = time.time()
            return
        self._run(acc)

    def _run(self, acc):
        self.status = "running"
        try:
            for argv, cwd in self.steps:
                self.write(f"$ {' '.join(argv)}\n")
                self.proc = acc.popen(argv, cwd=cwd)
                for line in self.proc.stdout:
                    self.write(line)
                code = self.proc.wait()
                if self.status == "cancelled":
                    break
                if code != 0:
                    self.write(f"\n✗ exit code {code}\n")
                    self.status = "failed"
                    break
            else:
                self.status = "succeeded"
        except Exception as e:  # report, never kill the server thread
            self.write(f"\n✗ {e}\n")
            self.status = "failed"
        finally:
            self.ended = time.time()
            self.proc = None
            if self.on_done:
                self.on_done(self)

    def cancel(self):
        if self.status not in ("queued", "running"):
            return
        self.status = "cancelled"
        p = self.proc
        if p:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def summary(self):
        return {"id": self.id, "title": self.title, "account": self.account, "target": self.target,
                "status": self.status, "started": self.started, "ended": self.ended}


class JobManager:
    def __init__(self, keep=100):
        self.jobs = {}
        self.keep = keep
        self.lock = threading.Lock()
        self.serial = {}  # account -> lock for queued batches (update all)

    def busy(self, account, target):
        return any(j.account == account and j.target == target and j.status in ("queued", "running")
                   for j in self.jobs.values())

    def start(self, acc, title, target, steps, on_done=None, queued=False):
        with self.lock:
            if self.busy(acc.name, target):
                raise ValueError(f"a job for {target} is already running")
            serial = self.serial.setdefault(acc.name, threading.Lock()) if queued else None
            job = Job(title, acc.name, target, steps, on_done, serial)
            self.jobs[job.id] = job
            for old in sorted(self.jobs)[:-self.keep]:
                if self.jobs[old].status not in ("queued", "running"):
                    del self.jobs[old]
        threading.Thread(target=job.run, args=(acc,), daemon=True).start()
        return job

    def list(self):
        return [j.summary() for j in sorted(self.jobs.values(), key=lambda j: -j.id)]

    def get(self, job_id):
        return self.jobs.get(job_id)
