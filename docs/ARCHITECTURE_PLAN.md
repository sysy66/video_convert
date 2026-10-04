# High-level Architectural Plan / 架构规划

> 本文以 [ROADMAP.md](ROADMAP.md) 为输入，给出 v1.7 的**目标架构**与**分阶段演进路径**（§3–6），以及 v1.8–v1.10 的**版本规划**和远期路线建议（§7–8）。
> 路线图回答“做什么”，本文回答“结构上怎么承载、按什么顺序做”。
> 状态：草案（2026-10），随 v1.7 推进持续修订。

---

## 1. 目标与约束

**目标**

1. 为 v1.7 的 11 个条目提供统一的结构落点，避免逐条打补丁导致 `main.py` / `post_process.py` 继续膨胀。
2. 让每个阶段都可独立发布：测试全绿、行为可回退、不需要一次性大重写。
3. 规划 v1.8–v1.10 的版本主题与条目，让 v1.7 的结构投入在后续版本中持续产生收益。
4. 为远期 v2.0（Go 移植）和 v3.0（Web UI / API）预留清晰边界，但**不提前实现**。

**约束（保持不变）**

- 单进程、单实例（`fcntl` 锁）、线程池并发模型；不引入外部服务（消息队列、数据库服务器）。
- SQLite 作为唯一持久化；仅用标准库 `sqlite3`，不引入 ORM。
- 运行环境：Linux + Intel QSV + 自编译 ffmpeg；媒体库可能位于 NFS/CIFS。
- 运行时零第三方依赖的原则不变（测试依赖除外）。

---

## 2. 现状快照（v1.6）

### 2.1 分层与数据流

```
main.py  ── CLI 解析 → 改写 config 全局变量 → main_loop()
   │        ├─ 启动恢复（pending_postprocess / pending_transcode / --retry-failed）
   │        ├─ 扫描循环：temp 清理 → purge 孤儿记录 → walk → should_submit → submit
   │        └─ 闭包 process_one / post_process + _pending 去重集
   ▼
application/  TranscodePipeline（共享依赖） + TranscodeJob（单文件任务）
   ├─ transcoder.transcode_stage   ffmpeg 编码 + QSV 降级重试 → temp 产物
   └─ post_process.post_process_stage   off / sync / async 三路：ratio 门 → 打分 → 落盘 → 终态
ingest/    walker（os.walk） + filter（history 状态 / mtime 稳定 / codec 探测）
media/     probe（ffprobe） / ffmpeg_runner（Popen + 进度） / ffmpeg_vmaf_scorer（libvmaf）
storage/   History（单连接 SQLite + RLock，files 表 + CHECK 约束）
runtime/   process_guard（ffmpeg PID 集） / shutdown（关停 flag） / temp_cleanup（活跃 temp 集）
policy/    vmaf_verdict / VMAF 日志清理
support/   路径、格式化、启动校验
```

### 2.2 状态机（隐式）

```
(无记录) ──scan──▶ pending_transcode ──编码成功──▶ [内存: temp 产物]
                       │                             │
                       │ 失败                         ├─ off/sync：打分(可选) → 落盘 → success / rejected
                       ▼                             └─ async：落盘 → pending_postprocess → 打分 → success / rejected
                    failed                                                                    
codec 命中 SKIP_CODECS ──▶ not required
```

### 2.3 结构性问题（与 v1.7 条目的对应关系）

| # | 问题 | 代码证据 | 影响的 v1.7 条目 |
|---|------|----------|------------------|
| P1 | **全局可变配置**：CLI 直接改写 `config` 模块属性，10 个模块、百余处直接读 `config.XXX` | `main._apply_cli_overrides`、`post_process` 中读 `config.KEEP_TARGET` / `ACCEPT_RATIO_MAX` | 按媒体库配置编码策略、测试隔离 |
| P2 | **调度逻辑写在入口文件**：`_pending`、`process_one`、`requeue_entry` 都是 `main_loop` 内闭包，无法单测，队列无上限 | `main.py` 闭包；每个文件一次 `pool.submit` | 有界队列与资源预算、运行控制 |
| P3 | **子进程管控分裂**：只有 `ffmpeg_runner` 用 `Popen` 并注册 PID；VMAF 与 ffprobe 用 `subprocess.run`，关停时只能等超时（VMAF 默认 10 h） | `ffmpeg_vmaf_scorer.py:204`、`probe.py:43` | VMAF 子进程可中断 |
| P4 | **两套关停标志**：`main._running` 与 `runtime.shutdown._shutting_down` 并存 | `main.signal_handler` 同时置两处 | 可中断、故障注入 |
| P5 | **状态转换分散**：`mark_*` 散落在 transcoder（3 处）、post_process（16 处）、filter（1 处）；不变量只靠表上 CHECK 约束兜底 | `history.mark_*` 调用点 | History 一致性、重试策略 |
| P6 | **落盘非原子**：`shutil.copyfile` 直接写最终路径；`_unique_path` 先 `exists()` 后写，存在 TOCTOU 竞争 | `post_process._land_output`、`path_utils._unique_path` | 输出原子落盘、并发冲突 |
| P7 | **落盘前无完整性校验**：只检查产物文件存在 | `transcode_stage` 末尾 | 产物完整性检查 |
| P8 | **身份 = 路径**：同路径替换为新文件后仍命中旧 `success` / `rejected` 记录 | `files.filepath PRIMARY KEY` | 源文件身份 |
| P9 | **扫描成本线性放大**：每个文件一次 `get_entry` 查询 + `stat`；每轮全量 `purge_orphan_records` 逐行 `exists()` | `filter._history_allows`、`history.purge_orphan_records` | 扫描成本控制 |
| P10 | **后处理上下文无类型**：`ctx` 字典 + 恢复路径用 `SimpleNamespace` 伪造 job | `post_process._resolve_output` / `_resume_async_postprocess` | 可维护性（所有后处理相关条目） |
| P11 | **失败无分类**：`failed` 只有一段 error 文本，无次数、无下次重试时间 | `files.error` | 失败重试策略重做 |

