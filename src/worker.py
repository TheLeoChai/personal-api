# /app/worker.py
import os, subprocess
import redis
from rq import Queue, Worker

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
redis_conn = redis.from_url(REDIS_URL)
q = Queue(connection=redis_conn)

def run_script(script: str, *args):
    """
    Run a script inside the image at /app/scripts/<script>.
    Returns {returncode, stdout, stderr}.
    """
    path = os.path.join("/app/scripts", script)
    res = subprocess.run(["python", path, *args], capture_output=True, text=True)
    return {"returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr}

if __name__ == "__main__":
    # RQ >= 2.0: pass connection directly, don't use Connection ctx
    worker = Worker([q], connection=redis_conn)
    worker.work()
