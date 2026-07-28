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
运行时依赖: `/opt/intel-media-<version>/lib`（Intel 运行时栈，见下）

用途：将 Intel QSV 硬件编码（`hevc_qsv` / `av1_qsv`）和 libvmaf 质量评估集成到同一个 ffmpeg 二进制中，
替代之前需要两个独立 ffmpeg 的方案。该二进制供转码管线统一调用。

**背景 / 为什么要单独编译 Intel 运行时栈：**

早期做法是直接用系统的 `libvpl-dev`/`libva-dev`（发行版打包版本）编译 FFmpeg，结果同样的
`-global_quality`/`-preset` 参数下，QSV 编码画质和体积明显不如 Jellyfin 官方 ffmpeg。对比
[jellyfin-ffmpeg 官方构建脚本](https://github.com/jellyfin/jellyfin-ffmpeg/blob/master/docker-build.sh)
确认：**FFmpeg 源码本身没有针对 QSV 编码路径的实质性补丁**，真正的差异在于 Jellyfin 私有编译、
钉死版本的 4 个 Intel 底层库——比发行版仓库的系统包新出一大截（实测对比：iHD 驱动 25.4.6 vs
Debian trixie 系统包 25.2.3，相隔约 13 个月的驱动迭代）。这些库才是 QSV 编码时实际做码率控制/
RDO 决策的地方，FFmpeg 只是把参数透传给它们。因此本节的重点是单独复刻这套 Intel 运行时栈，
再让自编译的 FFmpeg 链接它；下面第 4 步虽然也会 clone `jellyfin-ffmpeg` 的源码，但只是借用它
打好 tag 的版本号做可复现的版本锁定（避免用 upstream 裸 `--depth 1` 拉到漂移不定的 HEAD），
不是因为它的 FFmpeg 源码补丁本身能解决画质问题——真正起作用的还是这套 Intel 运行时栈。

> **换机器/换时间复刻时先核对版本号**：下表是编译本文时 `jellyfin-ffmpeg7 7.1.4-3` 对应的钉死版本，
> Jellyfin 会持续更新。先用 `/usr/lib/jellyfin-ffmpeg/ffmpeg -version` 看当前系统装的版本号，
> 再去 GitHub 找对应 tag（例如 `v7.1.4-3`），跑一遍下面这行提取该 tag 实际钉死的版本号，
> 不要盲目照抄本文的表格：
> ```bash
> curl -s https://raw.githubusercontent.com/jellyfin/jellyfin-ffmpeg/v7.1.4-3/docker-build.sh \
>   | grep -E "git clone -b|commit/[0-9a-f]+\.patch"
> ```

| 组件 | tag（截至 jellyfin-ffmpeg v7.1.4-3） | 作用 |
|---|---|---|
| gmmlib | `intel-gmmlib-22.9.0` | media-driver 的内存管理依赖 |
| libva | `2.23.0` | VAAPI 基础库 |
| media-driver | `intel-media-25.4.6`（+ patch `25fb926`） | 实际做硬件编码 RDO/码率控制的 VAAPI 驱动，产出 `iHD_drv_video.so` |
| vpl-gpu-rt | `intel-onevpl-25.4.6`（+ patch `c7eb030`） | 11 代+/Xe/Arc 核显的 oneVPL GPU 运行时，产出 `libmfx-gen.so` |
| libvpl | `v2.16.0` | oneVPL API 分发层，产出 `libvpl.so`，FFmpeg `--enable-libvpl` 直接依赖它 |

> 老硬件（Gen ≤ 11 / Rocket Lake 及更早，不支持 `av1_qsv`）还需要额外编译 `intel-vaapi-driver`
> （i965 驱动）和 `MediaSDK`（legacy MSDK runtime）。只用 Xe/Arc 核显跑 `hevc_qsv`/`av1_qsv` 可跳过。

**关于运行时环境变量**：VAAPI 驱动路径 / oneVPL 运行时路径默认是 libva/libvpl 在运行时读环境变量
（`LIBVA_DRIVERS_PATH`/`ONEVPL_PRIORITY_PATH`）动态查找的。1.1 节的 Jellyfin 官方 ffmpeg 之所以不需要
配置这两个变量，是因为它们编译 libva/libvpl 时把默认搜索路径直接硬编码进了源码（`va/va.c` 里
`secure_getenv("LIBVA_DRIVERS_PATH")` 被替换成写死的路径字符串），不再依赖环境变量，同时把
`LIBVA_DRIVER_NAME` 改名成 `LIBVA_DRIVER_NAME_JELLYFIN`，避免宿主机上给其他软件设置的同名变量
意外影响到它们的私有驱动栈。下面的编译步骤对 libva（2b）和 libvpl（2e）打了同款 patch，全部指向
本文的独立前缀，编译完成后 `/opt/ffmpeg-qsv-vmaf/bin/ffmpeg` 同样不需要调用者设置任何环境变量。

**Ubuntu / Debian:**

```bash
# 1. 安装编译依赖
sudo apt update
sudo apt install build-essential pkg-config nasm meson cmake autoconf libtool \
  libdrm-dev libpciaccess-dev git wget patch \
  libvpl-dev libva-dev

# 2. 编译 Intel 运行时栈到独立前缀（版本号见上表）
export PREFIX=/opt/intel-media-25.4.6
sudo mkdir -p "$PREFIX"
mkdir -p /tmp/intel-media-src && cd /tmp/intel-media-src

# 2a. gmmlib
git clone -b intel-gmmlib-22.9.0 --depth=1 https://github.com/intel/gmmlib.git
cmake -S gmmlib -B gmmlib/build -DCMAKE_INSTALL_PREFIX="$PREFIX"
make -j$(nproc) -C gmmlib/build && sudo make -C gmmlib/build install

# 2b. libva（硬编码驱动搜索路径到本文的独立前缀，运行时不用再设 LIBVA_DRIVERS_PATH；
#     LIBVA_DRIVER_NAME 改名隔离，避免宿主机上其他软件的同名环境变量误伤这份私有 libva）
git clone -b 2.23.0 --depth=1 https://github.com/intel/libva.git
cd libva
sed -i 's|secure_getenv("LIBVA_DRIVERS_PATH")|"'"$PREFIX"'/lib/dri"|g' va/va.c
sed -i 's|secure_getenv("LIBVA_DRIVER_NAME")|secure_getenv("LIBVA_DRIVER_NAME_QSVVMAF")|g' va/va.c
./autogen.sh
./configure --prefix="$PREFIX" --enable-drm --disable-glx --disable-x11 --disable-wayland --disable-docs
make -j$(nproc) && sudo make install
cd /tmp/intel-media-src

# 2c. media-driver（依赖 gmmlib + libva，用 PKG_CONFIG_PATH 指向刚装好的版本，避免误链系统库）
git clone -b intel-media-25.4.6 --depth=1 https://github.com/intel/media-driver.git
cd media-driver
wget -q -O - https://github.com/intel/media-driver/commit/25fb926.patch | git apply
PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" cmake -S . -B build \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DCMAKE_C_FLAGS="-Wno-error=array-bounds" -DCMAKE_CXX_FLAGS="-Wno-error=array-bounds" \
  -DENABLE_KERNELS=ON -DENABLE_NONFREE_KERNELS=ON
make -j$(nproc) -C build && sudo make -C build install
cd /tmp/intel-media-src

# 2d. vpl-gpu-rt（新款 oneVPL GPU 运行时，libmfx-gen.so）
git clone -b intel-onevpl-25.4.6 --depth=1 https://github.com/intel/vpl-gpu-rt.git
cd vpl-gpu-rt
wget -q -O - https://github.com/intel/vpl-gpu-rt/commit/c7eb030.patch | git apply
PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig" cmake -S . -B build \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_LIBDIR="$PREFIX/lib" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_RUNTIME=ON -DBUILD_TESTS=OFF -DBUILD_TOOLS=OFF \
  -DMFX_ENABLE_KERNELS=ON -DMFX_ENABLE_ENCTOOLS=ON -DMFX_ENABLE_AENC=ON
make -j$(nproc) -C build && sudo make -C build install
cd /tmp/intel-media-src

# 2e. libvpl（oneVPL 分发层；硬编码运行时搜索路径到本文的独立前缀，运行时不用再设 ONEVPL_PRIORITY_PATH）
git clone -b v2.16.0 --depth=1 https://github.com/intel/libvpl.git
cd libvpl
sed -i 's|ParseEnvSearchPaths(ONEVPL_PRIORITY_PATH_VAR, searchDirList)|searchDirList.push_back("'"$PREFIX"'/lib")|g' libvpl/src/mfx_dispatcher_vpl_loader.cpp
cmake -S . -B build \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" -DCMAKE_INSTALL_BINDIR="$PREFIX/bin" -DCMAKE_INSTALL_LIBDIR="$PREFIX/lib" \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DINSTALL_DEV=ON -DINSTALL_LIB=ON \
  -DBUILD_TESTS=OFF -DBUILD_EXAMPLES=OFF -DBUILD_EXPERIMENTAL=OFF
make -j$(nproc) -C build && sudo make -C build install
cd /tmp/intel-media-src
sudo ldconfig

# 3. 编译 libvmaf（Debian trixie 没有 libvmaf-dev 包，需从 Netflix 源码编译）
git clone https://github.com/Netflix/vmaf.git /tmp/vmaf-src
cd /tmp/vmaf-src/libvmaf
meson setup build --buildtype release
ninja -C build
sudo ninja -C build install
sudo ldconfig

# 4. 克隆 FFmpeg 源码 —— 用 jellyfin-ffmpeg 的官方 tag 而非裸的 upstream `--depth 1` HEAD，
#    否则每次在新机器上跑这行拉到的 FFmpeg 版本都不一样，等于前面钉的 Intel 组件版本白钉了。
#    tag 号跟着 1.1 节 `/usr/lib/jellyfin-ffmpeg/ffmpeg -version` 的输出走，此文对应 v7.1.4-3。
#    只用它的源码树，不用它 debian/rules 里那套 CUDA/AMF/Vulkan/x264/x265 配置——那些是给
#    N 卡/A 卡/软件编码路径用的，我们只用 Intel QSV 硬件编码，沿用下面这份精简 flags 即可。
git clone --branch v7.1.4-3 --depth 1 https://github.com/jellyfin/jellyfin-ffmpeg.git /tmp/ffmpeg-src
cd /tmp/ffmpeg-src

# 5. 配置（libvmaf + QSV 同时启用；--extra-cflags/--extra-ldflags 指向第 2 步编译的
#    Intel 运行时栈，取代系统 libvpl-dev/libva-dev；RPATH 嵌入两处库搜索路径）
./configure --prefix=/opt/ffmpeg-qsv-vmaf \
  --disable-static --enable-shared \
  --enable-libvpl --enable-libvmaf \
  --enable-gpl --enable-version3 \
  --enable-ffprobe --disable-doc \
  --extra-cflags="-I/opt/intel-media-25.4.6/include" \
  --extra-ldflags="-L/opt/intel-media-25.4.6/lib -Wl,-rpath,/opt/intel-media-25.4.6/lib -Wl,-rpath,/opt/ffmpeg-qsv-vmaf/lib"

# 6. 编译 & 安装
make -j$(nproc)
sudo make install
```

**运行时无需设置环境变量：** 前面 2b/2e 步骤已经把驱动/运行时搜索路径硬编码进 libva/libvpl 源码
（跟 1.1 节 Jellyfin 官方 ffmpeg 的做法一致），配合 FFmpeg 编译时嵌入的 RPATH，
`/opt/ffmpeg-qsv-vmaf/bin/ffmpeg` 开箱即用——不依赖调用者是否设置了 `LIBVA_DRIVERS_PATH`/
`ONEVPL_PRIORITY_PATH`，也不会被宿主机上给其他软件设置的同名变量意外影响。
`transcoder.py`/`quality.py` 的 `subprocess.run`/`Popen` 调用不需要为此做任何改动。

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

不同后端使用不同的 VMAF 模型（在 `src/config.py` 的 `_BACKENDS` 中配置）：
- → `data/models/vmaf_v0.6.1.json`（旧版整数特征，johnvansickle / jellyfin ffmpeg）
- → `data/models/vmaf_v0.6.1neg.json`（新版负浮点特征，自编译 ffmpeg）

各模型的下载方式见对应后端的部署章节（[1.2](#12-ffmpeg--johnvansickle用于-vmaf-评估--for-vmaf) / [1.3](#13-ffmpeg--自编译qsv--libvmaf-统一版--unified-build)）。

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
