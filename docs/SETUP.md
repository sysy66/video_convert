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
# 新版仓库已把 jellyfin-ffmpeg 元包拆分为 jellyfin-ffmpeg7 / jellyfin-ffmpeg8（旧的包名不存在），
# 两者安装路径相同（/usr/lib/jellyfin-ffmpeg/）；本机验证版本为 8.1.2，装 8 即可
sudo apt install jellyfin-ffmpeg8
```

**Verify:**
```bash
vainfo
/usr/lib/jellyfin-ffmpeg/ffmpeg -hwaccels
/usr/lib/jellyfin-ffmpeg/ffprobe -version
```

### 1.2 FFmpeg — 自编译（QSV + libvmaf 统一版 / unified build）

路径: `/opt/ffmpeg-qsv-vmaf/bin/ffmpeg`

用途：将 Intel QSV 硬件编码（`hevc_qsv` / `av1_qsv`）与 libvmaf 质量评估集成到同一个 ffmpeg 二进制中
（jellyfin 版 ffmpeg 不带 libvmaf）。程序默认用它执行 VMAF 打分（`config.VMAF_FFMPEG_PATH`）；
转码默认仍用 jellyfin（`config.FFMPEG_PATH`），如需 QSV 编码也走本构建，可用
`--ffmpeg` / `--ffprobe` 切换。

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

# 4. 克隆 FFmpeg 并切到稳定分支（--depth 1 只取最新源码，避免完整克隆过大导致断线；
#    git.ffmpeg.org 偶尔 DNS 解析失败，可用 GitHub 镜像 https://github.com/FFmpeg/FFmpeg.git）
git clone --depth 1 --branch release/8.1 https://github.com/FFmpeg/FFmpeg.git /tmp/ffmpeg-src
cd /tmp/ffmpeg-src
# 注意：必须使用稳定分支（release/8.1，如 tag n8.1.2），不要用 git master——
# master 在 2026-07 曾引入附件流 bug（-map 0:t? 复制附件 + QSV 编码时输出被截断为个位数帧）

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
自编译 ffmpeg 通常链接较新版本 libvmaf，需使用负浮点特征模型，随统一版安装到 `/opt/ffmpeg-qsv-vmaf/models/`：

```bash
sudo mkdir -p /opt/ffmpeg-qsv-vmaf/models
sudo curl -Lo /opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json \
  https://raw.githubusercontent.com/Netflix/vmaf/master/model/vmaf_v0.6.1neg.json
