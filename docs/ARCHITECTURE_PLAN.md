# Architectural Plan / 架构规划

> 本文以 [ROADMAP.md](ROADMAP.md) 为输入，回答 v1.7 各条目“结构上怎么做、做到什么程度、按什么顺序做”。
> 项目定位与复杂度准入规则见仓库根目录 [AGENTS.md](../AGENTS.md)，本文所有取舍都以它为准。
>
> 修订记录：
> - 2026-10 初版（`2785857`）：按通用产品标准规划，含五层架构、Scheduler、schema 迁移框架、v1.8–v1.10 版本规划等。
> - **2026-11-13 收缩版（本文）**：回到“个人媒体库瘦身工具”的定位，砍掉不必要的抽象；v1.7 之后的内容全部改为待定候选池。初版可在 git 历史中查看。

---

## 1. 原则

1. **保护媒体库 > 节省 GPU 时间 > 其他。** v1.7 里直接服务前两项的条目是必需项，其余是可选项或学习项。
2. **保持现有包结构**（`application` / `ingest` / `media` / `runtime` / `storage` / `policy` / `support`，外加已落地的 `domain/settings.py`），不引入 ports/adapters 分层，不为替换后端预先抽象。
3. **每一项都写清楚它解决的问题**，并标注类别：
   - **必需**：对应真实故障或可复现的 bug
   - **学习项**：作者有意练习的工业做法，范围收住
   - **可选**：有价值但不紧迫，做不做都不影响 v1.7 发布
   - **不做**：暂不进入计划
4. **能加列就不加表，能用进程内锁就不用文件协议**：本项目由 `fcntl` 锁保证单实例，进程内的线程互斥已经足够。

---

## 2. 现状（v1.6 + v1.7 已完成部分）

### 2.1 结构与数据流

```
main.py  ── CLI 解析 → config 全局量 →（逐步改为）冻结 Settings → main_loop()
   ├─ 启动恢复：pending_postprocess 直接打分 / pending_transcode 重入队 / --retry-failed
   └─ 扫描循环：temp 清理 → purge 孤儿记录 → walk → should_submit → 提交转码池
application/  TranscodePipeline + TranscodeJob
   ├─ transcode_stage      ffmpeg 编码 + QSV 降级重试 → cache 下的 temp 产物
   └─ post_process_stage   off / sync / async：ratio 门 → 打分（可选）→ 落盘 → 终态
domain/settings.py         冻结的 Settings（v1.7 Phase A 新增）
ingest/ media/ storage/ runtime/ policy/ support/   见 README
```

### 2.2 状态机

```
(无记录) ─扫描─▶ pending_transcode ─编码成功─▶ [内存中的 temp 产物]
                     │                          ├─ off/sync：打分(可选) → 落盘 → success / rejected
                     │ 失败                      └─ async：落盘 → pending_postprocess → 打分 → success / rejected
                     ▼
                  failed
codec 命中 SKIP_CODECS ─▶ not required
```

“超标必拒”（ratio > `ACCEPT_RATIO_MAX`）已经只在后处理的第一道门执行，转码阶段不做任何质量裁决。

### 2.3 问题清单与处理方式

| # | 问题 | 代码证据 | 处理 |
|---|------|----------|------|
| P1 | CLI 直接改写 `config` 全局量，业务模块到处读 `config.XXX` | `main._apply_cli_overrides` | **学习项，进行中**：Phase A，冻结 `Settings` 显式传递 |
| P2 | 调度逻辑写在 `main_loop` 闭包里，难以单测 | `main.py` 的 `process_one` / `requeue_entry` | 可选：settings-04 顺带整理，不新建 Scheduler |
| P3 | VMAF 子进程关停时要等满超时 | 原 `ffmpeg_vmaf_scorer` 用 `subprocess.run` | **已修复**（`8c66d04`）；ffprobe 帧数探测（超时 600 s）仍未纳入守卫，可选 |
| P4 | 两套关停标志（`main._running` 与 `shutdown._shutting_down`） | `main.signal_handler` | 必需（小）：合并为 `runtime.shutdown` 一处 |
| P5 | 转码失败的 `_fail()` 不检查关停，正常关机会把任务记成 `failed` | `transcoder._fail` | **必需（bug）**：加 `is_shutting_down()` 守卫 |
| P6 | 落盘直接 `copyfile` 到最终路径；选名与写入之间有竞争 | `post_process._land_output`、`path_utils._unique_path` | **必需**：Phase C |
| P7 | 落盘前只检查产物文件存在 | `transcode_stage` 末尾 | **必需（轻量）**：Phase C |
| P8 | 身份只看路径：同路径换了新文件会被当成已处理 | `files.filepath PRIMARY KEY` | **必需**：Phase B，加 size/mtime 两列 |
| P9 | 每个文件一次 history 查询；每轮全量 `exists()` 清孤儿 | `filter._history_allows`、`purge_orphan_records` | **先测量**，慢了再改 |
| P10 | 后处理上下文用 `ctx` 字典和 `SimpleNamespace` | `post_process._resolve_output` | 可选：改到哪里顺手换成 dataclass |
| P11 | `failed` 不区分阶段：损坏源文件每次 `--retry-failed` 都重试 | `files.error` 只有文本 | **必需**：Phase B，加 `failed_stage` 列 |