---

## 3. 目标架构

### 3.1 分层

```
┌─────────────────────────────────────────────────────────────────┐
│ interfaces/   cli.py（daemon / once / dry-run / status / failed） │  ← v3.0 再加 api/
├─────────────────────────────────────────────────────────────────┤
│ app/          Daemon（生命周期） Scheduler（队列+预算）            │
│               Recovery（启动恢复） Scanner（发现+筛选）            │
├─────────────────────────────────────────────────────────────────┤
│ domain/       Job / JobState / transitions（状态机，唯一写状态入口）│
│               Settings / LibraryProfile / EncodingProfile          │
│               QualityPolicy / RetryPolicy / ErrorKind               │
│               stages: transcode → verify → assess → commit         │
├─────────────────────────────────────────────────────────────────┤
│ ports/        JobRepository  MediaProbe  Encoder  QualityScorer    │
│ (Protocol)    OutputStore    ProcessRunner  Clock                  │
├─────────────────────────────────────────────────────────────────┤
│ adapters/     sqlite_repo  ffprobe  ffmpeg_qsv  libvmaf  fs_store  │
│               subprocess_runner                                    │
└─────────────────────────────────────────────────────────────────┘
依赖方向：interfaces → app → domain → ports ← adapters（adapters 只在组装根注入）
```

现有包到目标层的映射（**渐进迁移，不做一次性重命名**）：

| 现有 | 目标 |
|------|------|
| `main.py` | `interfaces/cli.py`（参数解析） + `app/daemon.py`（循环/恢复/关停） |
| `config.py` | `domain/settings.py`（不可变 `Settings` + `LibraryProfile`）+ 默认值常量 |
| `application/transcoder.py`、`post_process.py` | `domain/stages/*`（纯流程）+ `app/` 中的编排 |
| `ingest/` | `app/scanner.py` |
| `media/` | `adapters/`（ffprobe / ffmpeg / libvmaf） |
| `storage/history.py` | `adapters/sqlite_repo.py`，实现 `ports.JobRepository` |
| `runtime/` | `adapters/subprocess_runner.py` + `app/daemon.py`（关停） + `adapters/fs_store.py`（temp） |
| `policy/` | `domain/policy.py` |

### 3.2 设计原则

1. **配置启动后不可变**：CLI / `libraries.conf` / 默认值在组装根合并为一个 frozen `Settings`，按库解析出 `LibraryProfile`；业务代码不再 `import config`。
2. **状态只有一个写入口**：`domain/transitions.py` 定义合法转换表，`JobRepository` 只暴露 `transition(job_id, from, to, **fields)`（带 `WHERE status=?` 的 CAS 更新），非法转换直接抛错。
3. **阶段返回结果，不直接写库**：`transcode / verify / assess / commit` 返回 `StageOutcome`（成功产物或 `Failure(kind, msg)`），由编排层统一调用状态机，消除 `_fail` / `mark_failed` 散点。
4. **所有子进程走同一个 `ProcessRunner`**：统一超时、取消、PID 登记、stderr 截断与日志。
5. **文件系统副作用集中在 `OutputStore`**：temp 分配、名称预留、原子提交、遗留清理都在一个适配器里，可替换为测试用的内存实现。
6. **每阶段保持行为等价**：重构型阶段以现有测试为回归基线，功能型阶段先补测试再改实现。

---

## 4. 关键组件设计

### 4.1 Settings 与按库策略