```

**Verify:**

```bash
/opt/ffmpeg-qsv-vmaf/bin/ffmpeg -hwaccels
/opt/ffmpeg-qsv-vmaf/bin/ffmpeg -filters | grep libvmaf
/opt/ffmpeg-qsv-vmaf/bin/ffmpeg -encoders | grep qsv
```

使用 `--ffmpeg /opt/ffmpeg-qsv-vmaf/bin/ffmpeg --ffprobe /opt/ffmpeg-qsv-vmaf/bin/ffprobe` 切换到此自定义构建。

### 1.3 Python

No third-party Python packages are required. The project uses only the standard library.

```bash
python3 --version  # requires Python 3.10+
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
├── data/                # Runtime data: logs, history (excluded from git)
│   ├── app.log
│   ├── history.db               # SQLite (WAL mode)
│   ├── history.db-shm
│   ├── history.db-wal
│   └── vmaf_logs/               # Per-transcode frame-level JSON logs (auto-purged after 7 days)
├── src/
│   ├── __init__.py
│   ├── ai_analyze.py
│   ├── config.py
│   ├── ffmpeg_runner.py
│   ├── filter.py
│   ├── history.py
│   ├── path_utils.py
│   ├── probe.py
│   ├── process_guard.py
│   ├── quality.py
│   ├── temp_cleanup.py
│   ├── transcoder.py
│   ├── validation.py
│   ├── vmaf_async.py
│   └── walker.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   ├── test_ai_analyze.py
│   ├── test_benchmarks.py
│   ├── test_config.py
│   ├── test_filter.py
│   ├── test_history.py
│   ├── test_main.py
│   ├── test_probe.py
│   ├── test_probe_integration.py
│   ├── test_quality.py
│   ├── test_regression.py
│   ├── test_transcode_e2e.py
│   ├── test_transcoder.py
│   ├── test_transcoder_helpers.py
│   ├── test_validation.py
│   ├── test_vmaf_async.py
│   ├── test_vmaf_integration.py
│   └── test_walker.py
├── .forgejo/workflows/   # CI workflow (Forgejo Actions)
├── libraries.conf        # Library definitions (auto-generated on first run)
├── main.py
├── pyproject.toml
├── test_run.sh
├── README.md
└── docs/SETUP.md
```

### Configuration / 配置

媒体库定义在项目根目录的 `libraries.conf`（每行一个库，首次运行自动生成模板），
其余可调参数在 `src/config.py`：

| Variable | Default | Description |
|----------|---------|-------------|
| `LIBRARIES` | 从 `libraries.conf` 读取 | Libraries: name → `{input, output, cache}` paths. String form uses same dir for all three |
| `SCAN_INTERVAL` | 1200 | Scan interval (seconds). Scan cadence is independent of transcode progress: newly added files queue within one interval |
| `FILE_STABLE_WINDOW` | 300 | Only submit files whose mtime is older than this (seconds); guards against transcoding files that are still being written |
| `MAX_WORKERS` | 2 | Parallel transcode jobs |
| `CODEC_TIMEOUT` | 18000 | Per-file transcode timeout (seconds, 5 h) |
| `TEMP_PREFIX` | `.work_` | Prefix for temp directories |
| `VIDEO_EXTENSIONS` | `{".mp4", ".mkv", ...}` | File extensions to consider |
| `SKIP_CODECS` | `set()` | Codecs that skip transcoding; the active `VIDEO_ENCODER` codec is auto-added at startup |
| `VIDEO_ENCODER` | `"av1"` | Encoding target: `"av1"` → `av1_qsv` (AV1, Arc), `"hevc"` → `hevc_qsv` main10 (Iris Xe). CLI: `--encoder` |
| `ENCODER_PARAMS` | per-encoder dict | Per-encoder settings: codec, profile, quality, preset, b_frames, extbrc, adaptive_i, adaptive_b, b_strategy (av1) |
| `VIDEO_GOP` | 240 | GOP size (keyframe interval) |
| `VIDEO_LOOK_AHEAD_DEPTH` | 70 | QSV look-ahead depth (quality/efficiency tuning) |
| `AUDIO_CODEC` / `AUDIO_BITRATE` | `"aac"` / `"256k"` | Audio re-encode target for non-passthrough streams |
| `AUDIO_PASSTHROUGH_CODECS` | `{"aac"}` | Audio codecs to copy instead of re-encode |
| `KEEP_SUBTITLES` | `True` | Passthrough subtitle streams |
| `KEEP_ATTACHMENTS` | `True` | Passthrough attachment streams (fonts, etc.) |
| `SW_DECODE_FALLBACK_CODECS` | `{"vc1"}` | Codecs that fall back to software decode + hwupload when QSV decode crashes |
| `VMAF_SUBSAMPLE` | 30 | Evaluate 1 frame every N frames |
| `VMAF_N_THREADS` | 0 | libvmaf threads: 0 = auto (leave 1 core for transcoding), >0 explicit. CLI: `--vmaf-threads` |
| `VMAF_ALIGN_FPS` | `True` | Rate-align source & output with an fps filter before scoring (fixes VFR→CFR drift) |
| `VMAF_ALIGN_FPS_RATE` | `"30000/1001"` | Fallback target rate when source probing fails |
| `VMAF_FFMPEG_PATH` | `/opt/ffmpeg-qsv-vmaf/bin/ffmpeg` | ffmpeg binary used for VMAF scoring (must include libvmaf) |
| `VMAF_MODEL_PATH` | `/opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json` | VMAF model (negative-float features). CLI: `--vmaf-model` |
| `VMAF_TIMEOUT` | 36000 | VMAF scoring timeout per file (seconds, 10 h) |
| `VMAF_HWACCEL` | `True` | QSV hardware-accelerated decode for VMAF; falls back to software on failure (`VMAF_HW_FALLBACK`) |
| `VMAF_REQUIRED` | `False` | True=reject if VMAF unavailable; False=accept on ratio check only. VMAF enabled via `--vmaf sync|async` |
| `VMAF_ASYNC` | `False` | Async scoring: keep source, score in background worker, keep one file. CLI: `--vmaf async` |
| `VMAF_WORKERS` | 1 | Number of async VMAF scoring worker threads |
| `VMAF_EXTREME_LOW` | 60 | VMAF below this in scoring marks the file failed for retry instead of permanently rejecting it (suggests an incomplete input/output) |
| `VMAF_ACCEPT_HIGH` | 93 | VMAF ≥ this → accept if ratio ≤ `ACCEPT_RATIO_MAX` |
| `VMAF_ACCEPT_LOW` | 80 | VMAF < this → reject |
| `ACCEPT_RATIO_MAX` | 1.00 | Max output/input size ratio for high-VMAF accept |
| `ACCEPT_RATIO_LOW` | 0.75 | Max ratio for middle-range VMAF (≥25% smaller) |
| `VMAF_LOG_RETENTION_DAYS` | 7 | Auto-purge VMAF json logs older than this |

**libraries.conf 格式示例 / Library format examples:**

```
# Simple: input = output = cache
movies = /mnt/media/movies

