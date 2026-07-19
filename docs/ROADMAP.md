# Roadmap / 开发路线图

## v1.0 — Core Stable (当前)

### Done / 已完成

- [x] Intel QSV hardware encoding (`hevc_qsv`, main10) with automatic software-scale fallback
- [x] VMAF quality gate with configurable thresholds
- [x] Multi-library support (independent input / output / cache directories)
- [x] Audio passthrough (all-AAC copy; mixed codecs re-encode to AAC)
- [x] Subtitle + attachment stream passthrough
- [x] `os.walk`-based polling scanner with codec/history filtering
- [x] SQLite-persisted state machine (`started` / `success` / `rejected` / `failed` / `not required`)
- [x] Exponential-backoff retry for failed entries
- [x] Stale `.work_*` temp dir cleanup on each scan cycle
- [x] Stale `started` history entries marked `failed` on startup (power outage resilience)
- [x] 4-hour batch timeout to prevent NFS hang freeze
- [x] Per-transcode VMAF JSON audit logs with 7-day auto-purge
- [x] Thread-safe History (SQLite WAL mode + RLock)

### Known Issues / 已知问题

- [x] **CIFS `Stale file handle`** — resolved by defaulting cache to local `data/temp/`
- [x] **vpp_qsv filter reinit failure** — some H.264 files trigger `Error reinitializing filters`. Software-scale fallback works but adds latency.
- [x] **No graceful shutdown for in-flight transcodes** — SIGTERM now drains pending tasks (up to 120s) before exit; SIGKILL only as last resort.
- [x] **VMAF is all-or-nothing** — resolved by `VMAF_REQUIRED` toggle; set to `False` to accept without quality gate.

---

## v1.1 — Resilience & Polish

### Short-term / 短期

- [x] **Localise temp directories** — default `cache` to `data/temp` instead of CIFS mount, eliminating `Stale file handle` errors
- [x] **Granular audio stream mapping** — per-stream codec decision instead of all-or-nothing passthrough (e.g., `-c:a:0 copy -c:a:1 aac`)
- [x] **Graceful shutdown** — drain in-flight transcodes on SIGTERM instead of killing them immediately
- [x] **VMAF soft-fail mode** — configurable `VMAF_REQUIRED` flag: when False, unavailable VMAF defaults to accept instead of reject

### Medium-term / 中期

- [ ] **Transcode checkpoint / resume** — save encoding progress metadata to resume interrupted long transcodes
- [ ] **`config.py` validation** — validate library paths exist and are writable on startup

---

## v2.0 — Web UI & API

- [ ] **REST API** (FastAPI) — expose transcode queue, history, and progress
- [ ] **Web dashboard** — browse libraries, view history, trigger manual transcodes
- [ ] **Live progress streaming** — WebSocket for real-time per-file progress
- [ ] **VMAF score explorer** — browse per-frame VMAF logs from rejected files to tune thresholds

---

## v3.0 — Golang Port

- [ ] Port core scanner + transcoder + quality gate to Go
- [ ] Compare concurrency models (goroutines vs ThreadPoolExecutor)
- [ ] Native inotify/FSEvents support (no polling on local filesystems)

---

## Archive / 归档

Old roadmap items from the original `README.md` have been migrated here:

- Web UI stage (moved to v2.0)
- Golang port (moved to v3.0)
- **Automated test suite** — 实值低，撤销；单人工具实测 > mock CI 测试
- **Smart pass-through** — 已 HEVC/AV1 的文件已在前端跳过，remux 容器无实际收益，取消
