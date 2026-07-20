# Roadmap / 开发路线图

## ✔ v1.0 — Core Stable (已发布)

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
- [x] Per-file 4-hour timeout — kill stuck transcodes individually instead of halting the entire batch
- [x] Per-transcode VMAF JSON audit logs with 7-day auto-purge
- [x] Thread-safe History (SQLite WAL mode + RLock)
- [x] **Startup validation** — verify library paths, executables, VMAF deps, and disk space before entering scan loop
---

## ✔ v1.1 — Resilience & Polish (已发布)

- [x] **Localise temp directories** — default `cache` to `data/temp` instead of CIFS mount, eliminating `Stale file handle` errors
- [x] **Granular audio stream mapping** — per-stream codec decision instead of all-or-nothing passthrough (e.g., `-c:a:0 copy -c:a:1 aac`)
- [x] **Graceful shutdown** — drain in-flight transcodes on SIGTERM instead of killing them immediately
- [x] **VMAF soft-fail mode** — configurable `VMAF_REQUIRED` flag: when False, unavailable VMAF defaults to accept instead of reject

---

## 🔄 v2.0 — Web UI & API (规划中)

- [ ] **REST API** (FastAPI) — expose transcode queue, history, and progress
- [ ] **Web dashboard** — browse libraries, view history, trigger manual transcodes
- [ ] **Live progress streaming** — WebSocket for real-time per-file progress
- [ ] **VMAF score explorer** — browse per-frame VMAF logs from rejected files to tune thresholds

---

## 🔄 v3.0 — Golang Port (规划中)

- [ ] Port core scanner + transcoder + quality gate to Go
- [ ] Compare concurrency models (goroutines vs ThreadPoolExecutor)
- [ ] Native inotify/FSEvents support (no polling on local filesystems)

---

## ❌ Cancelled / 已取消

- **Automated test suite** — 已停止维护，`tests/` 保留供参考；单人工具实测 > mock CI 测试
- **Smart pass-through** — 已 HEVC/AV1 的文件已在前端跳过，remux 容器无实际收益，取消
- **Transcode checkpoint / resume** — 实现复杂且 ROI 低（hevc_qsv 编码器无法从中间帧恢复），取消
