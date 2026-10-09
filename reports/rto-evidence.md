# RTO/RPO Evidence — Lab 23

## Môi trường và cách tái lập

Linux bare mode, chaos netblock --mock, backend fs; dùng SIGSTOP/SIGCONT theo GUIDE.md. Distribution=Ubuntu-24.04, WSL version=2, kernel=6.18.40.1-microsoft-standard-WSL2.
Thông số được lưu ở `reports/environment.json`. Các file nguồn serving/, state/,
edge/, chaos/, tools/ và tests/ giữ nguyên. Chạy `python dr/drill.py`, sau đó
`python dr/write_reports.py` để tạo lại log và báo cáo. Lượt cũ được lưu vào
reports/archive/ trước khi chạy lại. Chưa có kết quả kiểm thử MinIO/Docker.

Timestamps dưới đây là UTC. Load generator ghi ts lúc **bắt đầu** request; RTO
theo tools/measure_rto.py là ts request thành công đầu tiên sau lỗi trừ ts chaos,
không phải thời điểm nhận xong response. Verdict PASS phản ánh phép đo này.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 2026-10-09T05:31:27.083+00:00 | chaos pause A; B còn alive | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.200s | request ok:false đầu sau outage | `reports/drill-1-nodr.jsonl:19` |
| Request thành công sau lỗi | Không có trong cửa sổ drill | kiểm tra toàn bộ log sau first failure | `reports/measure-drill-1.json` |
| Requests fail | 15/33 | đếm request sau kill có ok:false | `reports/measure-drill-1.json` |
| RTO | NO_RECOVERY | không có recovery trong thời gian quan sát | `reports/measure-drill-1.json` |

Không suy diễn outage vô hạn từ một cửa sổ 40s. Region A chỉ được resume sau khi
load generator kết thúc, nên resume không làm thay đổi kết quả baseline.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0.000 | action:kill | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.383 | request bắt đầu với ok:false | `reports/drill-2-withdr.jsonl:23` |
| Health check phát hiện | 16.155 | state_change / a / UNHEALTHY | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 28.997 | 2_restore_snapshot | `reports/failover-events.jsonl:2` |
| Region phụ ready | 35.244 | 4_wait_ready | `reports/failover-events.jsonl:4` |
| DNS cutover | 35.250 | 5_dns_cutover | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 36.300 | request OK đầu sau lỗi, served_by=b | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---|---|---|---|
| RTO — Inference API | 36.3s | 300s | PASS | `reports/drill-2-withdr.jsonl:40`, `reports/measure-drill-2.json` |
| RPO — Vector DB | 14.00s / 7 doc | 300s | PASS | `reports/failover-events.jsonl:2` |
| Tài liệu sau restore | 216 doc; weights=true | count > 0 và có weights | PASS | `reports/failover-events.jsonl:2` |
| Phiên bản embedding | embed-model=vi-e5-base@v3 | giữ cùng phiên bản snapshot | PASS | `reports/failover-events.jsonl:2` |
| Golden signals | 10 requests; p95=21.942ms; error rate=0.0% | p95 < 500ms; error rate=0% | PASS | `reports/runbook-run.jsonl:6` |

Drill 2 có 17/145 requests fail; recovered_by_region=b,
valid=true, warnings=[] theo `reports/measure-drill-2.json`.

RPO = primary_latest_doc_ts − restored_latest_doc_ts tại bước restore; docs_lost
đếm doc ở primary có ingested_at lớn hơn timestamp mới nhất của bản restore.
Đây là phép đo theo thời gian ingest mà state/snapshot.py cung cấp, không phải
đối chiếu toàn bộ ID. Ingest là tiến trình riêng vẫn chạy khi serving A bị pause:
RPO phản ánh thời điểm restore, không chỉ thời điểm outage. Dữ liệu còn trên A
trong drill nên có thể đối soát sau phục hồi; mất vĩnh viễn A thì các doc chưa
replicate không thể phục hồi từ snapshot này.

## 3. Breakdown RTO

| Thành phần | Giây | Cách tính / Evidence | Có thể giảm bằng |
|---|---:|---|---|
| Health-check detect; budget floor 15.0s | 16.155 | t_detect − t_outage; interval=5s × threshold=3; `reports/health-events.jsonl:2` | giảm interval có kiểm chứng timeout và false alarm |
| Snapshot restore + kiểm tra state | 0.145 | ts 2_restore − ts 1_verify; `reports/failover-events.jsonl:1`, `reports/failover-events.jsonl:2` | replica cập nhật sẵn, giảm kích thước snapshot |
| GPU pool warm-up + poll | 6.243 | ts 4_wait_ready − ts 3_scale_pool; waited_s=6.231; `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:4` | giữ standby full, trả thêm chi phí compute |
| DNS/LB TTL cache + cadence request | 1.090 | t_recovered − t_cutover; `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:40` | giảm TTL hoặc invalidation được kiểm soát |
| Operator xác nhận + điều phối + làm tròn RTO | 12.667 | phần còn lại: detect→verify, restore→scale, ready→cutover; `reports/runbook-run.jsonl:2`, `reports/failover-events.jsonl:1` | tái sử dụng alert có threshold, giữ xác nhận của operator |
| **Tổng** | **36.300** | bằng RTO của tools/measure_rto.py | |

Không gán độ trễ xác nhận operator vào thời gian restore. Budget interval ×
threshold được ghi riêng; detection thực tế còn phụ thuộc pha poll, timeout và
thời gian probe. Tổng có hiệu chỉnh độ làm tròn 0.1s của công cụ đo.
