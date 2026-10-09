# Postmortem — DR Drill Lab 23

Incident diễn ra ngày 2026-10-09 theo Asia/Bangkok (UTC+7).
Timeline dùng UTC để khớp log. Linux bare mode, chaos netblock --mock, backend fs; dùng SIGSTOP/SIGCONT theo GUIDE.md. Distribution=Ubuntu-24.04, WSL version=2, kernel=6.18.40.1-microsoft-standard-WSL2.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T05:32:15.818+00:00 | Outage bắt đầu: pause serving A; B vẫn alive | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T05:32:16.201+00:00 | User bị ảnh hưởng: request đầu tiên fail | `reports/drill-2-withdr.jsonl:23` |
| 2026-10-09T05:32:31.974+00:00 | Health checker báo A UNHEALTHY sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 2026-10-09T05:32:44.578+00:00 | Operator biết incident; auto xác nhận cho drill | `reports/runbook-run.jsonl:2` |
| 2026-10-09T05:32:44.816+00:00 | Restore DB, weights và embedding version | `reports/failover-events.jsonl:2` |
| 2026-10-09T05:32:51.063+00:00 | Region B ready sau warm-up | `reports/failover-events.jsonl:4` |
| 2026-10-09T05:32:51.068+00:00 | DNS/LB cutover sang B | `reports/failover-events.jsonl:5` |
| 2026-10-09T05:32:52.158+00:00 | Resolved: request đầu tiên OK từ B | `reports/drill-2-withdr.jsonl:40` |

Độ trễ thông báo operator: 28.759s từ outage; `reports/runbook-run.jsonl:2`.
auto=true chỉ là chế độ drill; mặc định CLI vẫn hỏi y/N và hủy khi không đồng ý.

## 2. RTO/RPO đo được vs mục tiêu — gap analysis

- RTO mục tiêu 300s; đo được **36.3s**; gap (measured − target)
  **-263.7s**, còn dư 263.7s; `reports/drill-2-withdr.jsonl:40`.
- RPO mục tiêu 300s; đo được **14.00s**, **7 doc** chưa có trong bản
  restore; gap **-286.00s**; `reports/failover-events.jsonl:2`.
- Bước tốn nhiều thời gian nhất: **health-check detection, 16.155s**.
  Detection phải chờ 3 lỗi liên tiếp; runbook kiểm tra thêm 3 lần trước khi
  operator xác nhận. Breakdown ở `reports/rto-evidence.md` phân biệt chúng.
- Golden signals: p95 21.942ms, error rate 0.0%
  trên 10 requests trực tiếp tới B; `reports/runbook-run.jsonl:6`. Đây là sample nhỏ, chưa chứng
  minh độ ổn định dài hạn hoặc end-to-end edge. Log loadgen xác nhận recovery
  qua edge; `reports/drill-2-withdr.jsonl:40`.
- Baseline có 15 request fail và NO_RECOVERY trong cửa sổ
  quan sát; `reports/measure-drill-1.json`.

## 3. Root cause — 5 whys

1. Vì sao request fail? Serving A bị mất khả năng trả lời, trong khi edge vẫn
   route tới A; `reports/drill-2-withdr.jsonl:23`.
2. Vì sao edge chưa chuyển ngay? Pointer chỉ đổi sau khi failover hoàn thành,
   và cache TTL tiếp tục giữ route cũ; `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:40`.
3. Vì sao B chưa tiếp quản được ngay? B khởi đầu warm, thiếu weights và dữ
   liệu; phải restore và scale full trước; `reports/failover-events.jsonl:1`.
4. Vì sao phục hồi vẫn có độ trễ? Detection có threshold chống flapping,
   operator có bước xác nhận độc lập, pool warm-up và TTL đều thuộc critical
   path; `reports/health-events.jsonl:2`, `reports/runbook-run.jsonl:2`, `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5`.
5. Vì sao bản restore thiếu tài liệu mới? Snapshot định kỳ 30s là replication
   bất đồng bộ. Ingest tiếp tục độc lập với serving nên tạo khoảng trống dữ
   liệu trước lúc restore; `reports/replication.jsonl:1`, `reports/failover-events.jsonl:2`.

Nếu outage thật làm mất cả filesystem A, phép đo RPO cần thêm durable ingest
watermark/ack log ở failure domain khác, vì không còn primary DB để đối chiếu.
Snapshot hiện dùng copy file SQLite, chưa đảm bảo backup nhất quán dưới write
load cao. Đây là hạn chế của mô phỏng local, chưa phải bảo đảm DR production.