# Per-directory: separate output and cache
shows = /mnt/media/shows, /mnt/out/shows, /mnt/cache

# 优先级：--library CLI 参数 > libraries.conf > (无内置默认)
```

---

## 3. VMAF Quality Assessment / VMAF 质量评估

VMAF (Video Multi-Method Assessment Fusion) is an optional post-transcode quality check.
It compares the original and transcoded video and produces a score (0–100).

Requires `src/quality.py` as the Python wrapper.

VMAF 模型文件为 `/opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json`（负浮点特征，匹配自编译统一版 libvmaf），
为默认模型；如需其他版本可通过 `--vmaf-model` CLI 指定，并在 `VMAF_MODEL_PATH` 中手动配置。

模型下载方式见部署章节 [1.2](#12-ffmpeg--自编译qsv--libvmaf-统一版--unified-build)。

### Behaviour / 行为

- VMAF 默认关闭，需显式启用：`--vmaf sync`（同步打分）或 `--vmaf async`（异步打分，见下）。
- 打分前对源与产物做帧率对齐（`VMAF_ALIGN_FPS`），消除 VFR→CFR 帧序漂移造成的分数虚低。
- If VMAF is unavailable (binary missing, timeout, or error) and `VMAF_REQUIRED=True`, the result is **REJECTED** — no score means no accept.
- The score is stored in `data/history.db` under the `vmaf_score` column.
- ACCEPT/REJECT log lines include VMAF when available:
  ```
  ACCEPT: foo.mkv (856 MB, smaller, 33.1%, VMAF 96.54, 1627.3s)
  REJECT: foo.mkv (VMAF 82.61, ratio 54.2%, keeping original, 4365.9s)
  ```
- Per-frame JSON logs are written to `data/vmaf_logs/` and auto-purged after 7 days.
- libvmaf 多线程可通过 `--vmaf-threads N` 调整（0=auto，默认留 1 核给转码）。
- Quality rules:
  - VMAF ≥ 93 → accept if output is not larger than input (ratio ≤ 1.00)
  - VMAF 80–92 → accept only if output is ≥25% smaller (ratio ≤ 0.75)
  - VMAF < 80 → reject
  - VMAF unavailable → reject (仅当 `VMAF_REQUIRED=True`)
  - VMAF < 60（`VMAF_EXTREME_LOW`）→ 视为输入/产物异常，按 failed 退避重试，不永久拒绝

### Async mode / 异步模式（`--vmaf async`）

默认（同步）模式下 VMAF 打分在转码线程内执行，一次打分可阻塞数分钟到数小时。
开启异步模式后：

1. 转码成功 → 输出文件立即移入输出区，**源文件保留在原位**，状态记为 `pending_vmaf`。
2. 后台 worker 线程（`VMAF_WORKERS` 个，默认 1）随后对比源文件与新文件打分。
3. 打分完成：接受 → 删除源文件，状态 `success`；拒绝（含打分失败）→ 删除输出区新文件，状态 `rejected`。
4. `ratio` 超过 `ACCEPT_RATIO_MAX` 时必拒，直接丢弃输出不排队打分（打分很贵，不浪费）。

注意事项：

- **打分窗口期源文件与输出文件双份存在**，请确保磁盘空间充足。
- 优雅关闭（SIGINT/SIGTERM）时主循环会等待所有排队打分完成（`drain`）。
- 进程被 `kill -9` 后残留的 `pending_vmaf` 记录会在下次启动时自动恢复重新打分。

```
python3 main.py --vmaf async ...（其余参数照常）
```

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
| `VMAF failed for ...` | libvmaf binary or model missing | Run `/opt/ffmpeg-qsv-vmaf/bin/ffmpeg -filters | grep libvmaf` and check `/opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json` |
| Unexpectedly low VMAF | Corrupt source or frame misalignment | Inspect `data/vmaf_logs/<stem>.json` for per-frame scores |
| `SKIP (codec=hevc)` | Already HEVC, no re-encode needed | Normal behaviour |
| 转码输出仅数帧 | 自编译版为 git-master 构建（附件流截断 bug，2026-07 曾引入） | 升级到 release/8.1 稳定版（n8.1.2）重编，见 [1.2](#12-ffmpeg--自编译qsv--libvmaf-统一版--unified-build)；已修复版本无此问题 |
| Stale `.work_*` dirs | Process crash or power outage | Auto-cleaned each scan cycle; `mark_stale_started_as_failed` resets stuck entries |
| NFS locked `.nfs*` files | NFS file handle leak | `lsof +D /path/to/dir` to find the holding process |
| 转码卡死超时（5h） | NFS hang or very large file | Per-file timeout kills the stuck task; it retries next cycle with backoff (`--codec-timeout` 可调） |
