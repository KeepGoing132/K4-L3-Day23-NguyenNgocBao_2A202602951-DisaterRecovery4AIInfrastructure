"""Generate evidence and postmortem from actual drill logs; refuse invalid drills.

Run from the project root: python dr/write_reports.py
No sample RTO/RPO values or fabricated timestamps are used.
"""
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.measure_rto import measure  # noqa: E402


def records(path):
    return [(index, json.loads(line)) for index, line in enumerate(
        (ROOT / path).read_text(encoding="utf-8").splitlines(), 1) if line.strip()]


def reference(path, index):
    return f"`{path}:{index}`"


def iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(timespec="milliseconds")


def generate():
    import os

    os.chdir(ROOT)
    values = []
    for number, path in ((1, "reports/drill-1-nodr.jsonl"), (2, "reports/drill-2-withdr.jsonl")):
        value = measure(path, "chaos/chaos-events.jsonl", "reports/health-events.jsonl",
                        "reports/failover-events.jsonl", 300)
        if not value["valid"]:
            raise ValueError(f"drill {number} invalid: {value['invalid_reasons']}")
        (ROOT / "reports" / f"measure-drill-{number}.json").write_text(
            json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        values.append(value)
    m1, m2 = values
    if m1["rto_verdict"] != "NO_RECOVERY" or m2["warnings"] or m2["rto_verdict"] != "PASS":
        raise ValueError("baseline or recovery verdict does not satisfy the report requirements")
    if m2["rpo_at_restore_s"] is None or m2["docs_lost"] is None:
        raise ValueError("missing measured RPO")

    baseline = records("reports/drill-1-nodr.jsonl")
    traffic = records("reports/drill-2-withdr.jsonl")
    kills = records("chaos/chaos-events.jsonl")
    kill1 = next((i, row) for i, row in kills if row.get("action") == "kill"
                 and baseline[0][1]["ts"] <= row["ts"] <= baseline[-1][1]["ts"])
    kill2 = next((i, row) for i, row in kills if row.get("action") == "kill"
                 and traffic[0][1]["ts"] <= row["ts"] <= traffic[-1][1]["ts"])
    t0 = kill2[1]["ts"]
    first1 = next((i, row) for i, row in baseline if row["ts"] >= kill1[1]["ts"] and not row["ok"])
    first2 = next((i, row) for i, row in traffic if row["ts"] >= t0 and not row["ok"])
    recovered = next((i, row) for i, row in traffic if row["ts"] > first2[1]["ts"] and row["ok"])
    health = next((i, row) for i, row in records("reports/health-events.jsonl")
                  if row["region"] == "a" and row["to"] == "UNHEALTHY" and row["ts"] >= t0)
    phases = {row["step"]: (i, row) for i, row in records("reports/failover-events.jsonl")
              if row.get("step") and t0 <= row["ts"] <= traffic[-1][1]["ts"]}
    one, two, three, four, five = [phases[f"{n}_{name}"] for n, name in (
        (1, "verify_target"), (2, "restore_snapshot"), (3, "scale_pool"),
        (4, "wait_ready"), (5, "dns_cutover"))]
    runbook = {row["step"]: (i, row) for i, row in records("reports/runbook-run.jsonl")
               if t0 <= row["ts"] <= traffic[-1][1]["ts"]}
    environment = json.loads((ROOT / "reports/environment.json").read_text(encoding="utf-8"))
    rto, rpo, lost = m2["rto_measured_s"], m2["rpo_at_restore_s"], m2["docs_lost"]
    floor = m2["health_check_config"]["detect_floor_s"]
    detection = round(health[1]["ts"] - t0, 3)
    restore = round(two[1]["ts"] - one[1]["ts"], 3)
    warmup = round(four[1]["ts"] - three[1]["ts"], 3)
    ttl = round(recovered[1]["ts"] - five[1]["ts"], 3)
    operator = round(rto - detection - restore - warmup - ttl, 3)
    dr1fail = round(first1[1]["ts"] - kill1[1]["ts"], 3)
    result_count, p95 = two[1]["count"], runbook[6][1]["p95_latency_ms"]
    detection_ref = reference("reports/health-events.jsonl", health[0])
    restore_ref = reference("reports/failover-events.jsonl", two[0])
    ready_ref = reference("reports/failover-events.jsonl", four[0])
    cutover_ref = reference("reports/failover-events.jsonl", five[0])
    recovered_ref = reference("reports/drill-2-withdr.jsonl", recovered[0])
    incident_ref = reference("reports/runbook-run.jsonl", runbook[2][0])
    golden_ref = reference("reports/runbook-run.jsonl", runbook[6][0])
    platform_note = (
        "Windows 11, Python 3.11.9, backend fs. Chaos tạm dừng/khôi phục cây tiến trình "
        "Region A bằng NtSuspendProcess/NtResumeProcess, backend=windows, mock=false. "
        "Đây là lượt chạy thật trên Windows; chưa xác minh đường chạy Linux bare --mock "
        "mà GUIDE.md yêu cầu để chấm điểm. Tại thời điểm lượt đo này, WSL/Docker chưa được cài trên máy."
        if environment["platform"] == "win32" else
        "Linux bare mode, chaos netblock --mock, backend fs; dùng SIGSTOP/SIGCONT theo GUIDE.md. "
        f"Distribution={environment.get('distribution')}, WSL version={environment.get('wsl_version')}, "
        f"kernel={environment.get('kernel_release')}."
    )
    milestones = [
        ("t_outage (mốc 0)", 0, "action:kill", reference("chaos/chaos-events.jsonl", kill2[0])),
        ("User thấy lỗi đầu tiên", first2[1]["ts"] - t0, "request bắt đầu với ok:false",
         reference("reports/drill-2-withdr.jsonl", first2[0])),
        ("Health check phát hiện", detection, "state_change / a / UNHEALTHY", detection_ref),
        ("Snapshot restore xong", two[1]["ts"] - t0, "2_restore_snapshot", restore_ref),
        ("Region phụ ready", four[1]["ts"] - t0, "4_wait_ready", ready_ref),
        ("DNS cutover", five[1]["ts"] - t0, "5_dns_cutover", cutover_ref),
        ("**RTO đo được**", rto, "request OK đầu sau lỗi, served_by=b", recovered_ref),
    ]
    timeline_table = "\n".join(f"| {name} | {seconds:.3f} | {how} | {ref} |"
                               for name, seconds, how, ref in milestones)
    evidence = f"""# RTO/RPO Evidence — Lab 23

## Môi trường và cách tái lập

{platform_note}
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
| t_outage | {iso(kill1[1]['ts'])} | chaos pause A; B còn alive | {reference('chaos/chaos-events.jsonl', kill1[0])} |
| Request fail đầu tiên | +{dr1fail:.3f}s | request ok:false đầu sau outage | {reference('reports/drill-1-nodr.jsonl', first1[0])} |
| Request thành công sau lỗi | Không có trong cửa sổ drill | kiểm tra toàn bộ log sau first failure | `reports/measure-drill-1.json` |
| Requests fail | {m1['requests_failed']}/{len(baseline)} | đếm request sau kill có ok:false | `reports/measure-drill-1.json` |
| RTO | NO_RECOVERY | không có recovery trong thời gian quan sát | `reports/measure-drill-1.json` |

Không suy diễn outage vô hạn từ một cửa sổ 40s. Region A chỉ được resume sau khi
load generator kết thúc, nên resume không làm thay đổi kết quả baseline.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
{timeline_table}

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---|---|---|---|
| RTO — Inference API | {rto:.1f}s | 300s | PASS | {recovered_ref}, `reports/measure-drill-2.json` |
| RPO — Vector DB | {rpo:.2f}s / {lost} doc | 300s | {'PASS' if rpo <= 300 else 'FAIL'} | {restore_ref} |
| Tài liệu sau restore | {result_count} doc; weights=true | count > 0 và có weights | PASS | {restore_ref} |
| Phiên bản embedding | {two[1]['embed_model_version']} | giữ cùng phiên bản snapshot | PASS | {restore_ref} |
| Golden signals | 10 requests; p95={p95:.3f}ms; error rate={runbook[6][1]['error_rate']:.1%} | p95 < 500ms; error rate=0% | {'PASS' if p95 < 500 and runbook[6][1]['error_rate'] == 0 else 'FAIL'} | {golden_ref} |

Drill 2 có {m2['requests_failed']}/{len(traffic)} requests fail; recovered_by_region=b,
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
| Health-check detect; budget floor {floor:.1f}s | {detection:.3f} | t_detect − t_outage; interval=5s × threshold=3; {detection_ref} | giảm interval có kiểm chứng timeout và false alarm |
| Snapshot restore + kiểm tra state | {restore:.3f} | ts 2_restore − ts 1_verify; {reference('reports/failover-events.jsonl', one[0])}, {restore_ref} | replica cập nhật sẵn, giảm kích thước snapshot |
| GPU pool warm-up + poll | {warmup:.3f} | ts 4_wait_ready − ts 3_scale_pool; waited_s={four[1]['waited_s']}; {reference('reports/failover-events.jsonl', three[0])}, {ready_ref} | giữ standby full, trả thêm chi phí compute |
| DNS/LB TTL cache + cadence request | {ttl:.3f} | t_recovered − t_cutover; {cutover_ref}, {recovered_ref} | giảm TTL hoặc invalidation được kiểm soát |
| Operator xác nhận + điều phối + làm tròn RTO | {operator:.3f} | phần còn lại: detect→verify, restore→scale, ready→cutover; {incident_ref}, {reference('reports/failover-events.jsonl', one[0])} | tái sử dụng alert có threshold, giữ xác nhận của operator |
| **Tổng** | **{rto:.3f}** | bằng RTO của tools/measure_rto.py | |

Không gán độ trễ xác nhận operator vào thời gian restore. Budget interval ×
threshold được ghi riêng; detection thực tế còn phụ thuộc pha poll, timeout và
thời gian probe. Tổng có hiệu chỉnh độ làm tròn 0.1s của công cụ đo.
"""
    (ROOT / "reports/rto-evidence.md").write_text(evidence, encoding="utf-8")

    dated = dt.datetime.fromtimestamp(t0, dt.timezone(dt.timedelta(hours=7)))
    deadline1 = (dated.date() + dt.timedelta(days=1)).isoformat()
    deadline2 = (dated.date() + dt.timedelta(days=2)).isoformat()
    deadline3 = (dated.date() + dt.timedelta(days=3)).isoformat()
    timeline = [
        (kill2[1]["ts"], "Outage bắt đầu: pause serving A; B vẫn alive",
         reference("chaos/chaos-events.jsonl", kill2[0])),
        (first2[1]["ts"], "User bị ảnh hưởng: request đầu tiên fail",
         reference("reports/drill-2-withdr.jsonl", first2[0])),
        (health[1]["ts"], "Health checker báo A UNHEALTHY sau 3 lỗi liên tiếp", detection_ref),
        (runbook[2][1]["ts"], "Operator biết incident; auto xác nhận cho drill", incident_ref),
        (two[1]["ts"], "Restore DB, weights và embedding version", restore_ref),
        (four[1]["ts"], "Region B ready sau warm-up", ready_ref),
        (five[1]["ts"], "DNS/LB cutover sang B", cutover_ref),
        (recovered[1]["ts"], "Resolved: request đầu tiên OK từ B", recovered_ref),
    ]
    timeline_rows = "\n".join(f"| {iso(timestamp)} | {event} | {ref} |"
                               for timestamp, event, ref in timeline)
    durations = {"health-check detection": detection, "snapshot restore": restore,
                 "GPU warm-up": warmup, "DNS/TTL": ttl, "operator confirmation": operator}
    largest = max(durations, key=durations.get)
    environment_action = (
        "Lưu cấu hình Ubuntu/WSL và tái chạy bare --mock trước mỗi lần thay đổi DR"
        if environment.get("graded_linux_path_verified") else
        "Cài WSL 2/Ubuntu, chạy lại bare --mock và tạo lại báo cáo"
    )
    environment_impact = (
        "môi trường chuẩn đã được xác minh trong lượt này; giữ khả năng tái lập"
        if environment.get("graded_linux_path_verified") else
        "xác minh môi trường chuẩn; chưa cam kết giảm RTO"
    )
    postmortem = f"""# Postmortem — DR Drill Lab 23

Incident diễn ra ngày {dated.date().isoformat()} theo Asia/Bangkok (UTC+7).
Timeline dùng UTC để khớp log. {platform_note}

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
{timeline_rows}

Độ trễ thông báo operator: {runbook[2][1]['ts'] - t0:.3f}s từ outage; {incident_ref}.
auto=true chỉ là chế độ drill; mặc định CLI vẫn hỏi y/N và hủy khi không đồng ý.

## 2. RTO/RPO đo được vs mục tiêu — gap analysis

- RTO mục tiêu 300s; đo được **{rto:.1f}s**; gap (measured − target)
  **{rto - 300:.1f}s**, còn dư {300 - rto:.1f}s; {recovered_ref}.
- RPO mục tiêu 300s; đo được **{rpo:.2f}s**, **{lost} doc** chưa có trong bản
  restore; gap **{rpo - 300:.2f}s**; {restore_ref}.
- Bước tốn nhiều thời gian nhất: **{largest}, {durations[largest]:.3f}s**.
  Detection phải chờ 3 lỗi liên tiếp; runbook kiểm tra thêm 3 lần trước khi
  operator xác nhận. Breakdown ở `reports/rto-evidence.md` phân biệt chúng.
- Golden signals: p95 {p95:.3f}ms, error rate {runbook[6][1]['error_rate']:.1%}
  trên 10 requests trực tiếp tới B; {golden_ref}. Đây là sample nhỏ, chưa chứng
  minh độ ổn định dài hạn hoặc end-to-end edge. Log loadgen xác nhận recovery
  qua edge; {recovered_ref}.
- Baseline có {m1['requests_failed']} request fail và NO_RECOVERY trong cửa sổ
  quan sát; `reports/measure-drill-1.json`.

## 3. Root cause — 5 whys

1. Vì sao request fail? Serving A bị mất khả năng trả lời, trong khi edge vẫn
   route tới A; {reference('reports/drill-2-withdr.jsonl', first2[0])}.
2. Vì sao edge chưa chuyển ngay? Pointer chỉ đổi sau khi failover hoàn thành,
   và cache TTL tiếp tục giữ route cũ; {cutover_ref}, {recovered_ref}.
3. Vì sao B chưa tiếp quản được ngay? B khởi đầu warm, thiếu weights và dữ
   liệu; phải restore và scale full trước; {reference('reports/failover-events.jsonl', one[0])}.
4. Vì sao phục hồi vẫn có độ trễ? Detection có threshold chống flapping,
   operator có bước xác nhận độc lập, pool warm-up và TTL đều thuộc critical
   path; {detection_ref}, {incident_ref}, {ready_ref}, {cutover_ref}.
5. Vì sao bản restore thiếu tài liệu mới? Snapshot định kỳ 30s là replication
   bất đồng bộ. Ingest tiếp tục độc lập với serving nên tạo khoảng trống dữ
   liệu trước lúc restore; `reports/replication.jsonl:1`, {restore_ref}.

Nếu outage thật làm mất cả filesystem A, phép đo RPO cần thêm durable ingest
watermark/ack log ở failure domain khác, vì không còn primary DB để đối chiếu.
Snapshot hiện dùng copy file SQLite, chưa đảm bảo backup nhất quán dưới write
load cao. Đây là hạn chế của mô phỏng local, chưa phải bảo đảm DR production.

## 4. Action items — owner + deadline

Các mức giảm bên dưới là đề xuất, chưa phải kết quả của lượt đo mới.

| # | Action | Owner | Deadline (Asia/Bangkok) | Tác động dự kiến |
|---|---|---|---|---|
| 1 | {environment_action} | lab operator | {deadline1} | {environment_impact} |
| 2 | Cho runbook tái sử dụng alert threshold còn mới nhưng giữ operator confirm | DR maintainer | {deadline2} | bỏ khoảng 10–12s xác nhận lặp; phải drill lại |
| 3 | Thử interval=1s cùng timeout phù hợp; giữ threshold=3, đo false alarm | SRE | {deadline2} | budget detection 15s→3s; actual phụ thuộc timeout |
| 4 | Replicate mỗi 10s và dùng SQLite backup API/manifest atomic khi được phép sửa nền | data owner | {deadline3} | giảm budget lag 30s→10s, tăng I/O; đo lại docs_lost |
| 5 | Drill failback có kiểm soát, reingest các doc thiếu trước khi về A | incident commander | {deadline3} | giảm nguy cơ mất dữ liệu/flapping; chưa có số đo |

## 5. Ba câu hỏi bắt buộc

1. **interval × threshold = 5 × 3 = {floor:.1f}s**, chiếm
   **{floor / rto * 100:.2f}%** RTO; {detection_ref}, {recovered_ref}.
   Detection thực đo {detection:.3f}s. Công thức là budget của lab; pha poll,
   timeout và chi phí probe quyết định thời điểm phát hiện cụ thể.
2. Giảm interval xuống 1s cho budget danh nghĩa 3s, giảm 12s so với 15s.
   Không khẳng định RTO chắc chắn giảm đúng 12s: timeout=2s và probe tuần tự
   có thể làm cycle vượt 1s. Cần giảm timeout phù hợp hoặc probe song song,
   đánh giá false positives; chi phí là nhiều probe hơn và nhạy với lỗi ngắn.
3. Nếu outage kéo dài 6 giờ và A mất dữ liệu vĩnh viễn, **{lost} doc** đo ở lần
   restore là dữ liệu chưa có trong snapshot, không được diễn giải thành mất
   toàn bộ 6 giờ dữ liệu. Các doc đó phải reingest từ nguồn bền vững; nếu không
   còn nguồn thì khách hàng có thể nhận kết quả tìm kiếm/AI thiếu thông tin.
   Trong drill này A chỉ bị pause và có thể đối soát sau resume; {restore_ref}.

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
   {reference('chaos/chaos-events.jsonl', kill2[0])} với {recovered_ref}.
   Chạy lại tools/measure_rto.py để kiểm tra valid, warnings, PASS và region phục
   hồi; bảng báo cáo chỉ tổng hợp các timestamp thật đó.
"""
    (ROOT / "reports/postmortem.md").write_text(postmortem, encoding="utf-8")
    print(f"Reports written from logs: RTO={rto}s, RPO={rpo}s, docs_lost={lost}")


if __name__ == "__main__":
    generate()
