"""Run real local drills without changing the provided serving/state/chaos code.

Usage: python dr/drill.py

Linux uses the prescribed bare --mock chaos CLI (SIGSTOP/SIGCONT).
Windows uses native process suspension/resumption and explicitly records
backend=windows, mock=false. Its logs are real evidence but are NOT a substitute
for verifying the Linux bare --mock execution required by GUIDE.md.

Existing evidence is archived under reports/archive/<timestamp>/ before a rerun.
Only processes started by this command are suspended, resumed, or terminated.
"""
import argparse
import ctypes
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chaos import kill_region as chaos  # noqa: E402
from state import seed_vectors  # noqa: E402
from tools.measure_rto import measure  # noqa: E402

EVIDENCE = (
    "chaos/chaos-events.jsonl", "reports/drill-1-nodr.jsonl",
    "reports/drill-2-withdr.jsonl", "reports/health-events.jsonl",
    "reports/failover-events.jsonl", "reports/replication.jsonl",
    "reports/runbook-run.jsonl", "reports/measure-drill-1.json",
    "reports/measure-drill-2.json", "reports/environment.json",
)


def windows_process_tree(pid: int) -> list[int]:
    """Include the worker behind the Windows venv python.exe redirector."""
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        function.restype = wintypes.BOOL
    snapshot_handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot_handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot_handle, ctypes.byref(entry))
        while found:
            parents[entry.th32ProcessID] = entry.th32ParentProcessID
            found = kernel.Process32NextW(snapshot_handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot_handle)
    result = [pid]
    for parent in result:
        result.extend(child for child, owner in parents.items() if owner == parent and child not in result)
    return result


