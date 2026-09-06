# video_convert

Automated video transcoding daemon with Intel QSV hardware acceleration and optional VMAF quality assessment.

Watches directories for new video files, transcodes them via Intel Quick Sync Video (default: AV1, switchable to HEVC), and can evaluate the output with VMAF to decide whether to accept or reject the result. Audio, subtitle, and attachment streams are preserved intelligently.

## Features

- **Intel QSV hardware encoding** — default `av1_qsv` (AV1, `--encoder av1`), optional `hevc_qsv` main10 (`--encoder hevc`); per-encoder params (quality/preset/B-frames) in `config.ENCODER_PARAMS`; software-decode fallback for QSV-crash codecs
- **VMAF quality gate (opt-in)** — `--vmaf sync|async`; accepts only when quality and compression ratio meet configurable thresholds; off by default
- **Multi-library** — each library can have independent input, output, and cache directories, configured in `libraries.conf`
- **Audio passthrough** — all-AAC sources copy audio; mixed codecs re-encode to AAC
- **Subtitle + attachment passthrough** — keeps fonts and subs intact
- **Exponential-backoff retry** — failed entries wait longer each cycle, never retry-spam
- **Crash-safe** — stale `.work_*` temp dirs and `started` history entries are cleaned on startup
- **Per-file 5-hour timeout** — prevents NFS hangs from freezing the daemon forever
- **SQLite history** — thread-safe, WAL-mode, zero dependencies beyond stdlib
- **Async VMAF scoring** — `--vmaf async`: keep source, score in a background worker, keep exactly one file

## Quick Start

```bash
# 1. Install Jellyfin FFmpeg (for QSV transcoding)
curl -fsSL https://repo.jellyfin.org/install.sh | sudo bash
sudo apt install jellyfin-ffmpeg7

# 2. Clone & configure
git clone <url> video_convert
cd video_convert

# 3. Configure libraries — run once to auto-generate a sample libraries.conf
#    in the project root, then fill in your media paths.
python3 main.py --help  # generates libraries.conf if missing
# 每行一个库：名称 = 输入[, 输出[, 缓存]]。输出/缓存省略时与输入相同。
#   movies = /mnt/movies
#   shows  = /mnt/shows, /mnt/out, /mnt/cache
# 优先级：--library CLI 参数 > libraries.conf > (无内置默认)

# 4. Run
python3 main.py

# (Optional) Enable VMAF quality gate — see docs/SETUP.md for the self-built
# unified ffmpeg (/opt/ffmpeg-qsv-vmaf/bin/ffmpeg, QSV + libvmaf) and model.
python3 main.py --vmaf async
```

## Architecture

```
main.py            — CLI + event loop: scan → transcode → (assess) → repeat
src/
  config.py        — all tunables in one file; libraries loaded from libraries.conf
  walker.py        — os.walk-based file discovery (filters dotfiles/temp dirs)
  filter.py        — history-based quick filter before queue submission
  transcoder.py    — transcode_stage(pipeline, job) 两段式转码主流程（仅编码，产物留 cache 临时目录，落盘/打分由 post_process_stage 接手）
  ffmpeg_runner.py — ffmpeg subprocess execution + progress parsing
  process_guard.py — PID registry, per-file timeout kill, stale-started recovery
  path_utils.py    — temp/unique path helpers
  temp_cleanup.py  — stale .work_* cleanup (skips active jobs)
  vmaf_async.py    — async VMAF scoring workers (pending_vmaf state machine)
  quality.py       — VMAF computation and accept/reject decision
  history.py       — SQLite-persisted state machine (started/pending_vmaf/success/rejected/failed/not required)
  probe.py         — ffprobe wrapper for stream + codec detection
  validation.py    — startup validation of paths/executables/disk space
  ai_analyze.py    — optional LLM analysis of transcode logs
```

See [docs/SETUP.md](docs/SETUP.md) for detailed setup, configuration reference, and troubleshooting.
See [docs/ROADMAP.md](docs/ROADMAP.md) for development roadmap and known issues.