```python
@dataclass(frozen=True)
class EncodingProfile:      # 取代 VIDEO_ENCODER + ENCODER_PARAMS + AUDIO_* 全局量
    encoder: str            # av1 | hevc
    params: Mapping[str, str]
    audio_codec: str; audio_bitrate: str; audio_passthrough: frozenset[str]
    keep_subtitles: bool; keep_attachments: bool

@dataclass(frozen=True)
class QualityPolicy:        # 取代 VMAF_* / ACCEPT_RATIO_* / KEEP_TARGET
    vmaf_mode: Literal["off", "sync", "async"]
    accept_high: float; accept_low: float; extreme_low: float
    ratio_max: float; ratio_low: float
    keep_target: bool

@dataclass(frozen=True)
class LibraryProfile:
    name: str; input: Path; output: Path; cache: Path
    encoding: EncodingProfile; quality: QualityPolicy
    skip_codecs: frozenset[str]

@dataclass(frozen=True)
class Settings:
    libraries: tuple[LibraryProfile, ...]
    runtime: RuntimeSettings      # 并发、超时、扫描间隔、路径
```

- `libraries.conf` 保持现有单行语法向后兼容；新增可选的分节语法（`[movies]` + `key = value`）承载按库覆盖项，未覆盖项继承全局默认。
- 启动校验（`support/validation.py`）改为校验 `Settings` 对象，并新增**输入目录重叠检测**（一个库的 input 是另一个库 input 的祖先时报错），这是“媒体库边界”条目的前置条件。
- 过渡期：保留 `config` 模块作为默认值来源，组装根从中构造 `Settings`；逐模块改为接收参数，最后移除写入式 CLI 覆盖。

### 4.2 状态机与存储 schema v2

**状态集合**（在现有基础上扩展，不改语义）：

```
discovered? ─▶ pending_transcode ─▶ transcoded* ─▶ pending_postprocess ─▶ success
                    │                                                 └─▶ rejected
                    ├─▶ failed(retryable) ──到期──▶ pending_transcode
                    ├─▶ failed(permanent)
                    └─▶ not required
* transcoded 仅存在于内存，不落库（与现状一致）
```

**schema v2 新增列**（通过 `PRAGMA user_version` 做顺序迁移，迁移脚本放在 `adapters/sqlite_migrations.py`）：

| 列 | 用途 | 对应条目 |
|----|------|----------|
| `library` | 记录归属库名，恢复时不再靠路径前缀猜测 | 媒体库边界 |
| `source_size`, `source_mtime`（可选 `source_fingerprint`：首尾各 1 MiB 的哈希） | 源文件身份；不匹配时视为新文件，旧记录归档 | 源文件身份 |
| `attempts`, `next_retry_at`, `error_kind` | 重试策略 | 失败重试策略 |
| `updated_at` | 状态变更时间，便于 status 查询与排序 | 可观测性 |
| `events` 表（`filepath, ts, from, to, detail`） | 状态变更审计；可选，按天数清理 | 可观测性 / v3.0 |

**并发与锁**：

- 保留“单连接 + RLock”模型（简单、已验证），但所有读写统一经 `_execute()` / `_query()` 两个私有方法加锁，消除遗漏点。
- 状态转换用 `UPDATE ... WHERE filepath=? AND status=?`，根据 `rowcount` 判定是否抢占成功，作为调度去重的第二道防线（第一道仍是内存集合）。
- 新增批量接口：`known_paths(library) -> dict[path, (status, size, mtime)]`，供扫描一次性加载。

### 4.3 Scheduler：有界队列与资源预算

取代 `main_loop` 内的 `_pending` + 两个裸 `ThreadPoolExecutor`。

```
Scanner ──offer(job)──▶ ┌──────────── Scheduler ─────────────┐
Recovery ──offer(job)─▶ │ inflight: set[key]（去重）          │
                        │ transcode_q: 有界（max_queued）     │──▶ transcode workers × MAX_WORKERS
                        │ post_q: 有界                        │──▶ post workers × POST_PROCESS_WORKERS
                        │ budget: ResourceBudget              │
                        └─────────────────────────────────────┘
```

- **有界**：`offer()` 在队列满时返回 `False`，Scanner 停止本库本轮的提交（下一轮扫描会重新发现），不再一次性创建成千上万个 Future。
- **ResourceBudget**：统一核算 CPU 线程预算：`VMAF 并发 × VMAF 线程数 + 转码占用 ≤ 总核数`；启动时校验并给出告警或自动下调，取代当前 `VMAF_N_THREADS` 与 `POST_PROCESS_WORKERS` 各自为政。
- **优先级**：恢复任务 > 到期重试 > 新发现文件（简单的多队列轮询即可，不需要优先级堆）。
- **快照接口**：`snapshot() -> SchedulerStatus(active, queued, by_stage, started_at…)`，供日志、`status` 命令和未来 API 使用。
- 关停：`Scheduler.shutdown(drain=True)` 不再接收新任务，取消排队任务，通知 `ProcessRunner` 终止运行中的子进程（见 4.4）。

