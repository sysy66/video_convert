# Test Quality Improvement Plan / 测试质量改进计划

## Phase 1 — Infrastructure (foundation)

| # | Task | Why |
|---|------|-----|
| 1 | **Add CI** — GitHub Actions workflow running `pytest` on push / PR | Core gap — tests only run locally |
| 2 | **Configure coverage** — `[tool.coverage]` in `pyproject.toml`, set threshold (e.g. > 80%) | No visibility into untested code |
| 3 | **Add static analysis** — `ruff` (lint + format), check in CI | Zero code-quality tooling |
| 4 | **Declare test dependencies** — `[project.optional-dependencies] test =` in `pyproject.toml` | Dependencies currently unpinned / undeclared |

## Phase 2 — Test-base refactor (medium priority)

| # | Task | Why |
|---|------|-----|
| 5 | **Unify test style** — migrate `test_filter.py` / `test_walker.py` from `unittest.TestCase` to plain pytest | Inconsistent styles; plain pytest is cleaner & the project norm |
| 6 | **Turn conftest helpers into real fixtures** — decorate `make_history` etc. with `@pytest.fixture` so they can be injected | Reduces boilerplate; idiomatic pytest |
| 7 | **Add pytest markers** — `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.slow` | Enables selective runs; prepares for integration tests |
| 8 | **Test `_normalize_libraries` (config.py)** — currently zero coverage | Obvious gap in config parsing |

## Phase 3 — Fill unit-test gaps (high impact)

| # | Task | Why |
|---|------|-----|
| 9 | **Test uncovered helpers in `transcoder.py`:** `kill_active_ffmpeg`, `register_ffmpeg_pid`, `unregister_ffmpeg_pid`, `mark_stale_started_as_failed`, `_parse_progress_time`, `_parse_progress_fps`, `_unique_path`, `_format_size`, `_random_suffix`, `_rmtree_robust` | transcoder.py is the largest module (661 lines); many public APIs & complex helpers untested |
| 10 | **Test `main()` entry point** — lock file, signal registration, exception handling, shutdown flow | Main entry point completely untested |
| 11 | **Test more CLI flags** — currently 5 of 23 flags tested | CLI is the primary user interface |
| 12 | **Test `ai_analyze.py`** — HTTP request/response handling & file I/O | Only module with zero coverage (66 lines) |

## Phase 4 — Integration tests (highest value)

| # | Task | Why |
|---|------|-----|
| 13 | **Create small synthetic test videos** — generate with ffmpeg (1s, 64×64, test pattern), store in `tests/fixtures/` | Enables real ffmpeg/ffprobe calls without external dependencies |
| 14 | **Add ffprobe integration tests** — call `get_video_codec`, `get_streams_info` with real binary | Verify parsing logic against real ffprobe output |
| 15 | **Add end-to-end transcode test** — test `transcode()` with small synthetic video: encode to H.264/HEVC/AV1, verify output exists, duration correct, bitrate etc. | Validates real pipeline: ffmpeg → transcode → ffprobe verification |
| 16 | **Mark slow tests** — all real-ffmpeg tests get `@pytest.mark.slow`, excluded by default | Keep `pytest` fast; `pytest --run-slow` opt-in |
| 17 | **Test VMAF with synthetic video** — `compute_vmaf` calls real ffmpeg + VMAF on real encoded output | Validate VMAF pipeline integrity |

## Phase 5 — Regression & performance (nice-to-have)

| # | Task | Why |
|---|------|-----|
| 18 | **Add regression tests for key functions** — hash current real outputs to detect unexpected changes | Lock down behaviour |
| 19 | **Add performance benchmarks** — `pytest-benchmark` for encode speed, VMAF computation time | Detect performance regressions |
| 20 | **Cross-environment testing** — CI with multiple Python versions (3.10, 3.11, 3.12) | Ensure compatibility |

---

## Timeline estimate

| Phase | Est. effort | Impact |
|-------|-------------|--------|
| **Phase 1** (infrastructure) | ~1 h | High — automates everything |
| **Phase 2** (refactor) | ~1 h | Medium — cleaner code |
| **Phase 3** (fill gaps) | ~2 h | High — more coverage |
| **Phase 4** (integration) | ~3 h | **Highest** — biggest value |
| **Phase 5** (regression/perf) | ~1 h | Nice-to-have |
| **Total** | **~8 h** | Incremental, prioritised |

---

## Status

