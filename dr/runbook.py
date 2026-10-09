"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Record one completed checklist step."""
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    """Require an explicit operator response outside the drill/CI mode."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Confirm the outage, perform one failover, then verify the result."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions (a/b)")
    started = time.monotonic()
    # The standby may not be ready yet; it must at least be alive so state can
    # be restored. Primary failures must be consecutive before proceeding.
    for attempt in range(3):
        cycle = time.monotonic()
        ready, reason = hc.probe(primary, 2.0)
        try:
            alive = httpx.get(f"{URL[target]}/healthz", timeout=2.0).status_code == 200
        except httpx.RequestError:
            alive = False
        if ready or not alive:
            step(1, "xac_nhan_outage", ok=False, primary_ready=ready,
                 target_alive=alive, reason="outage not confirmed or standby unavailable")
            return {"ok": False, "reason": "outage not confirmed or standby unavailable"}
        if attempt < 2:
            time.sleep(max(0.0, cycle + 5.0 - time.monotonic()))
    step(1, "xac_nhan_outage", ok=True, primary=primary, target=target,
         consecutive_fails=3, interval_s=5.0, reason=reason)

    chaos = pathlib.Path("chaos/chaos-events.jsonl")
    events = [json.loads(line) for line in chaos.read_text(encoding="utf-8").splitlines()
              if line.strip()] if chaos.exists() else []
    outage = next((event for event in reversed(events)
                   if event.get("action") == "kill" and event.get("region") == primary), {})
    step(2, "thong_bao_incident", primary=primary, target=target,
         t_outage=outage.get("ts"), operator_notified_ts=time.time(), auto=auto)
    if not confirm(auto, f"Fail over region-{primary} to region-{target}?"):
        step(3, "scale_gpu_pool", ok=False, reason="operator declined")
        return {"ok": False, "reason": "operator declined"}

    result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", **result)
    if not result["ok"]:
        return result
    restored = result["state"]
    step(4, "verify_state_replica", count=restored["count"], weights=restored["weights"],
         embed_model_version=result["embed_model_version"],
         rpo_seconds=result["rpo_seconds"], docs_lost=result["docs_lost"])
    step(5, "dns_cutover", ok=result["ok"], target=target, cutover_ts=result["cutover_ts"])

    latencies, errors = [], 0
    with httpx.Client(timeout=3.0) as client:
        for index in range(10):
            request_started = time.monotonic()
            try:
                response = client.get(f"{URL[target]}/v1/infer", params={"q": f"verify {index}"})
                body = response.json()
                ok = response.status_code == 200 and body.get("region") == target
                status = response.status_code
            except (httpx.RequestError, ValueError):
                ok, status = False, None
            latency = round((time.monotonic() - request_started) * 1000, 3)
            latencies.append(latency)
            errors += not ok
            fo.emit(event="golden_signal_request", target=target,
                    seq=index, ok=ok, status=status, latency_ms=latency)
    p95 = sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1]
    step(6, "verify_golden_signals", requests=10, p95_latency_ms=p95,
         error_rate=errors / 10, ok=errors == 0)
    step(7, "post_incident", ok=errors == 0,
         elapsed_s=round(time.monotonic() - started, 3),
         measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    return {**result, "ok": errors == 0, "p95_latency_ms": p95, "error_rate": errors / 10}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