### 4.4 ProcessRunner：统一子进程管控

```python
class ProcessRunner(Protocol):
    def run(self, cmd: Sequence[str], *, timeout: float | None,
            on_stderr_line: Callable[[str], None] | None = None,
            cancel: CancelToken) -> ProcessResult: ...
```

- 唯一实现基于 `Popen`：登记 PID → 读取 stderr（可选逐行回调，用于转码进度）→ 轮询 `cancel` 与超时 → SIGTERM → 宽限期 → SIGKILL。
- `CancelToken` 由 Daemon 持有的单一 `threading.Event` 派生，**合并 P4 中的两套关停标志**。
- ffmpeg 转码、libvmaf 打分、ffprobe 探测全部改用它；`process_guard.kill_active_ffmpeg()` 退化为兜底。
- 结果对象携带 `cancelled: bool`，阶段据此返回 `Failure(kind=INTERRUPTED)`，状态机对 INTERRUPTED 不写终态（沿用现在“关停不标 failed”的语义）。

### 4.5 OutputStore：原子落盘协议

```
1. reserve(name)   在目标目录用 O_CREAT|O_EXCL 创建占位文件 <name>.mkv.reserving
                   冲突则换名（foo.1.mkv …），消除 _unique_path 的 TOCTOU
2. upload          复制 temp 产物到 <目标目录>/.<name>.mkv.partial-<suffix>
3. fsync           对 partial 文件及目录 fsync（NFS 上 best-effort）
4. verify          对 partial 文件做完整性检查（4.6）
5. commit          os.replace(partial, final)（同目录 rename，在 NFS/CIFS 上同样原子）
6. release         删除占位文件；记录状态
```

- 崩溃恢复：Recovery 扫描各库输出目录中的 `*.partial-*` / `*.reserving`，结合 history 判定删除或继续；与现有 `.work_*` 清理合并为一个“遗留工件清理器”。
- async 模式的“先落盘后打分”语义不变：commit 后立即 `transition(pending_postprocess)`，打分对象仍是库中文件。
- 跨文件系统时 step 2 是复制，同文件系统时可直接 rename，由 `OutputStore` 内部判定（`st_dev` 比较）。

### 4.6 完整性校验（verify 阶段）

新增 `domain/stages/verify.py`，在 assess 之前运行，失败归类为 `ErrorKind.OUTPUT_INVALID`：

- ffprobe 可读；存在且仅存在一个视频流；编码器与 `EncodingProfile.encoder` 一致。
- 位深 / 像素格式符合预期（如 `p010le` / main10）。
- 时长与源时长偏差在阈值内（复用已有的 probe 能力，替代被移除的帧数闸门，但只做粗校验）。
- 音频 / 字幕 / 附件流数量与转码命令映射结果一致。

### 4.7 RetryPolicy 与错误分类

```python
class ErrorKind(Enum):
    TRANSIENT       # 超时、NFS I/O 错误、设备忙 → 指数退避重试
    INPUT_CORRUPT   # 解码失败、probe 失败 → 不自动重试
    OUTPUT_INVALID  # verify 不通过 → 有限次重试（可能换降级路径）
    CONFIG          # 编码器不可用、路径无权限 → 不重试，启动/首次即告警
    INTERRUPTED     # 关停打断 → 不写终态
```

- 分类器集中在一处：基于异常类型 + ffmpeg stderr 特征（复用 `_summarize_error_stderr` 的已有知识）。
- `RetryPolicy(max_attempts, base_delay, max_delay)` 计算 `next_retry_at`；Scanner 每轮通过 `repo.due_retries(now)` 拉取到期项。
- `--retry-failed` 保留，语义变为“忽略 `next_retry_at` / 次数限制，立即重试所有 failed”。

### 4.8 Scanner 与扫描成本

- 每轮每库：`known = repo.known_paths(lib)` 一次查询 → walk → 内存比对（路径 + size + mtime），仅对“新出现 / 身份变化 / 到期重试”的文件构造 Job。
- `purge_orphan_records` 改为基于本轮 walk 结果做集合差，不再逐行 `os.path.exists()`；并降频（例如每 N 轮或每天一次）。
- 预留 `ChangeSource` 接口：默认实现是轮询 walk；v2.0 的 inotify 只需新增实现，不改 Scanner。

### 4.9 运行控制与可观测性

- CLI 子命令（`main.py` 无子命令时等价 `daemon`，保持兼容）：
  - `daemon` — 现有常驻模式
  - `once` — 扫描一轮、处理完队列后退出（cron / 调试）
  - `dry-run` — 扫描 + 筛选 + 打印将执行的 ffmpeg 命令，不写库、不转码
  - `status` — 读 history + 状态快照文件，输出各状态计数、活动任务、排队数
  - `failed [--kind X]` — 列出失败记录及错误分类、下次重试时间
