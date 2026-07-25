# Setup Guide / 部署指南

## 1. System Dependencies / 系统依赖

### FFmpeg (for transcoding / 用于转码)

本项目使用 Jellyfin FFmpeg 以获得 Intel QSV 硬件加速支持。
This project uses Jellyfin FFmpeg for Intel QSV hardware acceleration.

**Ubuntu / Debian:**
```bash
# 1. 安装 Intel media 驱动（QSV 所需）
sudo apt update
sudo apt install intel-media-va-driver vainfo

# 2. 安装 Jellyfin FFmpeg
curl -fsSL https://repo.jellyfin.org/install-debuntu.sh | sudo bash
sudo apt install jellyfin-ffmpeg
```

**Verify:**
```bash
# QSV 硬件加速
vainfo
/usr/lib/jellyfin-ffmpeg/ffmpeg -encoders | grep qsv
/usr/lib/jellyfin-ffmpeg/ffprobe -version
```

If QSV is not available, update `config.py`:
```python
FFMPEG_PATH = "/usr/bin/ffmpeg"
FFPROBE_PATH = "/usr/bin/ffprobe"
```

### Python

No third-party Python packages are required. The project uses only the standard library.

```bash
python3 --version  # requires Python 3.8+
```

---

## 2. Project Setup / 项目配置

```bash
git clone <repo-url> /path/to/video_convert
cd /path/to/video_convert
```

### Directory structure / 目录结构

```
video_convert/
├── bin/                 # Static ffmpeg with libvmaf (excluded from git)
├── data/                # Runtime data: logs, history, models (excluded from git)
│   ├── app.log
│   ├── history.db               # SQLite (WAL mode)
│   ├── history.db-shm
│   ├── history.db-wal
│   ├── models/
│   │   └── vmaf_v0.6.1.json
│   └── vmaf_logs/               # Per-transcode frame-level JSON logs (auto-purged after 7 days)
├── src/
│   ├── __init__.py
│   ├── config.py
│   ├── history.py
│   ├── probe.py
│   ├── quality.py
│   ├── scanner.py
│   └── transcoder.py
├── tests/
│   ├── conftest.py
│   ├── test_history.py
│   ├── test_main.py
│   ├── test_probe.py
│   ├── test_scanner.py
│   └── test_transcoder.py
├── main.py
├── pyproject.toml
├── README.md
└── docs/SETUP.md
```

### Configuration / 配置

Edit `src/config.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LIBRARIES` | `{"test1": "/mnt/test1"}` | Libraries: name → `{input, output, cache}` paths. String form uses same dir for all three. |
| `SCAN_INTERVAL` | 1200 | Scan cycle interval (seconds) |
| `FAILED_RETRY_BASE` | 3600 | Base delay for failed retry exponential backoff (seconds) |
| `FAILED_RETRY_MAX` | 604800 | Max delay for failed retry (seconds) |
| `MAX_WORKERS` | 2 | Parallel transcode jobs |
| `TEMP_PREFIX` | `.work_` | Prefix for temp directories |
| `VIDEO_EXTENSIONS` | `{".mp4", ".mkv", ...}` | File extensions to consider |
| `SKIP_CODECS` | `{"hevc", "av1"}` | Codecs that skip transcoding |
| `VIDEO_QUALITY` | 20 | QSV quality (lower = better, range ~1-51) |
| `VIDEO_PRESET` | `slow` | QSV preset (speed/quality trade-off) |
| `AUDIO_PASSTHROUGH_CODECS` | `{"aac"}` | Audio codecs to copy instead of re-encode |
| `KEEP_SUBTITLES` | `True` | Passthrough subtitle streams |
| `KEEP_ATTACHMENTS` | `True` | Passthrough attachment streams (fonts, etc.) |
| `VMAF_SUBSAMPLE` | 30 | Evaluate 1 frame every N frames |
| `VMAF_LOG_RETENTION_DAYS` | 7 | Auto-purge VMAF json logs older than this |
| `VMAF_ACCEPT_HIGH` | 93 | VMAF ≥ this → accept if ratio ≤ `ACCEPT_RATIO_MAX` |
| `VMAF_ACCEPT_LOW` | 85 | VMAF < this → reject |
| `ACCEPT_RATIO_MAX` | 1.00 | Max output/input size ratio for high-VMAF accept |
| `ACCEPT_RATIO_LOW` | 0.75 | Max ratio for middle-range VMAF (≥25% smaller) |

**Library format examples / 多库格式示例:**

