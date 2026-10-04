# video_convert

Automated video transcoding daemon with Intel QSV hardware acceleration and optional VMAF quality assessment.

Watches directories for new video files, transcodes them via Intel Quick Sync Video (default: AV1, switchable to HEVC), and can evaluate the output with VMAF to decide whether to accept or reject the result. Audio, subtitle, and attachment streams are preserved intelligently.

## Features

- **Intel QSV hardware encoding** — default `av1_qsv` (AV1, `--encoder av1`), optional `hevc_qsv` main10 (`--encoder hevc`); per-encoder params (quality/preset/B-frames) in `config.ENCODER_PARAMS`; software-decode fallback for QSV-crash codecs
- **VMAF quality gate (opt-in)** — `--vmaf sync|async`; accepts only when quality and compression ratio meet configurable thresholds; off by default
- **Multi-library** — each library can have independent input, output, and cache directories, configured in `libraries.conf`
- **Audio passthrough** — all-AAC sources copy audio; mixed codecs re-encode to AAC
- **Subtitle + attachment passthrough** — keeps fonts and subs intact
- **Manual failed-entry retry** — `--retry-failed` requeues all `failed` history entries once at startup; no automatic backoff yet
- **Crash-safe** — stale `.work_*` temp dirs are cleaned on startup/each scan cycle; interrupted (`pending_transcode`) entries are automatically requeued for re-transcoding
- **Per-file 5-hour timeout** — prevents NFS hangs from freezing the daemon forever
- **SQLite history** — thread-safe, WAL-mode, zero dependencies beyond stdlib
- **Async VMAF scoring** — `--vmaf async`: land first, then score the library file (what you see is what you score); same verdict rules as sync

## Quick Start

```bash
# 1. Install Jellyfin FFmpeg (for QSV transcoding)
curl -fsSL https://repo.jellyfin.org/install.sh | sudo bash
sudo apt install jellyfin-ffmpeg7

# 2. Clone & configure
git clone <url> video_convert
cd video_convert

# 3. Configure libraries — run once to auto-generate a sample libraries.conf
#    in the project root (Ctrl+C after the warning; --help exits before the
#    template is written, so don't use it for this step), then fill in your
#    media paths.
python3 main.py
# 每行一个库：名称 = 输入[, 输出[, 缓存]]。输出省略时默认与输入相同；缓存省略时默认与输出相同。
#   movies = /mnt/movies
#   series = /mnt/series, /mnt/series_out
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
  application/     — job model, pipeline orchestration, transcoding, post-processing
  ingest/          — directory discovery and pre-submit filtering
  media/           — ffprobe, ffmpeg and VMAF adapters
  storage/         — SQLite-persisted transcoding history
  runtime/         — process guards, shutdown state and stale-temp cleanup
  policy/          — VMAF accept/reject rules
  support/         — path helpers and startup validation
  tools/           — optional standalone utilities
```

See [docs/SETUP.md](docs/SETUP.md) for detailed setup, configuration reference, and troubleshooting.
See [docs/ROADMAP.md](docs/ROADMAP.md) for development roadmap and known issues.
See [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) for the target architecture and phased migration plan.