---

## 3. 结构上不做的事

以下内容在初版里出现过，**明确不做**（理由见 §8）：

- interfaces / app / domain / ports / adapters 五层结构和 Protocol 端口
- `Scheduler` 类、优先级队列、`ResourceBudget`
- `ProcessRunner` / `CancelToken` 抽象（转码和 VMAF 已经都是 `Popen` + 进程守卫，够用）
- `OutputStore` 类、`O_EXCL` 预留文件、`fsync` 协议
- schema 迁移框架、`events` 审计表、换自增主键、版本化归档、内容指纹
- 新的失败状态值、错误类型体系、自动退避重试
- `status.json` 快照文件、`dry-run`、HTTP 接口

---

## 4. v1.7 各项方案

### 4.1 配置：冻结 Settings（Phase A，学习项）

目标：配置“加载 → 一次构造 → 显式传递”，业务模块不再 `import src.config`。分 5 个任务（`.context/backlog/v1.7-settings-0*.md`）。

**Phase A 落地说明（2026-11-12，settings-01）**

- 位置：`src/domain/settings.py`。
- 当前为扁平结构：`Settings` 持有全局的 `encoding` / `quality`，各 `LibraryProfile` 仅含路径。
  已落地类：`Settings`、`PathsSettings`、`RuntimeSettings`、`EncodingProfile`、`QualityPolicy`、`LibraryProfile`；
  字段名与 `config.py` 常量一一对应（如 `vmaf_accept_high` 对应 `VMAF_ACCEPT_HIGH`）。
- VMAF 模式常量（`VMAF_MODE_OFF/SYNC/ASYNC`）定义于 `src/domain/settings.py`，`config.py` 重新导出。
- v1.6 起无调用方的废弃常量已从 `config.py` 和文档中删除。

**范围边界**

- 保持扁平结构。按库覆盖编码 / 质量参数**不在 v1.7 做**（见 §7）；等真有某个库需要不同参数时，再把 `encoding` / `quality` 挪进 `LibraryProfile`，结构上已经留了位置。
- `libraries.conf` 语法不变。
- settings 系列是纯重构，规范里明确禁止改变行为。合并两套关停标志（P4）、转码 `_fail` 关停守卫（P5）、**输入目录重叠检测**（一个库的 input 是另一个库 input 的祖先时报错，避免恢复任务匹配到错误的库）这三件行为改动单独放在 `backlog/v1.7-small-fixes.md`。

### 4.2 存储：只加列（Phase B，必需）

一次性给 `files` 表加 4 列，用 `ALTER TABLE ... ADD COLUMN`，不重建表、不换主键、不加新状态值：

| 列 | 用途 |
|----|------|
| `source_size INTEGER` | 源文件身份 |
| `source_mtime REAL` | 源文件身份 |
| `failed_stage TEXT` | `source` / `transcode` / `postprocess`，仅 `failed` 行有值 |
| `attempts INTEGER DEFAULT 0` | 失败次数 |

