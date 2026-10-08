import atexit
import codecs
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from .security import resolve


COMMANDS = {}
LOCK = threading.Lock()


def available():
    return sys.platform.startswith("linux")


class Command:
    def __init__(self, command, root, path, folder):
        self.id = uuid.uuid4().hex
        self.command = command
        self.root = root
        self.path = path
        self.created = time.time()
        self.output = ""
        self.truncated = False
        self.status = "running"
        self.code = None
        self.lock = threading.Lock()
        self.process = subprocess.Popen([shutil.which("bash") or "/bin/sh", "-c", command], cwd=str(folder),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        start_new_session=True, env={**os.environ, "PYTHONUNBUFFERED": "1", "TERM": "dumb"})

    def read(self):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := self.process.stdout.read1(16384):
                with self.lock:
                    output = self.output + decoder.decode(chunk)
                    self.truncated = self.truncated or len(output) > 1000000
                    self.output = output[-1000000:]
            with self.lock:
                self.output += decoder.decode(b"", final=True)
        finally:
            self.process.stdout.close()
            code = self.process.wait()
            with self.lock:
                self.code = code
                self.status = "cancelled" if self.status == "stopping" else "completed" if code == 0 else "failed"

    def stop(self):
        with self.lock:
            if self.status != "running":
                return
            self.status = "stopping"
        self.signal(signal.SIGTERM)
        timer = threading.Timer(3, lambda: self.signal(signal.SIGKILL))
        timer.daemon = True
        timer.start()

    def signal(self, value):
        with self.lock:
            if self.status not in ("running", "stopping"):
                return
        try:
            os.killpg(self.process.pid, value)
        except ProcessLookupError:
            pass

    def public(self):
        with self.lock:
            return {"id": self.id, "command": self.command, "root": self.root, "path": self.path, "created": self.created,
                    "output": self.output, "truncated": self.truncated, "status": self.status, "code": self.code}


def run(command, root, path):
    if not available():
        raise ValueError("The server terminal is available when ComfyUI runs on Linux")
    if not isinstance(command, str) or not command.strip() or len(command) > 16000 or "\x00" in command:
        raise ValueError("Enter a command of up to 16000 characters")
    folder = resolve(root, path, must_exist=True)
    if not folder.is_dir():
        raise ValueError("Choose a working folder")
    with LOCK:
        if sum(item.status in ("running", "stopping") for item in COMMANDS.values()) >= 3:
            raise ValueError("Stop a running command before starting another (maximum 3)")
        job = Command(command, root, path, folder)
        COMMANDS[job.id] = job
        for identifier in list(COMMANDS):
            if len(COMMANDS) <= 30:
                break
            if COMMANDS[identifier].status not in ("running", "stopping"):
                del COMMANDS[identifier]
    threading.Thread(target=job.read, name="robot-terminal", daemon=True).start()
    return job


def snapshot():
    with LOCK:
        return [item.public() for item in reversed(list(COMMANDS.values()))]


def stop(identifier):
    with LOCK:
        job = COMMANDS[identifier]
    job.stop()
    return job


@atexit.register
def shutdown():
    with LOCK:
        for job in COMMANDS.values():
            job.signal(signal.SIGTERM)