- Daemon 定期把 `Scheduler.snapshot()` 原子写入 `data/status.json`，`status` 命令读取该文件（避免进程间通信）。v3.0 的 API 可直接复用同一份快照与 repository 查询。
- 日志：每个 Job 带固定前缀 `[lib/filename]`，阶段结束输出一行结构化耗时（transcode / verify / vmaf / commit），扫描轮输出 active / queued / due-retry 计数。

---

## 5. v1.7 条目 → 架构落点

| v1.7 条目 | 主要组件 | 阶段 |
|-----------|----------|------|
| VMAF 子进程可中断 | ProcessRunner、CancelToken（4.4） | A |
| History 访问和状态一致性 | transitions、JobRepository、schema v2（4.2） | B |
| 源文件身份与媒体库边界 | schema v2 身份列、Settings 重叠校验（4.1 / 4.2） | B |
| 失败重试策略重做 | ErrorKind、RetryPolicy（4.7） | B → D |
| 输出原子落盘与并发冲突保护 | OutputStore（4.5） | C |
| 转码产物完整性检查 | verify 阶段（4.6） | C |
| 有界任务队列与资源预算 | Scheduler、ResourceBudget（4.3） | D |
| 扫描成本控制 | Scanner 批量查询、purge 降频（4.8） | E |
| 按媒体库配置编码策略 | Settings / LibraryProfile（4.1） | A（结构）→ E（功能） |
| 运行控制与可观测性 | CLI 子命令、status 快照（4.9） | F |
| 故障注入和文档同步 | 各 port 的测试替身 + 故障注入用例 | 每阶段 + F 收尾 |
| 可选：ab-av1 智能码率 | 作为 `Encoder` 的另一实现 / EncodingProfile 策略 | v1.7 之后 |

---

## 6. 分阶段演进路径

每个阶段一个或多个 PR，完成标准统一为：CI 全绿、覆盖率不低于当前门槛、ROADMAP 与 SETUP 同步更新。

```
A 基础设施 ──▶ B 状态与存储 ──▶ C 落盘与校验 ──▶ D 调度与重试 ──▶ E 按库策略/扫描 ──▶ F 运维与可观测
     └──────────────(A 的 Settings 结构是 E 的前置)──────────────────────────▲
```

### Phase A — 基础设施（重构为主，行为不变 + VMAF 可中断）

1. 引入 frozen `Settings` / `LibraryProfile`，组装根构造并注入 `TranscodePipeline`；先迁移 `post_process` / `policy` / `transcoder` 读取的配置项。
2. 实现 `ProcessRunner` + `CancelToken`，迁移 ffmpeg → libvmaf → ffprobe；合并两套关停标志。
3. 把 `main_loop` 的闭包抽到 `app/daemon.py`（先原样搬迁，便于后续替换为 Scheduler）。
4. 测试：关停时 VMAF 在宽限期内退出；Settings 校验（含输入目录重叠）。

### Phase B — 状态与存储

1. `PRAGMA user_version` 迁移框架 + schema v2（新增列向后兼容，旧库自动迁移）。
2. `transitions.py` 合法转换表 + `repo.transition()` CAS 更新；阶段改为返回 `StageOutcome`，`mark_*` 散点收拢到编排层。
3. 源文件身份校验接入 `should_submit`；`library` 列用于恢复归属。
4. `ErrorKind` 分类器落地（先只记录，不改变重试行为）。
5. 测试：非法转换拒绝、并发 transition 竞争、迁移幂等、同路径换文件被识别为新任务。

### Phase C — 落盘与校验

1. `OutputStore`：名称预留 + partial + fsync + replace；替换 `_land_output` 与 `_unique_path`。
2. verify 阶段接入三种 vmaf 模式的公共前置段。
3. 遗留工件清理器合并 `.work_*` / `.partial-*` / `.reserving`。
4. 用 dataclass `PostProcessContext` 替换 `ctx` 字典与 `SimpleNamespace`。
5. 测试：同名并发提交不覆盖、commit 前崩溃不留半成品、verify 拦截损坏产物。

### Phase D — 调度与重试

1. `Scheduler`（有界队列、去重、优先级、snapshot）替换 `_pending` 与裸线程池。
2. `ResourceBudget` 启动校验与告警。
3. `RetryPolicy` 生效：`attempts` / `next_retry_at`，Scanner 拉取到期重试；调整 `--retry-failed` 语义并更新文档。
4. 测试：队列满时背压、关停时排队任务取消且下次启动可恢复、退避时间计算。

### Phase E — 按库策略与扫描成本

