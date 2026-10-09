# Chạy và kiểm chứng phần DR

Ba module health_checker.py, failover.py và runbook.py triển khai phần bài tập.
Không sửa mã nền trong serving/, edge/, state/, chaos/, tools/ hoặc tests/.

## Chạy ngay trên Windows hiện tại

Tại thư mục gốc dự án, mở PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 dr/drill.py
.\.venv\Scripts\python.exe -X utf8 dr/write_reports.py
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/ dr/test_workflow.py -v
```

drill.py chạy hai drill thật (40s baseline; ingest/replicate 150s cho recovery),
khởi tạo A có dữ liệu và B rỗng, chạy serving + edge, gây outage trong cửa sổ
traffic, chờ alert trước khi gọi runbook, đo bằng tools/measure_rto.py có sẵn.
Ở drill có DR, runner chờ health checker hoàn tất vòng poll ghi standby warm
chưa ready rồi mới gây outage A, để quan sát đủ ba vòng kiểm tra kế tiếp.
Giữ máy hoạt động suốt lượt đo; Sleep/hibernate có thể làm request và log bị
gián đoạn dù phục hồi đã hoàn tất.
Nó tự dừng các tiến trình do nó tạo sau khi hoàn tất. Không cần Docker để chạy
biến thể Windows này. Không chạy đồng thời stack khác trên 8001/8002/8080.

Windows dùng NtSuspendProcess/NtResumeProcess trên cây tiến trình Python,
bao gồm worker phía sau launcher virtualenv. Log ghi backend=windows,
mock=false để phân biệt với Linux. Đây không phải đường chấm điểm bare --mock
được yêu cầu trong GUIDE.md; phải xác minh lại trên Linux trước khi tuyên bố
hoàn tất yêu cầu môi trường chấm điểm. Không chuyển nhãn log thành bare/mock.

Rerun lưu state và evidence cũ vào reports/archive/<UTC timestamp>/ rồi tạo
state mới. Các báo cáo Markdown không đổi cho đến khi chạy write_reports.py.

## Chạy đường chuẩn trên Ubuntu/WSL hoặc Linux

Sau khi môi trường Linux được cài, tại thư mục gốc:

```bash
python3 -m venv run/venv-linux
source run/venv-linux/bin/activate
pip install -r requirements.txt
python dr/drill.py
python dr/write_reports.py
python -m pytest tests/ dr/test_workflow.py -v
```

Trên Linux, cùng runner dùng chaos/kill_region.py --mode netblock --mock và
restore --backend bare. environment.json sẽ ghi platform=linux, backend=bare,
mock=true. Dùng venv Linux riêng, không tái sử dụng venv Windows.

Có thể chạy trọn quy trình bằng `bash dr/finish_linux.sh`. Script cài dependency
nếu thiếu, chạy hai drill, điền lại báo cáo, chạy tests và đóng gói ZIP.

Nếu WSL vừa được cài và Windows yêu cầu restart, sau khi restart chạy tại thư
mục gốc trong PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\dr\finish_wsl.ps1
```

Script cài Ubuntu 24.04 nếu thiếu, chạy phần lab bằng user root trong distro để cài
dependency, rồi làm trọn quy trình Linux. Nó không tự restart Windows.
Nếu Windows vẫn báo WSL chưa khả dụng sau Restart và còn cập nhật chờ áp dụng,
chọn **Update and restart** hoặc **Settings → Windows Update → Restart now**.
Trên Windows mới, Restart thông thường có thể trì hoãn việc áp dụng cập nhật;
không cần bật lại các tính năng đã Enabled hay cài lại WSL liên tục.
Nếu WSL2 chưa khả dụng nhưng WSL1 đã khả dụng, script dùng WSL1 cho bare lab
và ghi rõ phiên bản vào environment.json. Không đổi default WSL của Windows.

## Chạy bán tự động từng phần

```bash
python dr/health_checker.py --interval 5 --threshold 3 --duration 100
python dr/runbook.py --primary a --target b --backend fs
```

Cần có snapshot trước khi chạy runbook. Lệnh mặc định hỏi y/N, không đồng ý
thì không gọi failover. --auto chỉ dành cho drill/CI. Failover/runbook trả exit
code khác 0 nếu abort. Xem run/*.log, reports/runbook-run.jsonl và
reports/failover-events.jsonl để điều tra.

## Evidence để nộp

Giữ reports/drill-1-nodr.jsonl, reports/drill-2-withdr.jsonl,
reports/health-events.jsonl, reports/failover-events.jsonl,
reports/replication.jsonl, chaos/chaos-events.jsonl và ba báo cáo Markdown.
runbook-run.jsonl, environment.json và measure-drill-*.json bị .gitignore bỏ
qua nhưng báo cáo tham chiếu chúng: nếu nộp bằng Git, thêm rõ bằng
`git add -f reports/runbook-run.jsonl reports/environment.json reports/measure-drill-1.json reports/measure-drill-2.json`.
Không thêm .venv/, state sinh ra hoặc logs/archive không thuộc lượt nộp.
