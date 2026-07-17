# video_convert

Automated video transcoding daemon with Intel QSV hardware acceleration and VMAF quality assessment.

Watches directories for new video files, transcodes them to HEVC 10-bit via Intel Quick Sync Video, then evaluates the output with VMAF to decide whether to accept or reject the result. Audio, subtitle, and attachment streams are preserved intelligently.

## Features

- **Intel QSV hardware encoding** (`hevc_qsv`, main10 profile) with automatic software-scale fallback when vpp_qsv filter reinit fails
- **VMAF quality gate** — transcode is accepted only if quality and compression ratio meet configurable thresholds
- **Multi-library** — each library can have independent input, output, and cache directories
- **Audio passthrough** — all-AAC sources copy audio; mixed codecs re-encode to AAC
- **Subtitle + attachment passthrough** — keeps fonts and subs intact
- **Exponential-backoff retry** — failed entries wait longer each cycle, never retry-spam
- **Crash-safe** — stale `.work_*` temp dirs and `started` history entries are cleaned on startup
- **4-hour batch timeout** — prevents NFS hangs from freezing the daemon forever
- **SQLite history** — thread-safe, WAL-mode, zero dependencies beyond stdlib

## Quick Start

```bash
# 1. Install Jellyfin FFmpeg (for QSV transcoding)
curl -fsSL https://repo.jellyfin.org/install.sh | sudo bash
sudo apt install jellyfin-ffmpeg

# 2. Clone & configure
git clone <url> video_convert
cd video_convert
# edit src/config.py — set LIBRARIES to your watch directory

# 3. (Optional) Set up VMAF
#    Place bin/ffmpeg (johnvansickle static build with libvmaf)
#    and data/models/vmaf_v0.6.1.json in the project tree.
#    See docs/SETUP.md for details.

# 4. Run
python3 main.py
```

## Architecture

```
main.py            — event loop: scan → transcode → assess → repeat
src/
  scanner.py       — os.walk-based file discovery with codec/history filtering
  transcoder.py    — ffmpeg orchestrator, QSV + sw_fallback + progress parser
  quality.py       — VMAF computation and accept/reject decision
  history.py       — SQLite-persisted state machine (started/success/rejected/failed/not required)
  probe.py         — ffprobe wrapper for stream + codec detection
  config.py        — all tunables in one file
```

See [docs/SETUP.md](docs/SETUP.md) for detailed setup, configuration reference, and troubleshooting.