1. `libraries.conf` 分节语法 + 按库 `EncodingProfile` / `QualityPolicy` / `skip_codecs`。
2. Scanner 批量 `known_paths` + 集合差 purge + purge 降频。
3. 基准：在合成的 10 万条记录库上测量单轮扫描耗时，作为回归指标纳入 `test_benchmarks.py`。

### Phase F — 运维与可观测性

1. CLI 子命令 `once` / `dry-run` / `status` / `failed`。
2. `status.json` 快照、Job 级日志前缀与阶段耗时。
3. 故障注入套件：关停中断、VMAF 超时、输出冲突、源文件消失、存储写失败（基于 port 测试替身）。
4. 文档收尾：ROADMAP v1.7 勾选、SETUP 配置参考重写、README 架构段落更新。

---

## 7. v1.8 及之后的版本规划

v1.7 解决“稳不稳”。之后每个 minor 版本只做一个主题，条目控制在 8 个以内，没做完的条目按优先级顺延，不拖住整个版本发布。

### 7.1 版本总览

| 版本 | 主题 | 主要解决的问题 | 依赖 v1.7 的哪些阶段 |
|------|------|----------------|----------------------|
| **v1.8** | 编码决策前移 | 一个 reject 要先白跑最长 5 h 转码 + 10 h 打分才能得出结论；编码参数对所有内容一刀切；HDR 元数据没有明确的处理策略 | A（ProcessRunner）、C（verify）、E（按库 profile） |
| **v1.9** | 生态集成与安全运维 | 落盘后媒体服务器不感知；出错只能看日志；源文件删除不可撤销；与家庭使用时段、磁盘余量争资源 | D（Scheduler）、F（status 快照） |
| **v1.10** | 数据驱动与存量治理 | 阈值靠经验调整；历史 rejected 永远不再评估；history 只增不减 | B（schema v2）、F |
| **v1.x → v2/v3 决策点** | 远期路线重排 | 确定 Web/API 与 Go 移植的先后顺序 | — |

```
v1.7 稳定性 ──▶ v1.8 编码决策前移 ──▶ v1.9 集成与运维 ──▶ v1.10 数据与存量 ──▶ 决策点 ──▶ v3.0 API/Web（建议先行）
   A B C D E F      依赖 A/C/E              依赖 D/F               依赖 B/F                       └─▶ v2.0 Go（视决策点结论）
```

### 7.2 v1.8 — 编码决策前移（Pre-flight & Smart Encoding）

**动机**：目前质量与体积要等全片转码（甚至全片打分）后才知道。对注定被 reject 的文件，这些 GPU 时间全部浪费，而且同一文件不会自动换参数再试。v1.8 把决策提前到“转码之前”，并让参数随内容变化。

- [ ] **抽样预检（pre-flight）** — 正式转码前，从片头、片中、片尾截取 N 段（默认 3 × 20 s）用当前 profile 编码并打 VMAF，预测 ratio 与分数；明显会被 reject 的文件直接记为 `rejected(predicted)`，不进入全片转码。预测值写入 history，用于和全片结果对比、校准误差
- [ ] **目标 VMAF 参数搜索** — 把 v1.7 中“可选：ab-av1”条目正式纳入：在抽样片段上对 QSV `quality`（ICQ）做二分搜索，找到满足目标 VMAF（默认 93）的最大压缩参数。实现方式二选一，在版本启动时评估：① 作为 `Encoder` 端口的一个实现调用 ab-av1（需验证其对 `av1_qsv` 的支持）；② 复用抽样预检的基础设施自行实现（无外部依赖，推荐）
- [ ] **分辨率分档参数** — `EncodingProfile` 支持按分辨率分档（≤720p / 1080p / 2160p）设置 `quality` 与 look-ahead，替代当前全局单一值
- [ ] **HDR / 色彩元数据保真** — 明确并实现策略：HDR10 静态元数据（mastering display / MaxCLL）与 HLG 透传；Dolby Vision 按库配置 `skip`（默认，不转码）或 `strip`（保留 HDR10 基础层）；`color_primaries / transfer / matrix` 透传。verify 阶段（4.6）同步校验这些字段
- [ ] **编码 profile 版本化** — 每个 `EncodingProfile` 内容计算出 `profile_id`（参数哈希）写入 history。这是 v1.10 存量重评估的前提，也让“这个文件是用什么参数压的”可追溯
- [ ] **转码参数可回放** — history 记录实际执行的 ffmpeg 命令摘要（含是否走了 SW 降级），便于复现和排查

**退出标准**：在一个有代表性的样本库（≥ 50 个文件，覆盖动画 / 真人 / 高噪点 / HDR）上，预检预测的 verdict 与全片结果一致率 ≥ 90%；被 reject 的文件平均耗费的转码时长下降 ≥ 70%。

