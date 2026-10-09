"""Package source plus all referenced evidence, excluding venv/state/archives."""
import json
import pathlib
import subprocess
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTRA = (
    "chaos/chaos-events.jsonl", "reports/drill-1-nodr.jsonl", "reports/drill-2-withdr.jsonl",
    "reports/health-events.jsonl", "reports/failover-events.jsonl", "reports/replication.jsonl",
    "reports/runbook-run.jsonl", "reports/environment.json", "reports/measure-drill-1.json",
    "reports/measure-drill-2.json", "run/validation-linux.log",
)


def package():
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    files += [str(path.relative_to(ROOT)).replace("\\", "/") for path in (ROOT / "dr").glob("*")
              if path.is_file()]
    files += list(EXTRA)
    target = ROOT / "run/lab23-submission.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(set(files)):
            if relative and (ROOT / relative).is_file():
                archive.write(ROOT / relative, relative)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError("submission archive integrity check failed")
        environment = json.loads(archive.read("reports/environment.json"))
        measurement = json.loads(archive.read("reports/measure-drill-2.json"))
    print(json.dumps({"bundle": str(target), "rto_s": measurement["rto_measured_s"],
                      "rpo_s": measurement["rpo_at_restore_s"], "docs_lost": measurement["docs_lost"],
                      "linux_path_verified": environment["graded_linux_path_verified"]}))


if __name__ == "__main__":
    package()