- **迁移**：启动时用 `PRAGMA table_info(files)` 检查缺哪列就补哪列，幂等。迁移前把 `history.db` 复制一份 `.bak`。
- **源文件身份**：`mark_pending_transcode` 时记录 size / mtime。扫描遇到终态记录时比较，size 不同或 mtime 差超过 2 s（容忍 NFS/FAT 的时间精度）就当作新文件：日志记下旧记录摘要，删除旧行，重新提交。旧库里这两列为空的行，首次扫描时静默回填，不触发重转。
- **失败分阶段**：probe 失败记 `source`，编码失败记 `transcode`，后处理异常记 `postprocess`。`--retry-failed` 跳过 `source`，并且只重试 `attempts < 3` 的行。
- **后处理失败免重转**：只有 async 已落盘的场景才有产物可复用，而现有 CHECK 约束要求 `failed` 行 `output_path` 为空，要支持就得重建表。**本期不做**，后处理失败仍然重新转码。
- **加锁**：所有读写都经过 `History` 的 `RLock`，补一个多线程并发读写的测试即可，不改连接模型。

对应 `.context/TASK_A.md`（失败分类）与 `.context/TASK_B.md`（源文件身份）。两者共用这一次加列，建议合并成一个任务，或者 B 紧接 A 做。

### 4.3 落盘与校验（Phase C，必需）

**原子落盘**（替换 `_land_output` 的写入部分）：

1. 在目标目录复制到 `.<最终文件名>.partial`
2. `os.replace(partial, final)`。同目录 rename，在本地盘和 NFS/CIFS 上都是原子的
3. 选名（`_unique_path`）和 `os.replace` 放在同一把模块级 `threading.Lock` 里，两个任务不会抢到同一个名字（单实例已由 `fcntl` 锁保证）
4. 复制在锁外进行，锁内只做选名和 rename，不阻塞其他任务的复制
5. 扫描轮的遗留清理顺带删除输出目录中超过一天的 `.*.partial`

**落盘前校验**（新增一个函数，三种 VMAF 模式在 `_resolve_output` 里共用）：

- ffprobe 能读；恰好一个视频流，编码与配置的编码器一致
- 音频 / 字幕 / 附件流数量与源文件一致（按现有的流映射规则）
- 时长与源时长偏差 > 2 % 时：第一个版本**只告警**，观察一段时间没有误报再改成拒绝
- 校验失败记 `failed`（`failed_stage='transcode'`），清理 temp

### 4.4 运维小件（Phase D，可选）

- `--once`：扫描一轮，等队列跑完后退出（方便调试和 cron）
- `--status`：只读打开 history，打印各状态计数和失败列表（含 `failed_stage`、`attempts`、错误摘要）后退出；不抢单实例锁
- 每轮扫描日志补一行“运行中 N / 排队 M”
- 启动时如果 `VMAF 线程数 × POST_PROCESS_WORKERS` 超过 CPU 核数，打一条告警
- 排队上限：扫描时如果排队数已超过 `MAX_QUEUED`（默认 500），本轮停止提交，下一轮会重新发现

---

## 5. v1.7 ROADMAP 条目对照

| ROADMAP 条目 | 结论 | 落点 |
|--------------|------|------|
| VMAF 子进程可中断 | ✔ 已完成（`8c66d04`）；ffprobe 帧数探测可选 | — |
| 输出原子落盘与并发冲突保护 | 必需 | §4.3，Phase C |
| 转码产物完整性检查 | 必需（轻量） | §4.3，Phase C |
| 失败重试策略重做 | 必需，收缩为“分阶段 + 次数上限”，不做退避 | §4.2，Phase B |
| 有界任务队列与资源预算 | 收缩为排队上限 + 线程数告警 | §4.4，Phase D |
| History 访问和状态一致性 | 必需（小）：统一加锁 + 并发测试 | §4.2，Phase B |
| 源文件身份与媒体库边界 | 必需：size/mtime + 输入目录重叠检测 | §4.2 Phase B、small-fixes |
| 扫描成本控制 | 先测量：真实库单轮扫描超过 1 分钟再处理 | — |
| 按媒体库配置编码策略 | 移出 v1.7，进候选池 | §7 |
| 运行控制与可观测性 | 收缩为 `--once` / `--status` / 一行日志 | §4.4，Phase D |
| 故障注入和文档同步 | 不单独做套件：每个任务自带对应的故障测试 | 各阶段 |
| 可选：智能码率（ab-av1） | 移出 v1.7，进候选池 | §7 |

---

## 6. 阶段与任务