def windows_pause(pid: int, pause: bool):
    """Suspend/resume one owned process using a checked process handle."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        native = ctypes.WinDLL("ntdll")
        function = native.NtSuspendProcess if pause else native.NtResumeProcess
        function.argtypes, function.restype = [wintypes.HANDLE], wintypes.LONG
        status = function(handle)
        if status != 0:
            raise OSError(f"native suspend/resume failed: NTSTATUS=0x{status & 0xffffffff:08x}")
    finally:
        kernel.CloseHandle(handle)


class Drill:
    def __init__(self):
        self.children = []
        self.streams = []
        self.regions = {}
        self.paused = set()
        self.suspended_pids = []

    def spawn(self, name, *arguments, env=None):
        stream = (ROOT / "run" / f"{name}.log").open("w", encoding="utf-8")
        self.streams.append(stream)
        process = subprocess.Popen(
            [sys.executable, *arguments], cwd=ROOT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", **(env or {})},
            stdout=stream, stderr=subprocess.STDOUT,
        )
        self.children.append(process)
        return process

    def services(self):
        for region, port in (("a", 8001), ("b", 8002)):
            process = self.spawn(f"region-{region}", "-m", "uvicorn", "serving.app:app",
                                 "--host", "127.0.0.1", "--port", str(port),
                                 "--log-level", "warning",
                                 env={"REGION": region, "STATE_DIR": f"state/region-{region}",
                                      "WARMUP_SECONDS": "6", "MIN_VECTORS": "1"})
            self.regions[region] = process
            (ROOT / "run" / f"region-{region}.pid").write_text(str(process.pid))
        edge = self.spawn("edge", "-m", "uvicorn", "edge.proxy:app",
                          "--host", "127.0.0.1", "--port", "8080", "--log-level", "warning",
                          env={"EDGE_TTL_SECONDS": "5", "EDGE_TIMEOUT_SECONDS": "2",
                               "ACTIVE_REGION_FILE": "edge/active_region",
                               "REGION_A_URL": "http://127.0.0.1:8001",
                               "REGION_B_URL": "http://127.0.0.1:8002"})
        (ROOT / "run" / "edge.pid").write_text(str(edge.pid))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=1) as client:
                    checks = [client.get(f"http://127.0.0.1:{port}{path}").status_code
                              for port, path in ((8001, "/readyz"), (8002, "/healthz"),
                                                 (8080, "/v1/infer"))]
                if checks == [200, 200, 200]:
                    print("SERVICES ready: A ready, B alive, edge serves A", flush=True)
                    return
            except httpx.RequestError:
                pass
            if any(child.poll() is not None for child in self.children):
                raise RuntimeError("service exited; inspect run/*.log")
            time.sleep(0.5)
        raise TimeoutError("services did not become available")

    def wait_first_request(self, path):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            file = ROOT / path
            if file.exists() and file.stat().st_size:
                record = json.loads(file.read_text(encoding="utf-8").splitlines()[0])
                if not record.get("ok") or record.get("served_by") != "a":
                    raise RuntimeError("traffic must initially be served successfully by A")
                return
            time.sleep(0.1)
        raise TimeoutError("load generator did not record a request")

    def wait_health_transition(self, region, target, checker, timeout=45):
        deadline = time.monotonic() + timeout
        health_path = ROOT / "reports/health-events.jsonl"
        while time.monotonic() < deadline:
            events = [json.loads(line) for line in health_path.read_text(encoding="utf-8").splitlines()
                      if line.strip()] if health_path.exists() else []
            if any(event.get("region") == region and event.get("to") == target for event in events):
                return
            if checker.poll() is not None:
                raise RuntimeError("health checker exited before the expected transition")
            time.sleep(0.1)
        raise TimeoutError(f"checker did not report {region} {target}")

    def attack(self):
        if os.name != "nt":
            subprocess.run([sys.executable, "chaos/kill_region.py", "--region", "a",
                            "--mode", "netblock", "--mock"], check=True, cwd=ROOT)
        else:
            other_alive = chaos.is_alive("b")
            if not other_alive:
                raise RuntimeError("refusing double outage: region B is not alive")
            process = self.regions["a"]
            if process.poll() is not None:
                raise RuntimeError("region A already exited")
            # Emit the start immediately before the real OS operation, matching
            # the original chaos script; no synthetic timing is introduced.
            chaos.event(action="kill", region="a", mode="netblock", backend="windows",
                        mock=False, other_region="b", other_alive=other_alive,
                        forced_both=False, pid=process.pid, method="NtSuspendProcess(process_tree)",
                        note="real Windows process suspension; not Linux bare --mock")
            self.paused.add("a")
            for pid in reversed(windows_process_tree(process.pid)):
                windows_pause(pid, True)
                self.suspended_pids.append(pid)
        self.paused.add("a")

    def restore(self):
        if "a" not in self.paused:
            return
        if os.name != "nt":
            subprocess.run([sys.executable, "chaos/kill_region.py", "restore",
                            "--region", "a", "--backend", "bare"], check=True, cwd=ROOT)
        else:
            for pid in reversed(self.suspended_pids):
                windows_pause(pid, False)
            self.suspended_pids.clear()
            chaos.event(action="restore", region="a", backend="windows",
                        method="NtResumeProcess", pid=self.regions["a"].pid)
        self.paused.remove("a")

    def cleanup(self):
        try:
            self.restore()
        finally:
            for process in reversed(self.children):
                if process.poll() is None:
                    process.terminate()
            for process in self.children:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in self.streams:
                stream.close()
            for name in ("region-a", "region-b", "edge"):
                (ROOT / "run" / f"{name}.pid").unlink(missing_ok=True)


def save_measure(drill_number):
    load = "reports/drill-1-nodr.jsonl" if drill_number == 1 else "reports/drill-2-withdr.jsonl"
    result = measure(load, "chaos/chaos-events.jsonl", "reports/health-events.jsonl",
                     "reports/failover-events.jsonl", 300)
    (ROOT / "reports" / f"measure-drill-{drill_number}.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"DRILL {drill_number}: " + json.dumps(result, ensure_ascii=False), flush=True)
    return result


def prepare():
    import socket

    for port in (8001, 8002, 8080):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"port {port} is occupied; stop that service before the drill")
    (ROOT / "run").mkdir(exist_ok=True)
    (ROOT / "reports").mkdir(exist_ok=True)
    # Preserve old evidence and generated state rather than silently overwriting
    # a previous submission. Source files and report documents are untouched.
    paths = [ROOT / name for name in (*EVIDENCE, "state/region-a", "state/region-b", "state/_replica")]
    paths = [path for path in paths if path.exists()]
    if paths:
        archive = ROOT / "reports" / "archive" / time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        archive.mkdir(parents=True)
        for path in paths:
            relative = path.relative_to(ROOT)
            destination = archive / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(destination))
    seed_vectors.seed("a", 200, 2)
    seed_vectors.seed("b", 0, 0)
    (ROOT / "edge" / "active_region").write_text("a", encoding="utf-8")
    environment = {"ts": time.time(), "python": sys.version,
                   "platform": sys.platform, "chaos_backend": "windows" if os.name == "nt" else "bare",
                   "kernel_release": platform.release(),
                   "distribution": os.environ.get("LAB_LINUX_DISTRIBUTION"),
                   "wsl_version": os.environ.get("LAB_WSL_VERSION"),
                   "mock": os.name != "nt", "warmup_seconds": 6, "edge_ttl_seconds": 5,
                   "health_interval_s": 5, "threshold": 3, "snapshot_every_s": 30,
                   "snapshot_backend": "fs",
                   "graded_linux_path_verified": False}
    (ROOT / "reports" / "environment.json").write_text(
        json.dumps(environment, indent=2) + "\n", encoding="utf-8")


def run():
    os.chdir(ROOT)
    prepare()
    drill = Drill()
    try:
        drill.services()
        traffic = drill.spawn("loadgen-1", "loadgen/traffic.py", "--duration", "40",
                              "--rps", "2", "--out", "reports/drill-1-nodr.jsonl")
        drill.wait_first_request("reports/drill-1-nodr.jsonl")
        time.sleep(8)
        drill.attack()
        if traffic.wait(timeout=50) != 0:
            raise RuntimeError("baseline load generator failed")
        first = save_measure(1)
        if not first["valid"] or first["rto_verdict"] != "NO_RECOVERY":
            raise RuntimeError("baseline did not demonstrate an outage without recovery")
        drill.restore()
        if not chaos.is_ready("a"):
            raise RuntimeError("region A failed to recover before the second drill")

        ingest = drill.spawn("ingest", "state/ingest.py", "--region", "a", "--rate", "0.5",
                             "--duration", "150")
        replication = drill.spawn("replication", "state/replicate.py", "--every", "30",
                                  "--duration", "150", "--backend", "fs")
        time.sleep(5)
        if not (ROOT / "state/_replica/dr-artifacts/MANIFEST.json").exists():
            raise RuntimeError("first replication cycle did not complete")
        traffic = drill.spawn("loadgen-2", "loadgen/traffic.py", "--duration", "100",
                              "--rps", "2", "--out", "reports/drill-2-withdr.jsonl")
        checker = drill.spawn("health-checker", "dr/health_checker.py", "--interval", "5",
                              "--threshold", "3", "--duration", "100",
                              "--out", "reports/health-events.jsonl")
        drill.wait_first_request("reports/drill-2-withdr.jsonl")
        # B starts alive but unready. Its third failed probe is logged after
        # the same cycle's successful A probe. Inject immediately after that
        # completed cycle, so the full next three poll cycles are observed.
        # A fixed sleep can inject halfway through a cycle and make the lab's
        # nominal interval*threshold evidence gate depend on process startup.
        drill.wait_health_transition("b", "UNHEALTHY", checker, timeout=30)
        drill.attack()
        # Let the independent checker raise the alert before the operator
        # runbook starts. This makes detection part of the measured RTO.
        drill.wait_health_transition("a", "UNHEALTHY", checker)
        runbook = drill.spawn("runbook", "dr/runbook.py", "--primary", "a", "--target", "b",
                              "--backend", "fs", "--auto")
        if runbook.wait(timeout=90) != 0:
            raise RuntimeError("runbook failed; inspect run/runbook.log")
        for name, child in (("traffic", traffic), ("checker", checker),
                            ("ingest", ingest), ("replication", replication)):
            if child.wait(timeout=160) != 0:
                raise RuntimeError(f"{name} exited unsuccessfully")
        second = save_measure(2)
        if not second["valid"] or second["warnings"] or second["rto_verdict"] != "PASS":
            raise RuntimeError("recovery drill failed its evidence gates")
        environment_path = ROOT / "reports/environment.json"
        environment = json.loads(environment_path.read_text(encoding="utf-8"))
        environment["graded_linux_path_verified"] = sys.platform.startswith("linux")
        environment_path.write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")
        return second
    finally:
        drill.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    run()
