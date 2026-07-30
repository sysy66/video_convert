# Setup Guide / 部署指南

## 1. System Dependencies / 系统依赖

### 1.1 FFmpeg — Jellyfin（用于转码 / for transcoding）

路径: `/usr/lib/jellyfin-ffmpeg/ffmpeg`

用途：Intel QSV 硬件编码（`hevc_qsv` / `av1_qsv`）、`ffprobe` 探针、音视频解封装与复用。

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
vainfo
/usr/lib/jellyfin-ffmpeg/ffmpeg -hwaccels
/usr/lib/jellyfin-ffmpeg/ffprobe -version
```

### 1.2 FFmpeg — johnvansickle（用于 VMAF 评估 / for VMAF）

路径: `/opt/johnvansickle-ffmpeg/ffmpeg`

用途：软件解码 + libvmaf 计算 VMAF 分数。该 ffmpeg 不参与转码，仅在质量评估阶段使用。

**Ubuntu / Debian:**

```bash
sudo mkdir -p /opt/johnvansickle-ffmpeg
curl -Lo /tmp/ffmpeg.tar.xz \
  https://johnvansickle.com/ffmpeg/builds/ffmpeg-git-amd64-static.tar.xz
sudo tar -xf /tmp/ffmpeg.tar.xz --strip-components=1 \
  ffmpeg-git-*-amd64-static/ffmpeg -C /opt/johnvansickle-ffmpeg/
rm /tmp/ffmpeg.tar.xz
sudo chmod +x /opt/johnvansickle-ffmpeg/ffmpeg

mkdir -p data/models
curl -Lo data/models/vmaf_v0.6.1.json \
  https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1.json
```

**Verify:**
```bash
/opt/johnvansickle-ffmpeg/ffmpeg -version
python3 -c "from src.config import VMAF_ENABLED; print('VMAF enabled:', VMAF_ENABLED)"
```

`VMAF_ENABLED = True` when `/opt/johnvansickle-ffmpeg/ffmpeg` exists.

### 1.3 FFmpeg — 自编译（QSV + libvmaf 统一版 / unified build）

路径: `/opt/ffmpeg-qsv-vmaf/bin/ffmpeg`

用途：将 Intel QSV 硬件编码（`hevc_qsv` / `av1_qsv`）和 libvmaf 质量评估集成到同一个 ffmpeg 二进制中，
替代之前需要两个独立 ffmpeg 的方案。该二进制供转码管线统一调用。

**Ubuntu / Debian:**

```bash
# 1. 安装 Intel GPU 驱动（QSV 所需，建议从 Intel 官方 repo 获取最新版）
wget -O- https://repositories.intel.com/gpu/intel-graphics.key | sudo gpg --dearmor -o /usr/share/keyrings/intel-graphics.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu noble unified" | sudo tee /etc/apt/sources.list.d/intel-gpu.list
sudo apt update
sudo apt install intel-media-va-driver vainfo

# 2. 安装编译依赖
sudo apt install build-essential pkg-config nasm meson \
  libvpl-dev libva-dev libdav1d-dev libmfx-gen1.2

# 3. 编译 libvmaf（Debian trixie 没有 libvmaf-dev 包，需从 Netflix 源码编译）
git clone https://github.com/Netflix/vmaf.git /tmp/vmaf-src
cd /tmp/vmaf-src/libvmaf
meson setup build --buildtype release
ninja -C build
sudo ninja -C build install
sudo ldconfig

# 4. 克隆 FFmpeg（--depth 1 只取最新源码，避免完整克隆过大导致断线）
git clone --depth 1 https://git.ffmpeg.org/ffmpeg.git /tmp/ffmpeg-src
cd /tmp/ffmpeg-src

# 5. 配置（libvmaf + QSV 同时启用，嵌入 RUNPATH 使二进制自带库搜索路径）
./configure --prefix=/opt/ffmpeg-qsv-vmaf \
  --disable-static --enable-shared \
  --enable-libvpl --enable-libvmaf --enable-libdav1d \
  --enable-gpl --enable-version3 \
  --enable-ffprobe --disable-doc \
  --extra-ldflags='-Wl,-rpath,/opt/ffmpeg-qsv-vmaf/lib'

# 6. 编译 & 安装
make -j$(nproc)
sudo make install
```

**下载对应 VMAF 模型：**
自编译 ffmpeg 通常链接较新版本 libvmaf，需使用负浮点特征模型：

```bash
mkdir -p data/models
curl -Lo data/models/vmaf_v0.6.1neg.json \
  https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1neg.json
```

**Verify:**

```bash
/opt/ffmpeg-qsv-vmaf/bin/ffmpeg -hwaccels
/opt/ffmpeg-qsv-vmaf/bin/ffmpeg -filters | grep libvmaf
/opt/ffmpeg-qsv-vmaf/bin/ffmpeg -encoders | grep qsv
```

使用 `--ffmpeg /opt/ffmpeg-qsv-vmaf/bin/ffmpeg --ffprobe /opt/ffmpeg-qsv-vmaf/bin/ffprobe` 切换到此自定义构建。

### 1.4 Python

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
├── data/                # Runtime data: logs, history, models (excluded from git)
│   ├── app.log
│   ├── history.db               # SQLite (WAL mode)
│   ├── history.db-shm
│   ├── history.db-wal
│   ├── models/
│   │   ├── vmaf_v0.6.1.json       # 旧版整数特征
│   │   └── vmaf_v0.6.1neg.json    # 新版负浮点特征
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

Requires `src/quality.py` as the Python wrapper.

VMAF 模型文件（`data/models/vmaf_v0.6.1.json`，旧版整数特征，适合 johnvansickle / jellyfin ffmpeg）。
自编译 QSV+libvmaf 版 ffmpeg 可使用 `data/models/vmaf_v0.6.1neg.json`（新版负浮点特征），
通过 `--vmaf-ffmpeg` CLI 指定路径，并在 `VMAF_MODEL_PATH` 中手动配置对应模型。

各模型的下载方式见对应部署章节（[1.2](#12-ffmpeg--johnvansickle用于-vmaf-评估--for-vmaf) / [1.3](#13-ffmpeg--自编译qsv--libvmaf-统一版--unified-build)）。

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