| 阶段 | 内容 | 任务规范 | 状态 |
|------|------|----------|------|
| **A 配置与关停** | VMAF 可中断 | `archive/v1.7-vmaf-interrupt.md` | ✔ |
| | Settings 模型 | `archive/v1.7-settings-01-model.md` | ✔ |
| | 后处理 / 编码 / 扫描与 main / 清理 | `backlog/v1.7-settings-02…05` | 待做 |
| | 关停守卫（P5）、合并关停标志（P4）、输入目录重叠检测 | `backlog/v1.7-small-fixes.md` | 待做 |
| **B 存储** | 加 4 列、失败分阶段、源文件身份、并发测试 | `TASK_A.md` + `TASK_B.md`（按 §4.2 收缩） | 待做 |
| **C 落盘安全** | 原子落盘、落盘前校验 | 待写 | — |
| **D 运维小件** | `--once`、`--status`、日志、排队上限 | 待写 | 可选 |

A、B、C 之间没有硬依赖：B 和 C 也可以在 A 完成前开始，只是要按当时的取配置方式写。完成标准统一为：CI 全绿，ROADMAP 和 SETUP 同步更新。

---

## 7. v1.7 之后：候选池（全部待定）

不分配版本号，做到时再评估。括号里是目前的初步判断：

- **源文件回收站**：ACCEPT 后把源文件移进 `trash` 目录，保留 N 天（改动小、收益大，建议最先考虑）
- **HDR / 色彩元数据**：目前代码**没有任何 HDR 处理**，需要先确认现有产物是否丢了元数据，再决定透传方案；Dolby Vision 可能需要按库跳过
- **抽样预检 / 目标 VMAF 参数搜索**（含 ab-av1）：转码前抽几段编码打分，预测结果，减少注定被 reject 的全片转码
- **按分辨率分档参数**、**按库编码 / 质量策略**
- **媒体服务器刷新**：落盘后通知 Jellyfin 刷新媒体库
- **失败通知**（webhook / ntfy）
- **统计报表 / 阈值回放**：用 history 里的分数评估调整阈值的效果
- **rejected 重评估**：参数或阈值改变后，重新入队源文件仍在的临界 rejected 记录
- **systemd unit**

v2.0（Go 移植）与 v3.0（Web UI / API）维持 ROADMAP 原状，不在本文规划。

---

## 8. 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 包结构 | 保持现有结构，不引入分层 / 端口 | 4000 行左右的单人项目，分层带来的间接性大于收益 |
| 配置 | 冻结 `Settings` 显式传递 | 学习项；同时消除“同一任务的决策来自两份配置”的问题 |
| 并发互斥 | 进程内 `threading.Lock` | `fcntl` 已保证单实例，不需要跨进程协议 |
| 原子落盘 | 同目录 `.partial` + `os.replace` | 本地盘、NFS、CIFS 上都是原子 rename；不用 `copy2` / `chmod`（NFS 上可能 EPERM） |
| 存储演进 | 只 `ADD COLUMN`，`PRAGMA table_info` 判断 | 不需要迁移框架；不重建表就不会碰 CHECK 约束 |
| 失败分类 | `failed_stage` 一列 | 比新增状态值改动小，CHECK 约束不变 |
| 重试 | 只在 `--retry-failed` 启动时，次数上限 3 | 自动退避此前已废弃，个人工具手动触发足够 |
| 并发模型 | 线程 + ffmpeg 子进程 | 重活都在子进程里，GIL 不是瓶颈 |
| 持久化 | SQLite 单连接 + `RLock` | 每个文件只有个位数次写入，不需要连接池 |

---

## 9. 风险

1. **mtime 被外部工具改动**：源文件保留（`KEEP_SOURCE`）时，如果工具只改了 mtime，会触发重转，并在输出目录产生 `foo.1.mkv`。缓解：身份变化日志写清楚新旧 size/mtime；重转前如果旧记录的 `output_path` 仍存在，打告警。
2. **校验误杀**：VFR 源的时长偏差可能超出阈值。所以第一个版本只告警。
3. **加列迁移**：旧版本程序打开新库不受影响（只是多了列）；迁移前自动备份，回退时用 `.bak` 覆盖即可。
4. **`.partial` 残留**：如果在复制中途断电，会在输出目录留下隐藏文件。由扫描轮按时间清理；Jellyfin 默认忽略点开头的文件。
