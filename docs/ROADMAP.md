# Roadmap / 开发路线图

## ✔ v1.0 — Core Stable (已发布)

- [x] **Intel QSV hardware encoding** (`hevc_qsv`, main10) with automatic software-scale fallback
- [x] **VMAF quality gate** with configurable thresholds
- [x] **Multi-library support** (independent input / output / cache directories)
- [x] **Audio passthrough** (all-AAC copy; mixed codecs re-encode to AAC)
- [x] **Subtitle + attachment stream passthrough**
- [x] **Recursive directory scanner** — `os.walk`-based discovery with codec/history filtering
- [x] **SQLite-persisted state machine** (`started` / `success` / `rejected` / `failed` / `not required`)
- [x] **Exponential-backoff retry** for failed entries
- [x] **Stale temp dir cleanup** — `.work_*` dirs cleaned on each scan cycle
- [x] **Power outage resilience** — stale `started` entries marked `failed` on startup
- [x] **Per-file timeout** — kill stuck transcodes individually (default 5 h) instead of halting the entire batch
- [x] **VMAF audit logs** — per-transcode JSON logs with 7-day auto-purge
- [x] **Thread-safe History** — SQLite WAL mode + RLock
- [x] **Startup validation** — verify library paths, executables, VMAF deps, and disk space before entering scan loop
---

## ✔ v1.1 — Resilience & Polish (已发布)

- [x] **Localise temp directories** — per-library `cache` dir for `.work_*` temp files, eliminating CIFS `Stale file handle` errors; also scanned for stale temp cleanup
- [x] **Granular audio stream mapping** — per-stream codec decision instead of all-or-nothing passthrough (e.g., `-c:a:0 copy -c:a:1 aac`)
- [x] **Graceful shutdown** — drain in-flight transcodes on SIGTERM instead of killing them immediately
- [x] **VMAF soft-fail mode** — configurable `VMAF_MODE` flag: when `off`, unavailable VMAF defaults to accept instead of reject

---

## ✔ v1.2 — Config & CLI Polish (已发布)

- [x] **Dynamic `SKIP_CODECS`** — starts as empty set; `VIDEO_ENCODER` auto-added at startup
- [x] **`--skip-codecs` CLI flag** — append skip codecs from command line
- [x] **`CODEC_TIMEOUT = 18000`** — increased from 14400 to 18000 (5 h)
- [x] **`VMAF_TIMEOUT = 18000`** — increased from 7200 to 18000 (5 h)
- [x] **Test improvements** — 4 new tests covering skip-codecs logic, timeout propagation; 133 total tests passing

---

## ✔ v1.3 — Test Quality (已发布)

See [TEST_PLAN.md](TEST_PLAN.md) for the detailed phased plan.

- [x] **Phase 1 — Infrastructure** — Forgejo Actions CI（pytest/coverage/ruff 全绿）、coverage 门槛 69、ruff 静态检查、测试依赖声明
- [x] **Phase 2 — Test-base refactor** — 纯 pytest 统一、conftest factory fixtures、markers 注册、config.py 100% 覆盖
- [x] **Phase 3 — Fill unit-test gaps** — transcoder 73%、main() 91%、CLI 全量、ai_analyze 98%，总覆盖 82%
- [x] **Phase 4 — Integration tests** — 6 个合成视频 fixtures、ffprobe 14 用例、真实 QSV transcode e2e（hevc 真跑 / av1 硬件不支持 skip）、真实 libvmaf 3 用例、`--run-slow` 机制（CI 零改动，默认全 skip）
- [x] **Phase 5 — Regression & performance** — fixtures SHA256 锁定 + 重编码确定性回归、pytest-benchmark 编码/VMAF 基准（默认 disable）、CI 矩阵 3.10/3.11/3.12

---

## ✔ v1.4 — Async VMAF (已发布)

