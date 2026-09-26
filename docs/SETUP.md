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
│   ├── config.py
│   ├── application/
│   │   ├── __init__.py
│   │   ├── job.py
│   │   ├── pipeline.py
│   │   ├── post_process.py
│   │   └── transcoder.py
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── filter.py
│   │   └── walker.py
│   ├── media/
│   │   ├── __init__.py
│   │   ├── ffmpeg_runner.py
│   │   ├── ffmpeg_vmaf_scorer.py
│   │   └── probe.py
│   ├── policy/
│   │   ├── __init__.py
│   │   └── quality.py
│   ├── runtime/
│   │   ├── __init__.py
│   │   ├── process_guard.py
│   │   ├── shutdown.py
│   │   └── temp_cleanup.py
│   ├── storage/
│   │   ├── __init__.py
│   │   └── history.py
│   ├── support/
│   │   ├── __init__.py
│   │   ├── format_utils.py
│   │   ├── path_utils.py
│   │   └── validation.py
│   └── tools/
│       ├── __init__.py
│       └── ai_analyze.py
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   ├── test_ai_analyze.py
│   ├── test_benchmarks.py
│   ├── test_config.py
│   ├── test_ffmpeg_vmaf_scorer.py
│   ├── test_filter.py
│   ├── test_history.py
│   ├── test_main.py
│   ├── test_pipeline.py
│   ├── test_post_transcode.py
│   ├── test_probe.py
│   ├── test_probe_integration.py
│   ├── test_quality.py
│   ├── test_regression.py
│   ├── test_transcode_e2e.py
│   ├── test_transcoder.py
│   ├── test_transcoder_helpers.py
│   ├── test_validation.py
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
| `SW_DECODE_FALLBACK_CODECS` | `{"vc1", "mpeg4", "vp9"}` | Codecs that fall back to software decode + hwupload when QSV decode crashes |
| `VMAF_N_SUBSAMPLE` | 30 | Evaluate 1 frame every N frames |
| `VMAF_N_THREADS` | 0 | libvmaf threads: 0 = auto (leave 1 core for transcoding), >0 explicit. CLI: `--vmaf-threads` |
| `VMAF_FFMPEG_PATH` | `/opt/ffmpeg-qsv-vmaf/bin/ffmpeg` | ffmpeg binary used for VMAF scoring (must include libvmaf) |
| `VMAF_MODEL_PATH` | `/opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json` | VMAF model (negative-float features). CLI: `--vmaf-model` |
| `VMAF_TIMEOUT` | 36000 | VMAF scoring timeout per file (seconds, 10 h) |
| `VMAF_HWACCEL` | `True` | QSV hardware-accelerated decode for VMAF; falls back to software on failure (`VMAF_HW_FALLBACK`) |
| `VMAF_MODE` | `"off"` | VMAF scoring mode: `off` (ratio check only) / `sync` (score temp, then land) / `async` (land first, then score the landed file). CLI: `--vmaf off|sync|async` |
| `VMAF_WORKERS` | 1 | Post-process pool threads (`post_pool` max_workers) |
| `VMAF_EXTREME_LOW` | 60 | 当前未被任何代码路径读取（旧异步 worker 链的调用方在 v1.6 一并删除）；极低分数目前和其他低分一样，只是被 `VMAF_ACCEPT_LOW`（<80 拒绝）覆盖，不存在额外的"极端低分"判定 |
| `VMAF_ACCEPT_HIGH` | 93 | VMAF ≥ this → accept if ratio ≤ `ACCEPT_RATIO_MAX` |
| `VMAF_ACCEPT_LOW` | 80 | VMAF < this → reject |
| `ACCEPT_RATIO_MAX` | 1.00 | Max output/input size ratio for high-VMAF accept |
| `ACCEPT_RATIO_LOW` | 0.75 | Max ratio for middle-range VMAF (≥25% smaller) |
| `VMAF_LOG_RETENTION_DAYS` | 7 | Auto-purge VMAF json logs older than this |

**libraries.conf 格式示例 / Library format examples:**

```
# Simple: input = output = cache
movies = /mnt/media/movies

# Separate output (cache defaults to output)
series = /mnt/media/series, /mnt/out/series

# Per-directory: separate output and cache
shows = /mnt/media/shows, /mnt/out/shows, /mnt/cache

# 优先级：--library CLI 参数 > libraries.conf > (无内置默认)
```

---

## 3. VMAF Quality Assessment / VMAF 质量评估

VMAF (Video Multi-Method Assessment Fusion) is an optional post-transcode quality check.
It compares the original and transcoded video and produces a score (0–100).