## 4. Action items — owner + deadline

Các mức giảm bên dưới là đề xuất, chưa phải kết quả của lượt đo mới.

| # | Action | Owner | Deadline (Asia/Bangkok) | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Lưu cấu hình Ubuntu/WSL và tái chạy bare --mock trước mỗi lần thay đổi DR | lab operator | 2026-10-10 | môi trường chuẩn đã được xác minh trong lượt này; giữ khả năng tái lập |
| 2 | Cho runbook tái sử dụng alert threshold còn mới nhưng giữ operator confirm | DR maintainer | 2026-10-11 | bỏ khoảng 10–12s xác nhận lặp; phải drill lại |
| 3 | Thử interval=1s cùng timeout phù hợp; giữ threshold=3, đo false alarm | SRE | 2026-10-11 | budget detection 15s→3s; actual phụ thuộc timeout |
| 4 | Replicate mỗi 10s và dùng SQLite backup API/manifest atomic khi được phép sửa nền | data owner | 2026-10-12 | giảm budget lag 30s→10s, tăng I/O; đo lại docs_lost |
| 5 | Drill failback có kiểm soát, reingest các doc thiếu trước khi về A | incident commander | 2026-10-12 | giảm nguy cơ mất dữ liệu/flapping; chưa có số đo |

## 5. Ba câu hỏi bắt buộc

1. **interval × threshold = 5 × 3 = 15.0s**, chiếm
   **41.32%** RTO; `reports/health-events.jsonl:2`, `reports/drill-2-withdr.jsonl:40`.
   Detection thực đo 16.155s. Công thức là budget của lab; pha poll,
   timeout và chi phí probe quyết định thời điểm phát hiện cụ thể.
2. Giảm interval xuống 1s cho budget danh nghĩa 3s, giảm 12s so với 15s.
   Không khẳng định RTO chắc chắn giảm đúng 12s: timeout=2s và probe tuần tự
   có thể làm cycle vượt 1s. Cần giảm timeout phù hợp hoặc probe song song,
   đánh giá false positives; chi phí là nhiều probe hơn và nhạy với lỗi ngắn.
3. Nếu outage kéo dài 6 giờ và A mất dữ liệu vĩnh viễn, **7 doc** đo ở lần
   restore là dữ liệu chưa có trong snapshot, không được diễn giải thành mất
   toàn bộ 6 giờ dữ liệu. Các doc đó phải reingest từ nguồn bền vững; nếu không
   còn nguồn thì khách hàng có thể nhận kết quả tìm kiếm/AI thiếu thông tin.
   Trong drill này A chỉ bị pause và có thể đối soát sau resume; `reports/failover-events.jsonl:2`.

## 6. Rollback và phạm vi xác minh

Không tự động đảo qua lại A/B. Incident commander quyết định failback sau khi
A ready ổn định, dữ liệu được đối soát từ region đang phục vụ và phiên bản
embedding khớp. Quy trình thao tác nằm trong `reports/runbook.md`.
Lượt đo này chưa kiểm chứng outage mất đĩa, độc lập failure domain, cloud DNS,
GPU thật, MinIO hoặc đường chạy Linux nếu environment ghi win32.

## 7. Reflection questions trong GUIDE.md

1. Có thể giảm TTL hoặc giữ standby full để giảm warm-up mà không hạ threshold
   chống flapping. TTL thấp tăng tần suất đọc route/truy vấn DNS; standby full
   tăng chi phí compute. Cần đo lại RTO sau thay đổi, không dùng mức giảm dự đoán
   làm evidence cho lượt này.
2. Health checker chạy ở tiến trình riêng và chỉ gọi HTTP; không import serving/.
   Nếu đặt monitor cùng tiến trình serving, serving chết thì monitor cũng chết
   và không phát alert. Độc lập tiến trình trong lab chưa tương đương độc lập
   máy/region trong production.
3. Để kiểm chứng RTO 5 phút, mở `reports/measure-drill-2.json` rồi đối chiếu
   `chaos/chaos-events.jsonl:3` với `reports/drill-2-withdr.jsonl:40`.
   Chạy lại tools/measure_rto.py để kiểm tra valid, warnings, PASS và region phục
   hồi; bảng báo cáo chỉ tổng hợp các timestamp thật đó.
