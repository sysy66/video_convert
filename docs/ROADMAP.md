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
- [x] **Module split / 模块拆分** — `transcoder.py`（927 行）按职责拆分为路径、ffmpeg、进程、临时目录和 VMAF 模块；`transcoder` 收口为 transcode_stage(pipeline, job) 两段式转码主流程，兼容再导出层已移除。当前模块按 `application` / `ingest` / `media` / `runtime` / `storage` / `policy` / `support` 分包。

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

## ✔ v1.6 — Post-process rework (已发布)

- [x] **后台 worker 链删除** — `vmaf_async.py`（队列/worker/recover）与旧异步状态机删除；关停 flag 移入 `src/runtime/shutdown.py`
- [x] **async 先落盘后打分** — `--vmaf async` 改为 land-first inline 打分，打分对象为库中文件（所见即所得）；sync 保持先打分后落盘；判定规则两模式一致
- [x] **后处理 dispatcher** — `post_process_stage` 按 `vmaf_mode` 路由 ratio-only/sync/async 三函数，唯一 try/except/finally 护栏
- [x] **KEEP_TARGET** — 新增：reject（含 ratio 超标）时新文件仍落盘到目标库，仅记 `rejected`（默认关闭，需人工复核）
- [x] **Async post-process resume** — async 产物落盘后记录 `pending_postprocess`；启动时直接恢复打分，缺失产物才回退转码

---

## ◐ v1.7 — 稳定性、调度与质量策略

- [ ] **VMAF 子进程可中断** — 将转码、VMAF 和 ffprobe 子进程纳入统一进程守卫；关停时及时终止子进程，避免等待整个超时时间
- [ ] **输出原子落盘与并发冲突保护** — 目标文件名保留、临时文件复制和最终提交保持原子性，避免同名任务互相覆盖或留下半成品
- [ ] **转码产物完整性检查** — 落盘前用 ffprobe 检查文件可读性、视频流和附属流，校验位深、像素格式及基本流信息
- [ ] **失败重试策略重做** — 记录失败次数和下次重试时间，区分临时错误、输入损坏和配置错误，限制无效重试；同步修正路线图中与实际代码不符的重试说明
- [ ] **有界任务队列与资源预算** — 限制待处理任务数量，避免大媒体库一次扫描创建大量 Future；统一约束转码并发、VMAF 并发和 VMAF 线程数
- [ ] **History 访问和状态一致性** — 统一所有 SQLite 访问的加锁方式，收拢状态转换和产物路径不变量，补充并发读写测试
- [ ] **源文件身份与媒体库边界** — 用文件大小、修改时间或指纹识别同一路径上的新文件；校验媒体库输入目录重叠，避免恢复任务匹配到错误的库
- [ ] **扫描成本控制** — 减少大媒体库的重复遍历和逐文件查询，增加批量历史查询或文件变更筛选
- [ ] **按媒体库配置编码策略** — 允许不同媒体库分别配置编码器、质量参数、音频策略和 VMAF 阈值
- [ ] **运行控制与可观测性** — 增加单次扫描、试运行、队列状态和失败记录查询；日志补充活动任务、排队数量和阶段耗时
- [ ] **故障注入和文档同步** — 覆盖关停中断、VMAF 超时、输出冲突、源文件消失和网络存储失败；清理路线图及安装文档中的退役状态和旧流程说明
- [ ] **可选：智能码率（ab-av1）** — 根据目标 VMAF 分数（全平台默认 93）自动测算最佳压制参数，支持 **10-bit** 深度；不作为 v1.7 完成条件

---

## 🗺 v2.0 — Golang Port (远期规划，暂缓)

- [ ] Port core scanner + transcoder + quality gate to Go
- [ ] Compare concurrency models (goroutines vs ThreadPoolExecutor)
- [ ] Native inotify/FSEvents support (no polling on local filesystems)

---

## 🗺 v3.0 — Web UI & API (远期规划，暂缓)

- [ ] **REST API** (FastAPI) — expose transcode queue, history, and progress
- [ ] **Web dashboard** — browse libraries, view history, trigger manual transcodes
- [ ] **Live progress streaming** — WebSocket for real-time per-file progress
- [ ] **VMAF score explorer** — browse per-frame VMAF logs from rejected files to tune thresholds

---


## ❌ Cancelled / 已取消

- **Smart pass-through** — 已 HEVC/AV1 的文件已在前端跳过，remux 容器无实际收益，取消
- **Transcode checkpoint / resume** — 实现复杂且 ROI 低（hevc_qsv 编码器无法从中间帧恢复），取消
