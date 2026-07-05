# Setup Guide / 部署指南

## 1. System Dependencies / 系统依赖

### FFmpeg (for transcoding / 用于转码)

本项目使用 Jellyfin FFmpeg 以获得 Intel QSV 硬件加速支持。
This project uses Jellyfin FFmpeg for Intel QSV hardware acceleration.

**Ubuntu / Debian:**
```bash
# Add Jellyfin repository (see https://jellyfin.org/downloads/)
curl -fsSL https://repo.jellyfin.org/install.sh | sudo bash
sudo apt install jellyfin-ffmpeg
```

**Verify:**
```bash
/usr/lib/jellyfin-ffmpeg/ffmpeg -version
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
├── bin/             # Static ffmpeg with libvmaf (excluded from git)
├── data/            # Runtime data: logs, history, models (excluded from git)
│   ├── app.log
│   ├── history.json
│   └── models/
│       └── vmaf_v0.6.1.json
├── src/
│   ├── config.py
│   ├── history.py
│   ├── probe.py
│   ├── quality.py
│   ├── scanner.py
│   └── transcoder.py
├── main.py
└── docs/SETUP.md
```

### Configuration / 配置

Edit `src/config.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `LIBRARIES` | `{"test1": "/mnt/test1"}` | Watch directories (name → path) |
| `SCAN_INTERVAL` | 1200 | Scan interval (seconds) |
| `FAILED_RETRY_BASE` | 3600 | Failed retry base delay (seconds) |
| `FAILED_RETRY_MAX` | 604800 | Failed retry max delay (seconds) |
| `MAX_WORKERS` | 2 | Parallel transcode jobs |
| `VIDEO_QUALITY` | 20 | QSV quality (lower = better) |

---

## 3. VMAF Quality Assessment / VMAF 质量评估

VMAF (Video Multi-Method Assessment Fusion) is an optional post-transcode quality check.
It compares the original and transcoded video and produces a score (0–100).

The VMAF module consists of three parts:

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

`VMAF_ENABLED = True` when both `bin/ffmpeg` and `data/models/vmaf_v0.6.1.json` exist.

### Behaviour / 行为

- VMAF runs automatically after each successful transcode, before the original file is deleted.
- If VMAF is unavailable (binary/model missing, timeout, or error), transcode still succeeds — the score is just omitted.
- The score is stored in `data/history.json` under the `vmaf_score` field of each entry.
- DONE log includes VMAF when available:
  ```
  DONE: foo.mkv (856 MB, smaller, 33.1%, VMAF 96.54, 1627.3s)
  ```

---

## 4. Running / 运行

```bash
cd /path/to/video_convert
python3 main.py 2>&1 | tee -a data/app.log
```

To run in background:
```bash
nohup python3 main.py > data/app.log 2>&1 &
```

### Monitoring / 监控

- Log file: `data/app.log`
- History: `data/history.json` (JSON, human-readable)
- Progress lines: `[timestamp] INFO: PROGRESS: filename - 50%`
- Completion lines: `[timestamp] INFO: DONE: filename (size, smaller, ratio, VMAF, time)`

---

## 5. Troubleshooting / 故障排除

| Symptom | Cause | Fix |
|---------|-------|-----|
| `TRANSCODE FAILED: exit code 1` | ffmpeg error | Check `.ffmpeg.log` in the `.work_*` temp directory |
| `VMAF failed for ...` | libvmaf binary or model missing | Run `python3 -c "from src.config import VMAF_ENABLED; print(VMAF_ENABLED)"` |
| `SKIP (codec=hevc)` | Already HEVC, no re-encode needed | Normal behaviour |
| Stale `.work_*` dirs | Process crash or power outage | Auto-cleaned each scan cycle; `mark_stale_started_as_failed` resets stuck entries |
| NFS locked `.nfs*` files | NFS file handle leak | `lsof +D /path/to/dir` to find the holding process |