- [x] **Async VMAF scoring（`--vmaf-async`）** — 转码后先落输出区不删源，后台 worker 打分后取一；`pending_vmaf` 状态、启动恢复 kill -9 残留、优雅关闭 drain、ratio 超标必拒不入队
- [x] **CLI timeout flags（`--codec-timeout` / `--vmaf-timeout`）** — 转码与 VMAF 打分超时可用 CLI 覆盖；`VMAF_TIMEOUT` 默认值提高到 36000（10 h，适配全片 VMAF 打分耗时）
- [x] **Decoupled scan & transcode** — 转码池常驻进程，扫描周期独立于转码进度（新入库文件最多等一个扫描间隔，不再排队等批次转码完成）；提交前 history 轻量过滤 + `_pending` 内存去重（队列只含真正待处理文件）；stale temp 清理跳过活跃转码目录；SIGTERM 取消排队任务（下次启动重扫）
- [x] **Incomplete-input guard** — 写入稳定检测（`FILE_STABLE_WINDOW`，mtime 窗口内不入队，防残缺文件被转码）；async VMAF 极端低分（`VMAF_EXTREME_LOW`）改记 failed 退避重试而非永久 rejected（防写入中文件被误判低分锁死）
- [x] **Orphan record guard** — worker 处理前检查源文件存在（入队后被删则直接丢弃，不白跑 ffmpeg、不污染 history）；扫描轮清理源文件已消失的终态记录（rejected/failed/not required），`success` 永不清理（源缺失是 ACCEPT 删源的正常状态，保留同名去重）
- [x] **Module split / 模块拆分** — `transcoder.py`（927 行）按职责拆分为 `path_utils` / `ffmpeg_runner` / `process_guard` / `temp_cleanup` / `vmaf_async`；`transcoder` 仅保留 `transcode()` 主流程并 re-export 全部符号，`src.transcoder.*` 兼容层零破坏（main.py 与既有测试无需改动引用）

---

## ✔ v1.5 — Encoder & Config & VMAF Polish (已发布)

- [x] **媒体库配置外置 `libraries.conf`** — 从代码迁移到项目根目录配置文件（每行一个库：`名称 = 输入[, 输出[, 缓存]]`），首次运行自动生成带注释模板，开箱即用；`--library` CLI 优先于配置文件
- [x] **VMAF CLI 三态重构（`--vmaf off|sync|async`）** — 默认关闭（`VMAF_MODE=off`），需显式启用；VMAF hwaccel 解码默认开启，失败自动回退软件（`--no-vmaf-hwaccel` / `--no-vmaf-hw-fallback` 可关）
- [x] **VMAF 打分帧序对齐** — 打分前对源与产物按帧号线性化（`setpts=N/30/TB`），消除 VFR→CFR 帧序漂移导致的逐帧错位；实测（Arc A310 + av1_qsv）同源分数 63.19 → 94.83 且三次一致；删去 fps 网格投影兜底，帧数不可得或 >1% 时不下分
- [x] **VMAF 取分可靠性** — 优先解析 stderr（规避 libvmaf 2.3.0 json pooled 聚合 bug）、仅使用 json `pooled_metrics` 取分、`log_path` 补默认值
- [x] **VMAF 模型移入 `/opt/ffmpeg-qsv-vmaf/models/`** — 随自编译统一版安装，默认模型 `vmaf_v0.6.1neg.json`；清理过时的 johnvansickle 与旧模型上下文
- [x] **sync/async 打分决策统一** — 两条路径共用同一决策逻辑；打分失败（None）与极端低分（`VMAF_EXTREME_LOW`）统一按 failed 退避重试，不再永久 rejected
- [x] **libvmaf 多线程（`--vmaf-threads` / `VMAF_N_THREADS`）** — 0=auto（默认留 1 核给转码），>0 显式指定；>4 线程收益递减
- [x] **帧数校验闸门（引入→根治→移除）** — vpp_qsv 静默失败曾导致截断输出（帧数校验 + 视频尾窗探测拦截，含单元测试）；升级自编译 ffmpeg 到 release/8.1（n8.1.2，修复附件流截断 bug）后根治，校验层移除
- [x] **Async VMAF 日志补全** — 转码耗时（codec + vmaf 双计时）写入异步打分日志

---

## 🗺 v2.0 — Web UI & API (远期规划，暂缓)

- [ ] **REST API** (FastAPI) — expose transcode queue, history, and progress
- [ ] **Web dashboard** — browse libraries, view history, trigger manual transcodes
- [ ] **Live progress streaming** — WebSocket for real-time per-file progress
- [ ] **VMAF score explorer** — browse per-frame VMAF logs from rejected files to tune thresholds

---

## 🗺 v3.0 — Golang Port (远期规划，暂缓)

- [ ] Port core scanner + transcoder + quality gate to Go
- [ ] Compare concurrency models (goroutines vs ThreadPoolExecutor)
- [ ] Native inotify/FSEvents support (no polling on local filesystems)

---

## ❌ Cancelled / 已取消

- **Smart pass-through** — 已 HEVC/AV1 的文件已在前端跳过，remux 容器无实际收益，取消
- **Transcode checkpoint / resume** — 实现复杂且 ROI 低（hevc_qsv 编码器无法从中间帧恢复），取消
