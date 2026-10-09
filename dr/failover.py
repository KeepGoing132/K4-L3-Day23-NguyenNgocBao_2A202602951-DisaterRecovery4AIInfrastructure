"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped event and expose it to the operator."""
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and prepare the target; cut over only after readiness succeeds."""
    if target not in URL or backend not in {"fs", "minio"} or wait <= 0:
        raise ValueError("invalid target/backend or nonpositive wait")
    started = time.monotonic()
    stage = "1_verify_target"
    try:
        initial = state_of(target)
        emit(step=stage, target=target, state=initial)

        stage = "2_restore_snapshot"
        restore_started = time.monotonic()
        manifest = snapshot.get(target, backend)
        primary = manifest.get("source_region", "b" if target == "a" else "a")
        if primary == target:
            raise ValueError("snapshot source must differ from the failover target")
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                           pathlib.Path(f"state/region-{target}/vectors.sqlite"))
        restored = state_of(target)
        if not restored.get("weights") or restored.get("count", 0) < 1:
            raise ValueError("restored target has no model weights or documents")
        emit(step=stage, target=target, backend=backend,
             embed_model_version=manifest["embed_model_version"],
             snapshot_at=manifest["snapshot_at"],
             restore_duration_s=round(time.monotonic() - restore_started, 3),
             count=restored["count"], weights=restored["weights"], **rpo)

        stage = "3_scale_pool"
        pool = pathlib.Path(f"state/region-{target}/pool_state")
        pool.write_text("full", encoding="utf-8")
        emit(step=stage, target=target, pool_state="full")

        stage = "4_wait_ready"
        ready_started = time.monotonic()
        deadline = ready_started + wait
        last_reason = "not probed"
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, remaining))
                if response.status_code == 200:
                    readiness = response.json()
                    emit(step=stage, target=target, ready=True,
                         waited_s=round(time.monotonic() - ready_started, 3))
                    break
                last_reason = f"HTTP {response.status_code}"
            except httpx.RequestError as exc:
                last_reason = type(exc).__name__
            time.sleep(max(0.0, min(0.25, deadline - time.monotonic())))
        else:
            raise TimeoutError(f"region-{target} not ready after {wait}s: {last_reason}")

        stage = "5_dns_cutover"
        active = pathlib.Path("edge/active_region")
        # Replace the complete pointer atomically, so the proxy cannot read an
        # empty file while a concurrent write is in progress.
        temporary = active.with_suffix(".tmp")
        temporary.write_text(target, encoding="utf-8")
        temporary.replace(active)
        cutover = emit(step=stage, target=target, active_region=target, ok=True)
        return {"ok": True, "target": target, "cutover_ts": cutover["ts"],
                "state": {"region": target, "pool_state": "full",
                          "count": readiness["vectors"]["count"],
                          "weights": restored["weights"]},
                "embed_model_version": manifest["embed_model_version"],
                "elapsed_s": round(time.monotonic() - started, 3), **rpo}
    except (Exception, SystemExit) as exc:
        emit(event="failover_aborted", failed_step=stage, target=target,
             ok=False, reason=str(exc))
        return {"ok": False, "target": target, "failed_step": stage, "reason": str(exc)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