<!-- Update status as work progresses -->
- Phase 1: ✅ completed (2026-08-02)
  - CI: `.forgejo/workflows/test.yml` — push 触发，Forgejo Actions 全绿（pytest 133 passed + coverage 69.84% + ruff check/format）
  - Runner: `test-runner` → `docker://catthehacker/ubuntu:act-latest`
  - coverage: `fail_under = 69`（基线 69.84%）
  - ruff: `select = [E, F, I, UP, B]`，line-length 100，全仓已格式化并修复 29 处违规
  - 依赖: `[project.optional-dependencies] test = [pytest, pytest-cov, ruff]`
  - 踩坑记录: PEP 668（`--break-system-packages`）、`import main`（pytest `pythonpath=["."]`）、act-latest 需内置 node
- Phase 2: ✅ completed (2026-08-02)
  - Task 5: test_filter/test_walker 迁移到纯 pytest（unittest.TestCase → 原生 assert + tmp_path fixture）
  - Task 6: conftest 的 make_history/make_video_file/make_video_tree 转为 factory fixtures；test_history.py 14 处样板接入
  - Task 7: 注册 unit/integration/slow markers + `--strict-markers`；全部 140 用例标注 unit
  - Task 8: 新增 test_config.py（7 用例），src/config.py 覆盖 0 → 100%，总覆盖 69.84 → 70.09%
- Phase 3: ✅ completed (2026-08-02)
  - Task 9: 新增 test_transcoder_helpers.py（33 用例），transcoder.py 52% → 73%
  - Task 10: test_main.py 覆盖 main() 入口 lock/信号/异常/shutdown，main.py 66% → 78%
  - Task 11: CLI flags 全量覆盖（+15 用例，含 --library 三种形态），main.py → 91%
  - Task 12: ai_analyze.py 函数式重构 + 14 用例，覆盖率 0 → 98%
  - 总覆盖率 70.09 → 82%（208 passed）
- Phase 4: ✅ completed (2026-08-02)
  - Task 13: 6 个合成测试视频入仓（`tests/fixtures/`，320×240/1s/10fps testsrc + `generate_fixtures.sh`）
    - 分辨率选 320×240：QSV 最小编码分辨率，64×64 实测被 hevc_qsv/av1_qsv 拒绝
    - 覆盖 h264 / h264+aac / hevc 8bit / hevc10bit(main10) / mpeg4(avi) / vp9(webm)
  - Task 14: test_probe_integration.py（14 用例）— 真实 ffprobe 验证 codec/pix_fmt/duration/streams
  - Task 15: test_transcode_e2e.py（3 用例）— 真实 QSV：hevc_qsv 全流程通过（mark_done/输出/替换/清理/duration）
    - av1_qsv 本机 Iris Xe 无 AV1 编码硬件 → stderr 检测 "unsupported" 自动 skip
  - Task 16: `--run-slow` 机制 — conftest `pytest_addoption` + `pytest_collection_modifyitems`，
    未启用自动 skip；CI 零改动（选项 A：QSV/VMAF 用例在 CI 全部 skip）
  - Task 17: test_vmaf_integration.py（3 用例）— 真实 libvmaf：分数区间、json log、同源 >95
    - 踩坑：2024 johnvansickle 静态版有 scale2ref 断言 bug（`best_input >= 0`，
      fftools/ffmpeg_filter.c:2075），VMAF 测试优先用 /opt/ffmpeg-qsv-vmaf 自编译统一版
  - 验证：`pytest --run-slow` 226 passed + 1 skipped；默认 `pytest` 208 passed + 19 skipped
- Phase 5: ✅ completed (2026-08-02)
  - Task 18: test_regression.py — 6 个 fixtures SHA256 锁定（意外修改/重新生成即失败）+ 同参数两次重编码输出逐字节一致
  - Task 19: pytest-benchmark — test_benchmarks.py（编码速度 67.7ms / VMAF 18.8ms，320×240 1s），
    依赖 `pytest-benchmark`，addopts 加 `--benchmark-disable` 保持 CI 快速，手动 `--benchmark-enable --run-slow` 运行
  - Task 20: CI 矩阵 — workflow 加 `actions/setup-python@v5` + matrix 3.10/3.11/3.12（fail-fast: false），
    pytest/coverage/ruff 三版本各跑一遍
  - 新增 marker：`regression`（回归锁定）、`benchmark`（性能基准）
  - 验证：默认 `pytest` 208 passed + 23 skipped；`--run-slow` 232 passed + 1 skipped（av1 硬件限制）

---

## 全部阶段完成 ✅