**架构影响**：新增 `domain/stages/preflight.py`，位于 verify 之前、transcode 之前；`Encoder` 端口增加 `encode_segment()`；`QualityPolicy` 增加 `target_vmaf` 与 `preflight` 配置节。

### 7.3 v1.9 — 生态集成与安全运维（Integration & Safe Operations）

**动机**：这是一个跑在家庭 NAS 上的守护进程，最终消费者是 Jellyfin 等媒体服务器和家里的人。v1.9 让它“融入环境”：对外有通知和指标，对内尊重使用时段和磁盘余量，删除源文件可以撤销。

- [ ] **源文件回收站** — ACCEPT 后源文件移入每个库的 `trash` 目录（默认保留 14 天，然后自动清理），替代直接 `unlink`；`--no-trash` 恢复旧行为。这是成本最低、收益最高的安全网，优先实现
- [ ] **媒体服务器联动** — 落盘后调用 Jellyfin / Emby / Plex 的 API 刷新对应库（按库配置 URL 与 token）；可选：通过 Jellyfin Sessions API 检测源文件是否正在被播放，播放中则延迟替换与删源
- [ ] **通知** — 基于状态变更事件（4.2 `events`）推送 webhook / ntfy / Telegram，支持 failed、连续 reject、每日摘要三类，可按类别开关
- [ ] **指标导出** — 用标准库 `http.server` 提供 `/metrics`（Prometheus 文本格式），或写 node_exporter textfile；内容复用 `status.json`：队列长度、各状态计数、节省空间、各阶段耗时分布
- [ ] **运行时间窗与资源感知** — 按时间窗调度（例如只在 01:00–08:00 启动新的转码任务，运行中的任务不打断）；cache / output 所在磁盘余量低于阈值时暂停派发新任务
- [ ] **部署形态** — 提供 systemd unit（`Type=notify` + watchdog，用标准库 socket 写 `NOTIFY_SOCKET`，不引入依赖）和 Docker 镜像（挂载 `/dev/dri`、render 组、自编译 ffmpeg 分层构建）
- [ ] **多 GPU / 设备选择** — 按库或按 worker 指定 `/dev/dri/renderD*`，为有核显 + Arc 独显的机器分流

**退出标准**：在全新机器上按文档用 Docker 或 systemd 部署 ≤ 15 分钟；误删场景（ACCEPT 后发现画质问题）能在保留期内一条命令恢复源文件。

**架构影响**：新增 `ports.Notifier` / `ports.MediaServer` 与对应 adapters；Scheduler 增加 `DispatchGate`（时间窗、磁盘余量、媒体服务器播放状态的组合判定）；`OutputStore` 增加 `trash()`。

### 7.4 v1.10 — 数据驱动与存量治理（Data & Backlog）

**动机**：v1.7–v1.9 积累了结构化数据（profile_id、预测值、错误分类、事件）。v1.10 把这些数据变成决策：阈值怎么调、哪些旧文件值得再试、history 怎么瘦身。

- [ ] **统计报表 CLI** — `report [--library X] [--since 30d]`：节省空间、accept / reject / failed 比例、按源编码 / 分辨率 / profile 分组的平均 VMAF 与 ratio；输出 Markdown 或 JSON
- [ ] **`ai_analyze` 改用结构化输入** — 把 `report --json` 的结果作为 LLM 的输入，替代直接读取原始日志，降低 token 成本并提高结论稳定性
- [ ] **阈值回放模拟** — `simulate --vmaf-low 82 --ratio-max 0.9`：基于历史分数和 ratio 计算“如果当时用这组阈值，结果会怎样”，作为调参依据（也是 v3.0 VMAF 浏览器的命令行前身）
- [ ] **rejected 存量重评估** — 当 `profile_id` 或阈值变化后，按条件（例如“旧 profile + VMAF 处于临界区间”）把 rejected 记录批量重新入队；只针对源文件仍在的记录（已 ACCEPT 删源的文件不能从产物再压一遍，否则会叠加画质损失）
- [ ] **history 归档与维护** — 终态记录超过 N 天转入 `files_archive`；`events` 按保留期清理；定期 `VACUUM INTO` 备份
- [ ] **配置变更审计** — 启动时把生效的 `Settings` 摘要与上次运行对比并记录差异，便于把结果变化和配置变化对应起来

**退出标准**：能用 `simulate` 解释一次阈值调整带来的 accept 率变化；rejected 重评估在样本库上至少找回一部分在新 profile 下可接受的文件，且不重复处理已 success 的文件。

### 7.5 v1.x 收尾：v2.0 / v3.0 决策点

v1.10 发布后，用一个短周期评估远期路线，**建议把 v3.0（API / Web）排在 v2.0（Go 移植）之前**：

