# Runbook — Region chính down

**Phạm vi:** A chính (8001), B dự phòng (8002), edge (8080), snapshot backend fs.
Chạy ở thư mục gốc trong virtualenv đã cài requirements.txt. Lệnh dùng `python`;
trên Windows thay bằng `.\.venv\Scripts\python.exe -X utf8`. Quyền duyệt chuyển
traffic: **incident commander**. Người thao tác: **on-call / DR operator**.

## Chuẩn bị trước incident

Ở các terminal riêng, trước khi gây outage:

```bash
python state/replicate.py --every 30 --duration 300 --backend fs
python dr/health_checker.py --interval 5 --threshold 3 --duration 300
python loadgen/traffic.py --duration 300 --rps 2 --out reports/drill-2-withdr.jsonl
```

`python state/snapshot.py lag --backend fs` phải trả rpo_seconds khác null.
B phải còn alive; B chưa ready khi warm là bình thường. Nếu B cũng down, dừng
và khôi phục B trước. Log traffic phải bắt đầu trước chaos để có cửa sổ đo RTO.

## Checklist 7 bước

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | A.ready=false liên tiếp; B.alive=true. Đối chiếu alert UNHEALTHY với consecutive_fails>=3; không tin một probe. | on-call |
| 2 | Mở incident và chạy quy trình có xác nhận | `python dr/runbook.py --primary a --target b --backend fs` | Log step=2 ghi t_outage và operator_notified_ts; prompt y/N xuất hiện. Commander duyệt rồi nhập y. Chỉ chạy một lần. | commander + DR operator |
| 3 | Restore state và scale pool (lệnh bước 2 thực hiện) | `python -c "import json,pathlib; print('\n'.join(x for x in pathlib.Path('reports/failover-events.jsonl').read_text(encoding='utf-8').splitlines() if json.loads(x).get('step') in ['2_restore_snapshot','3_scale_pool']))"` | count>0, weights=true, embed_model_version, rpo_seconds và docs_lost; pool full. Có failover_aborted thì dừng, không sửa pointer bằng tay. | DR operator |
| 4 | Xác minh readiness | `python -c "import httpx; r=httpx.get('http://127.0.0.1:8002/readyz',timeout=2); print(r.status_code,r.text)"` | HTTP 200, ready=true; 4_wait_ready xuất hiện trước 5_dns_cutover. | SRE |
| 5 | Xác minh DNS/LB cutover | `python -c "import httpx; print(httpx.get('http://127.0.0.1:8080/edge/state',timeout=3).json())"` | active_region=b sau TTL 5s; loadgen ghi served_by=b. Không gọi thêm failover. | on-call |
| 6 | Xác minh golden signals | `python -c "import json,pathlib; print([json.loads(x) for x in pathlib.Path('reports/runbook-run.jsonl').read_text(encoding='utf-8').splitlines() if json.loads(x).get('step')==6][-1])"` | 10 requests thật tới B, error_rate=0, p95_latency_ms<500; đối chiếu loadgen qua edge. Không đạt thì commander quyết định rollback hoặc giữ B xử lý tiếp. | SRE |
| 7 | Đo RTO/RPO và postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | valid=true, warnings=[], PASS; RPO và docs_lost khác null. Sau khi log đủ, chạy `python dr/write_reports.py`. | lab operator / commander |

Runbook.py gọi failover đúng một lần, làm đủ restore, scale, wait-ready và
cutover. Bước 3–6 trong bảng kiểm chứng kết quả, không chạy thêm failover.
--auto chỉ dùng drill/CI. Thiếu snapshot hoặc ready timeout: CLI trả exit code
khác 0 và không chuyển pointer; xem reports/failover-events.jsonl, run/runbook.log.

## Rollback / failback có kiểm soát

**Điều kiện xem xét:** B lỗi phục vụ kéo dài hoặc golden signals không đạt.
Chỉ trả traffic về A khi A phục hồi, ready ổn định ít nhất 3 probe cách nhau 5s,
dữ liệu đã đối soát và embedding version khớp. A chưa ready thì giữ route hiện
tại, xử lý incident; không tự động đảo A/B.

**Quyền quyết định:** incident commander. **Thao tác:** DR operator; SRE xác minh.
Trên Linux, resume A bị SIGSTOP:
`python chaos/kill_region.py restore --region a --backend bare`.
A bị SIGKILL phải khởi động lại dịch vụ, tránh thêm instance chiếm cổng.
Runner Windows tự resume/dừng process trong cleanup; rerun bằng
`python dr/drill.py`, không dùng restore --backend bare trên Windows.

Sau khi commander duyệt failback, đối soát doc thiếu từ A với B trước. Tạo
snapshot **từ B đang phục vụ** rồi chuyển về A có xác nhận:

```bash
python state/snapshot.py put --region b --backend fs
python -c "from dr.runbook import confirm; from dr.failover import failover; print(failover('a','fs',60) if confirm(False,'Commander approved failback to A?') else {'ok':False,'reason':'cancelled'})"
```

Chờ TTL, kiểm tra edge và request thực tế phục vụ từ A. Dừng/chuyển ingest theo
region đang phục vụ khi đối soát. Lab chưa có reconciliation tự động nên không
failback khi dữ liệu chưa thống nhất.

## Tạo evidence trọn bộ

`python dr/drill.py` → `python dr/write_reports.py` →
`python -m pytest tests/ dr/test_workflow.py -v`.
Runner lưu evidence/state cũ vào reports/archive/ và dừng process do nó tạo.
Lượt nộp hiện đã chạy trên Ubuntu 24.04 / WSL 2 bằng Linux bare --mock theo
GUIDE.md. `reports/environment.json` ghi platform=linux, backend=bare, mock=true
và graded_linux_path_verified=true; kết quả kiểm thử ở `run/validation-linux.log`.
Các lượt thử trước được lưu riêng trong reports/archive/. Xem dr/README.md để
chạy lại; Windows dùng -X utf8 khi chạy biến thể Windows.
