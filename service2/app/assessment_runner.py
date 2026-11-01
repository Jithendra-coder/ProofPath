import json
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass


DEFAULT_IMAGE = "docker.io/library/python@sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe"
HARNESS = r'''import contextlib, io, json, sys, traceback
payload = json.load(sys.stdin)
namespace = {"__name__": "candidate_solution"}
try:
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(payload["code"], "solution.py", "exec"), namespace)
        function = namespace[payload["entrypoint"]]
        if not callable(function):
            raise TypeError("entrypoint is not callable")
        value = function(**payload["arguments"])
    result = {"ok": True, "value": value}
except BaseException:
    result = {"ok": False}
try:
    sys.stdout.write(json.dumps(result, separators=(",", ":"), allow_nan=False))
except BaseException:
    sys.stdout.write('{"ok":false}')
'''


@dataclass(frozen=True)
class Execution:
    status: str
    value: object = None


class PodmanAssessmentRunner:
    def __init__(self, image: str = DEFAULT_IMAGE, executable: str | None = None):
        self.image = image
        self.executable = executable or shutil.which("podman")

    def available(self) -> bool:
        if not self.executable:
            return False
        try:
            image = subprocess.run(
                [self.executable, "image", "exists", self.image], capture_output=True, timeout=3
            )
            engine = subprocess.run(
                [self.executable, "info", "--format", "json"], capture_output=True, timeout=3
            )
            info = json.loads(engine.stdout) if engine.returncode == 0 else {}
            return image.returncode == 0 and info.get("host", {}).get("security", {}).get("rootless") is True
        except (OSError, subprocess.TimeoutExpired):
            return False

    def run_case(self, code: str, entrypoint: str, arguments: dict) -> Execution:
        if not self.executable:
            return Execution("infrastructure_failed")
        name = f"proofpath-{uuid.uuid4().hex}"
        command = [
            self.executable, "run", "--rm", "--interactive", "--name", name,
            "--network=none", "--read-only",
            "--tmpfs", "/tmp:rw,size=16m,mode=1777,noexec,nosuid,nodev",
            "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=32", "--memory=192m", "--cpus=0.5", "--user=65534:65534",
            "--timeout=6", self.image, "python", "-I", "-S", "-c", HARNESS,
        ]
        payload = json.dumps({"code": code, "entrypoint": entrypoint, "arguments": arguments}, allow_nan=False)
        stdout, stderr = bytearray(), bytearray()
        overflow = threading.Event()

        def drain(stream, target):
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                if len(target) + len(chunk) > 65536:
                    overflow.set()
                elif not overflow.is_set():
                    target.extend(chunk)

        process = None
        try:
            started = time.monotonic()
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 12
            readers = [
                threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
                threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
            ]
            for reader in readers:
                reader.start()
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.close()
            while process.poll() is None:
                if overflow.is_set():
                    process.terminate()
                    return Execution("infrastructure_failed")
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    return Execution("infrastructure_failed")
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode == 124 or (
                process.returncode == 4294967295 and time.monotonic() - started >= 5.5 and not stderr
            ):
                return Execution("timed_out")
            if process.returncode != 0:
                return Execution("infrastructure_failed")
            for reader in readers:
                reader.join(timeout=1)
            if overflow.is_set():
                return Execution("infrastructure_failed")
            try:
                result = json.loads(stdout.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return Execution("candidate_error")
            return Execution("completed", result["value"]) if result.get("ok") else Execution("candidate_error")
        except (OSError, BrokenPipeError):
            return Execution("infrastructure_failed")
        finally:
            if process and process.poll() is None:
                process.terminate()
            try:
                subprocess.run([self.executable, "rm", "-f", name], capture_output=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