| 维度 | v3.0 API / Web | v2.0 Go 移植 |
|------|----------------|--------------|
| 用户可感知收益 | 高：可视化浏览 history、手动触发、阈值调参 | 低：瓶颈在 ffmpeg 子进程，Go 带来的吞吐提升有限 |
| 对 v1.7–v1.10 的复用 | 高：直接复用 `status.json`、`events`、report / simulate 查询 | 中：只复用 schema 与配置契约 |
| 风险 | 低：只读 API + commands 表，不改核心流程 | 高：重写核心逻辑，需要完整回归 |

**v2.0 的启动条件**（满足其一再立项）：需要单文件二进制分发（例如面向其他用户的 Docker 镜像要求极小体积）；Python 运行时的内存或启动开销成为实际问题；需要原生 inotify 而 Python 方案不满足。否则 v2.0 保持暂缓。

### 7.6 未排期候选池

以下想法目前价值或成本不明确，暂不进入任何版本，定期回顾：

- **其他编码后端**：VAAPI / NVENC / SVT-AV1 软件编码，作为 `Encoder` 端口的新实现（架构已预留，按需求接入）
- **音频策略扩展**：多声道转 Opus、无损音轨（TrueHD / DTS-HD）保留或降级的按库策略
- **字幕处理**：PGS 图形字幕 OCR 转 SRT、外挂字幕合并
- **多节点分布式转码**：多台机器共享任务队列；与“单进程、单写者”约束冲突，除非需求明确，否则不做
- **内容分类**：自动识别动画 / 真人 / 高噪点并选择 profile（可在 v1.8 抽样预检的数据上训练简单规则）

---

## 8. 面向远期版本的预留

**v2.0 Go 移植**

- 以 **SQLite schema + 状态转换表 + `libraries.conf` 语法** 作为跨语言契约，Go 版可读写同一份数据并逐步替换。
- `ports/` 的接口划分直接对应 Go interface；Scheduler 的有界队列 / 预算模型可映射为 channel + semaphore。
- `ChangeSource` 接口为 inotify 预留入口。

**v3.0 Web UI / API**

- API 进程**只读** history 与 `status.json`；写操作（手动触发、重试）通过 history 中的“请求”列或独立 `commands` 表下发，由 Daemon 消费，保持单写者模型，不需要进程内耦合。
- `events` 表天然提供进度时间线；实时进度可由 Daemon 把 `on_stderr_line` 解析出的进度写入快照，API 以 SSE/WebSocket 轮询推送。
- VMAF 分数浏览器直接读取 `data/vmaf_logs/` 与 history 关联字段，无需改动核心流程。

---

## 9. 决策记录（ADR-lite）

| 决策 | 选择 | 理由 |
|------|------|------|
| 并发模型 | 继续使用线程 + 子进程 | 重活都在 ffmpeg 子进程里，GIL 不是瓶颈；asyncio 改造收益低、风险高 |
| 持久化 | 继续使用 SQLite 单连接 + RLock | 写入频率低（每文件个位数次），连接池不必要；用 CAS 更新保证正确性 |
| 状态机 | 显式转换表 + CAS，不引入状态机库 | 状态少（6 个），表驱动足够且零依赖 |
| 包结构迁移 | 渐进迁移，每次移动伴随行为测试 | 避免大爆炸式重命名打断 v1.7 功能开发 |
| 配置格式 | 扩展 `libraries.conf` 分节语法，不引入 YAML/TOML 依赖 | 保持零运行时依赖；`configparser` 已在标准库中 |
| 原子落盘 | 同目录 partial + `os.replace` | 在本地盘、NFS、CIFS 上都是原子 rename；不依赖 `copy2`/`chmod`（NFS 上可能 EPERM） |

---

## 10. 风险与待定问题

1. **NFS/CIFS 上的 `O_EXCL` 与 `fsync` 语义**：NFSv3 的 `O_EXCL` 可靠性依赖服务器实现，需要在真实环境验证；不可靠时退化为“DB 预留名称 + rename 时校验目标不存在”。
2. **源指纹成本**：对网络存储上的大文件读取首尾数据有 I/O 成本；默认只用 size+mtime，指纹设为可选。
3. **schema 迁移的回退**：迁移前自动备份 `history.db`（`VACUUM INTO`），并在 SETUP 中写明回退步骤。
4. **verify 的误杀**：时长阈值对 VFR 源可能过严，先以“只告警”模式上线一个版本再转为硬性拦截。
5. **Phase A 改动面大**：Settings 注入会触及大部分测试的 fixture，需要先在 `conftest.py` 提供 `settings_factory`，与现有 factory fixtures 风格保持一致。
6. **待定**：`events` 表是否在 v1.7 内落地，还是推迟到 v3.0 启动时再加——取决于 `status` / `failed` 命令是否需要历史时间线。