```python
# Simple: input = output = cache
LIBRARIES = {"movies": "/mnt/media/movies"}

# Per-directory: separate output and cache
LIBRARIES = {
    "movies": {
        "input": "/mnt/media/movies",
        "output": "/mnt/out/movies",
        "cache": "data/temp",
    },
}
```

---

## 3. VMAF Quality Assessment / VMAF 质量评估

VMAF (Video Multi-Method Assessment Fusion) is an optional post-transcode quality check.
It compares the original and transcoded video and produces a score (0–100).

### Components

| Component | Path | Source |
|-----------|------|--------|
| Static ffmpeg (libvmaf) | `bin/ffmpeg` | johnvansickle.com |
| VMAF model | `data/models/vmaf_v0.6.1.json` | Netflix/vmaf |
| Python wrapper | `src/quality.py` | project source |

### Setup / 安装

#### Option A: Copy from existing machine (recommended for migration)

```bash
# On the source machine
tar czf vmaf-assets.tar.gz \
  bin/ffmpeg \
  data/models/vmaf_v0.6.1.json

# Copy archive to the new machine, then:
tar xzf vmaf-assets.tar.gz -C /path/to/video_convert
```

#### Option B: Fresh install / 全新安装

**ffmpeg (static build with libvmaf):**
Provided by https://johnvansickle.com/ffmpeg/

```bash
curl -Lo bin/ffmpeg.tar.xz \
  https://johnvansickle.com/ffmpeg/builds/ffmpeg-git-amd64-static.tar.xz
tar -xf bin/ffmpeg.tar.xz --strip-components=1 \
  ffmpeg-git-*-amd64-static/ffmpeg -C bin/
rm bin/ffmpeg.tar.xz
chmod +x bin/ffmpeg
```

**VMAF model:**
From Netflix/vmaf repository:
```bash
mkdir -p data/models
curl -Lo data/models/vmaf_v0.6.1.json \
  https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.json
```

### Verify

```bash
python3 -c "from src.config import VMAF_ENABLED; print('VMAF enabled:', VMAF_ENABLED)"
```

`VMAF_ENABLED = True` when `bin/ffmpeg` exists.

### Behaviour / 行为

- VMAF runs automatically after each transcode, before the original file is deleted.
- If VMAF is unavailable (binary missing, timeout, or error), the result is **REJECTED** — no score means no accept.
- The score is stored in `data/history.db` under the `vmaf_score` column.
- ACCEPT/REJECT log lines include VMAF when available:
  ```
  ACCEPT: foo.mkv (856 MB, smaller, 33.1%, VMAF 96.54, 1627.3s)
  REJECT: foo.mkv (VMAF 82.61, ratio 54.2%, keeping original, 4365.9s)
  ```
- Per-frame JSON logs are written to `data/vmaf_logs/` and auto-purged after 7 days.
- Quality rules:
  - VMAF ≥ 93 → accept if output is not larger than input (ratio ≤ 1.00)
  - VMAF 85–92 → accept only if output is ≥25% smaller (ratio ≤ 0.75)
  - VMAF < 85 → reject
  - VMAF unavailable → reject

---

## 4. Running / 运行

```bash
cd /path/to/video_convert
python3 main.py
```

To run in background:
```bash
nohup python3 main.py > data/app.log 2>&1 &
```

### Monitoring / 监控

- Log file: `data/app.log`
- History: `data/history.db` (SQLite; inspect with `sqlite3 data/history.db "SELECT * FROM files"`)
- Progress lines: `[timestamp] INFO: PROGRESS: filename - 50%`
- Completion lines: `[timestamp] INFO: ACCEPT/REJECT: filename (size, smaller/larger, ratio, VMAF, time)`

---

## 5. Troubleshooting / 故障排除

| Symptom | Cause | Fix |
|---------|-------|------|
| `TRANSCODE FAILED: exit code 1` | ffmpeg error | Check `.ffmpeg.log` in the `.work_*` temp directory |
| `VMAF failed for ...` | libvmaf binary or model missing | Run `python3 -c "from src.config import VMAF_ENABLED; print(VMAF_ENABLED)"` |
| Unexpectedly low VMAF | Corrupt source or frame misalignment | Inspect `data/vmaf_logs/<stem>.json` for per-frame scores |
| `SKIP (codec=hevc)` | Already HEVC, no re-encode needed | Normal behaviour |
| Stale `.work_*` dirs | Process crash or power outage | Auto-cleaned each scan cycle; `mark_stale_started_as_failed` resets stuck entries |
| NFS locked `.nfs*` files | NFS file handle leak | `lsof +D /path/to/dir` to find the holding process |
| Long transcode timeout (4h) | NFS hang or very large file | Default batch timeout kills stuck tasks; they retry next cycle with backoff |
