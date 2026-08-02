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
- Phase 2: pending
- Phase 3: pending
- Phase 4: pending
- Phase 5: pending