Requires `src/media/ffmpeg_vmaf_scorer.py` for the ffmpeg/libvmaf wrapper and
`src/policy/quality.py` for the accept/reject decision.

VMAF 模型文件为 `/opt/ffmpeg-qsv-vmaf/models/vmaf_v0.6.1neg.json`（负浮点特征，匹配自编译统一版 libvmaf），
为默认模型；如需其他版本可通过 `--vmaf-model` CLI 指定，并在 `VMAF_MODEL_PATH` 中手动配置。

模型下载方式见部署章节 [1.2](#12-ffmpeg--自编译qsv--libvmaf-统一版--unified-build)。

### Behaviour / 行为

- VMAF 默认关闭，需显式启用：`--vmaf sync`（先打分后落盘）或 `--vmaf async`（先落盘后打分，见下）。
- 打分前对源与产物做帧序对齐（`setpts=N/30/TB` 按帧号线性化），消除 VFR→CFR 帧序漂移造成的分数虚低；两路帧数不一致时由 framesync 以短流为准截断配对，帧数只做日志诊断，从不阻断或改变打分结果。
- If VMAF is unavailable (binary missing, timeout, or error) and `VMAF_MODE != "off"`, the result is **REJECTED** — no score means no accept.
- The score is stored in `data/history.db` under the `vmaf_score` column.
- ACCEPT/REJECT log lines include VMAF when available:
  ```
  ACCEPT: foo.mkv (856 MB, smaller, 33.1%, VMAF 96.54, transcode 1627.3s + vmaf 42.1s)
  REJECT: foo.mkv (VMAF 82.61, ratio 54.2%, transcode 4365.9s + vmaf 38.2s, keeping original)
  ```
- Per-frame JSON logs are written to `data/vmaf_logs/` and auto-purged after 7 days.
- libvmaf 多线程可通过 `--vmaf-threads N` 调整（0=auto，默认留 1 核给转码）。
- Quality rules:
  - VMAF ≥ 93 → accept if output is not larger than input (ratio ≤ 1.00)
  - VMAF 80–92 → accept only if output is ≥25% smaller (ratio ≤ 0.75)
  - VMAF < 80 → reject
  - VMAF unavailable → reject (仅当 `VMAF_MODE != "off"`)

### Async mode / 异步模式（`--vmaf async`）

与同步模式的唯一区别是落盘与打分的先后：先把转码产物放入输出库，再对库中文件打分（所见即所得）。打分仍为 inline（阻塞 post 线程），无后台 worker，判定规则与 sync 完全一致（同一 `vmaf_verdict`）。

1. 转码成功 → 输出文件立即移入输出区，随后对库中文件打分（终态落定前源文件保留）。
2. 打分完成：接受 → 删除源文件（除非 `--keep-source`），状态 `success`；拒绝 → 按 `KEEP_TARGET` 处置库中文件（默认删除），状态 `rejected`，源文件保留。
3. `ratio` 超过 `ACCEPT_RATIO_MAX` 时必拒（`KEEP_TARGET=True` 时仍落盘，仅记 rejected）。
4. 输出落盘成功后先记为 `pending_postprocess`；进程中断后下次启动直接复用库中文件打分，不重转码。
5. 恢复时源文件缺失记 `failed`；库中产物缺失降级为 `pending_transcode`，按普通转码重新入队。
6. 落盘到写入 `pending_postprocess` 之间的极短窗口仍按旧逻辑重转码，可能产生 `.1` 去重文件。

新增状态：

| 状态 | 含义 |
|---|---|
| `pending_postprocess` | async 产物已落盘，源文件仍在，等待打分和终态处理 |

注意事项：

- **打分前输出文件已在库中**，采集器（Jellyfin/Plex）可能在 verdict 落定前扫到被拒文件。
- `pending_postprocess` 仅表示 async 产物已落盘、终态尚未记录；启动恢复不依赖当前 VMAF 模式。
- 关停中断的打分**不会**记为任何终态（不是 `rejected` 也不是 `failed`）：记录保持原状态，下次启动时按 `pending_postprocess` 恢复逻辑直接复用库中文件重新打分，不重转码。

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
| Stale `.work_*` dirs | Process crash or power outage | Auto-cleaned each scan cycle (`cleanup_stale_temp_dirs`); interrupted `pending_transcode` history entries are requeued for re-transcoding at startup |
| NFS locked `.nfs*` files | NFS file handle leak | `lsof +D /path/to/dir` to find the holding process |
| 转码卡死超时（5h） | NFS hang or very large file | Per-file timeout kills the stuck task and marks it `failed`; retry only via `--retry-failed` at next startup, no automatic backoff (`--codec-timeout` 可调） |
