#!/usr/bin/env bash
#
# 转码测试驱动脚本：在干净的 ZFS 快照环境上执行多轮视频转码测试，
# 每轮可带独立 CLI 参数，结束后调用 AI 分析各轮日志，N>1 时生成对比报告。
#
# Transcoding test driver: runs multiple transcode iterations on a clean ZFS
# snapshot state, each with optional per-iteration CLI args, calls the AI
# analyzer on each run's log, and produces a cross-run comparison when N > 1.
#
# 数据安全警告 / Data-loss warning:
#   本脚本含破坏性命令：`zfs rollback -r`、`rm -rf /mnt/transcode_cache/*`，
#   以及 `kill -KILL`。运行会丢弃快照点之后写入的所有数据并清空缓存目录，
#   务必确认数据可丢弃后再执行。详见下方各步骤的 CAUTION 注释。
#
# 前置依赖 / Prerequisites:
#   - 存在 ZFS 数据集与快照 ($ZFS_DATASET@$ZFS_SNAPSHOT)
#   - 可用 Python 解释器（优先 .venv/bin/python，其次 python3 / python）
#   - 可用的 AI API 端点 ($API_URL / $API_MODEL) 用于日志分析
#
# 用法 / Usage:
#   ./test_run.sh [-n N] [-t SECONDS] [--args "..."]...
#
# 产物 / Outputs:
#   data/test_app_{1..N}.log          各轮转码日志
#   data/ai_report_{1..N}.md          各轮 AI 分析报告
#   data/ai_report_comparison.md      (N>1) 多轮对比报告
#
set -euo pipefail

# ============================================================
# Configuration / 配置区
# ============================================================
APP_DIR="/home/caolv/PycharmProjects/video_convert"
TEST_DURATION=3600            # 每轮最大运行秒数 / max seconds per iteration
ZFS_DATASET="pool0/test1"
ZFS_SNAPSHOT="snap8"
API_URL="http://192.168.1.16:8001/v1/chat/completions"
API_MODEL="gemma-4-12B"
ITERATIONS=1
ARGS_LIST=()                  # 每轮额外的 CLI 参数 / per-iteration extra CLI args

# ============================================================
# Argument parsing / 参数解析
# ============================================================
usage() {
    echo "Usage: $0 [-n iterations] [-t duration] [--args \"...\"]..."
    echo "  -n N         Number of test iterations (default: 1)  轮数（默认 1）"
    echo "  -t SECONDS   Test duration per iteration (default: $TEST_DURATION)  每轮时长（秒）"
    echo "  --args \"...\"  Extra CLI args for the next iteration  下一轮的额外 CLI 参数"
    echo ""
    echo "Examples / 示例:"
    echo "  $0 -n 3 -t 1800 --args \"--encoder hevc\" --args \"--encoder av1\" --args \"--encoder hevc\""
    echo "  $0 -n 2"
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        -n) ITERATIONS="$2"; shift 2 ;;
        -t) TEST_DURATION="$2"; shift 2 ;;
        --args) ARGS_LIST+=("$2"); shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

cd "$APP_DIR"

# Find a working Python interpreter / 查找可用的 Python 解释器
PYTHON_BIN=".venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
    echo "ERROR: no Python interpreter found"
    exit 1
fi
echo "  Python: $($PYTHON_BIN --version 2>&1)"

# Pad missing --args with empty strings so every iteration has a slot
# 用空串补齐不足的 --args，保证每轮都有一个对应槽位
while [ ${#ARGS_LIST[@]} -lt $ITERATIONS ]; do
    ARGS_LIST+=("")
done

echo "=============================================="
echo " Video Transcoder Test Suite"
echo " $(date)"
echo " Iterations: $ITERATIONS"
echo " Duration:   ${TEST_DURATION}s per iteration"
echo "=============================================="

# ============================================================
# run_iteration: 单轮测试流程
#   1) ZFS 回滚到已知良好快照
#   2) 清理转码缓存与旧状态
#   3) 后台启动转码器
#   4) 等待至 TEST_DURATION（或进程自行退出）
#   5) 超时后强制停止
#
# run_iteration: single test cycle
#   1) ZFS rollback to known-good snapshot
#   2) Clean transcode cache & old state
#   3) Start transcoder in background
#   4) Wait up to TEST_DURATION (or until process exits)
#   5) Force-stop if still alive after timeout
# ============================================================
run_iteration() {
    local iter="$1"
    local extra_args="$2"
    local log_file="data/test_app_${iter}.log"
    local hist_file="data/test_history_${iter}.db"

    echo ""
    echo "=============================================="
    echo " Iteration $iter / $ITERATIONS"
    echo "  Args: $extra_args"
    echo "  Log:  $log_file"
    echo "  Hist: $hist_file"
    echo "=============================================="

    echo ""
    # --- 1. Rollback ZFS to clean filesystem state ---
    # --- 1. 回滚 ZFS 到干净的文件系统状态 ---
    echo ""
    echo "  [1] Rolling back $ZFS_DATASET@$ZFS_SNAPSHOT ..."
    # 危险：zfs rollback -r 会销毁快照点之后的所有数据。仅应在快照存在且
    # 数据可丢弃时使用。CAUTION: `zfs rollback -r` destroys all data written
    # after the snapshot; only safe when that data is expendable.
    if zfs list -t snapshot -r "$ZFS_DATASET" 2>/dev/null | grep -q "@$ZFS_SNAPSHOT"; then
        zfs rollback -r "$ZFS_DATASET@$ZFS_SNAPSHOT"
        echo "    OK"
    else
        echo "    ERROR: snapshot $ZFS_DATASET@$ZFS_SNAPSHOT not found"
        return 1
    fi

    echo ""
    # --- 2. Remove any leftover cache, history DB, and old log ---
    # --- 2. 清理残留的缓存、历史库与旧日志 ---
    echo ""
    echo "  [2] Cleaning cache and test state ..."
    # 危险：`rm -rf /mnt/transcode_cache/*` 会递归强制删除该硬编码缓存目录下的
    # 所有内容（不删除目录本身），操作不可撤销。执行前务必确认缓存目录路径正确、
    # 其中数据均可丢弃。CAUTION: this forcibly deletes everything under the
    # hardcoded /mnt/transcode_cache/ directory (the directory itself remains);
    # irreversible — only safe when that data is expendable.
    rm -rf /mnt/transcode_cache/*
    rm -f "$hist_file"*
    rm -f "$log_file"
    echo "    OK"

    echo ""
    # --- 3. Launch transcoder with per-iteration args ---
    # --- 3. 以每轮参数启动转码器 ---
    echo ""
    echo "  [3] Starting transcoder ..."
    # shellcheck disable=SC2086  # 有意进行单词拆分，以传入多参数
    $PYTHON_BIN main.py -L "test1=/mnt/test1:/mnt/test1:/mnt/transcode_cache" \
        --log "$log_file" --history "$hist_file" $extra_args &
    PID=$!
    echo "    PID=$PID"
    sleep 1
    if ! kill -0 $PID 2>/dev/null; then
        echo "    ERROR: transcoder failed to start"
        wait $PID 2>/dev/null || true
        return 1
    fi

    echo ""
    # --- 4. Poll until timeout or natural exit ---
    # --- 4. 轮询直到超时或自然退出 ---
    echo ""
    echo "  [4] Waiting up to ${TEST_DURATION}s (PID $PID) ..."
    WAIT_SECONDS=0
    START_TS=$(date +%s)
    while [ $WAIT_SECONDS -lt $TEST_DURATION ]; do
        if ! kill -0 $PID 2>/dev/null; then
            wait $PID 2>/dev/null || true
            END_TS=$(date +%s)
            ELAPSED=$((END_TS - START_TS))
            echo "    Transcoder exited naturally after ${ELAPSED}s"
            break
        fi
        sleep 10
        WAIT_SECONDS=$((WAIT_SECONDS + 10))
    done

    # --- 5. Graceful stop, escalate to SIGKILL if needed ---
    # --- 5. 优雅停止，必要时升级为 SIGKILL ---
    # 仅当进程在超时后仍存活时执行：先发 SIGTERM，等待最多 30 秒让其自行清理，
    # 若仍存活再发 SIGKILL 强制终止，最后 wait 回收子进程。
    # Only runs if the process is still alive after the timeout: send SIGTERM,
    # wait up to 30s for clean shutdown, escalate to SIGKILL if it persists,
    # then `wait` to reap the child.
    if kill -0 $PID 2>/dev/null; then
        echo ""
        echo "  [5] Timeout reached after ${TEST_DURATION}s, stopping PID $PID ..."
        kill -TERM $PID
        for i in $(seq 1 30); do
            if ! kill -0 $PID 2>/dev/null; then
                break
            fi
            sleep 1
        done
        if kill -0 $PID 2>/dev/null; then
            echo "    WARNING: PID $PID still alive, sending SIGKILL"
            # 危险：SIGKILL 无法被捕获，会立即终止进程（不留清理机会）。
            # CAUTION: SIGKILL cannot be caught and terminates the process
            # immediately (no chance to clean up).
            kill -KILL $PID 2>/dev/null || true
        fi
        wait $PID 2>/dev/null || true
    fi
    echo "    Stopped"
}

# ============================================================
# Main test loop — each iteration runs independently
# 主测试循环——每轮独立运行
# ============================================================
for i in $(seq 1 $ITERATIONS); do
    idx=$((i - 1))
    run_iteration "$i" "${ARGS_LIST[$idx]}"

    # --------------------------------------------------
    # Per-iteration AI analysis (with args context)
    # 每轮 AI 分析（携带参数上下文）
    # --------------------------------------------------
    echo ""
    echo "  [ai] Analyzing iteration $i ..."
    LOG_FILE="data/test_app_${i}.log"
    REPORT_FILE="data/ai_report_${i}.md"
    if [ -s "$LOG_FILE" ]; then
        if $PYTHON_BIN src/ai_analyze.py \
            "$API_URL" "$API_MODEL" \
            "$APP_DIR/$LOG_FILE" \
            "$APP_DIR/$REPORT_FILE" \
            "${ARGS_LIST[$idx]}" 2>/dev/null; then
            echo "  Report saved to $REPORT_FILE"
        else
            echo "  WARNING: AI analysis API call failed for iteration $i"
        fi
    else
        echo "  WARNING: $LOG_FILE is empty, skipping AI analysis"
    fi
done

# ============================================================
# Cross-iteration comparison analysis (only when N > 1)
# 跨轮次对比分析（仅当 N > 1 时）
# ============================================================
echo ""
echo "=============================================="
echo " Comparison Analysis"
echo "=============================================="

if [ "$ITERATIONS" -le 1 ]; then
    echo "  Only $ITERATIONS iteration(s), skipping comparison"
else
    ALL_EXIST=true
    for i in $(seq 1 $ITERATIONS); do
        if [ ! -s "data/test_app_${i}.log" ]; then
            ALL_EXIST=false
            echo "  WARNING: data/test_app_${i}.log is missing or empty"
        fi
    done

    if [ "$ALL_EXIST" = true ]; then
        # Build a combined markdown file with all runs for the AI
        # 把所有轮次的日志合并到一个 markdown 文件供 AI 分析
        COMPARE_FILE=$(mktemp)

        cat > "$COMPARE_FILE" << COMPARE_TOP
# 多次运行对比分析

以下共 $ITERATIONS 次运行，每次使用不同的转码参数。请对比分析各次运行的结果。

COMPARE_TOP

        for i in $(seq 1 $ITERATIONS); do
            idx=$((i - 1))
            {
                echo ""
                echo "---"
                echo ""
                echo "## 第 ${i} 次运行"
                echo ""
                echo "**运行参数：** ${ARGS_LIST[$idx]}"
                echo ""
                echo '```'
                cat "data/test_app_${i}.log"
                echo '```'
            } >> "$COMPARE_FILE"
        done

        # Override system prompt with comparison-specific instructions
        # 用对比专用指令覆盖系统提示词
        export AI_SYSTEM_PROMPT="You are a professional video transcoding quality analyst. Below are the results of ${ITERATIONS} test runs with different configurations. Compare and contrast the runs, covering:
1. Configuration differences between each run
2. Key metrics comparison: files processed, success rate, VMAF scores, encoding time, compression ratio
3. Which configuration performed best overall and in each metric
4. Recommendations for the optimal configuration
Format the output in clean Markdown. All content must be in Simplified Chinese (简体中文)."

        if $PYTHON_BIN src/ai_analyze.py \
            "$API_URL" "$API_MODEL" \
            "$COMPARE_FILE" \
            "$APP_DIR/data/ai_report_comparison.md" 2>/dev/null; then
            echo ""
            echo "=== Comparison Report ==="
            cat "$APP_DIR/data/ai_report_comparison.md"
            echo ""
            echo "========================="
            echo "Report saved to data/ai_report_comparison.md"
        else
            echo "  WARNING: API call failed for comparison analysis"
        fi

        unset AI_SYSTEM_PROMPT
        rm -f "$COMPARE_FILE"
    else
        echo "  WARNING: some logs are missing or empty, skipping comparison analysis"
    fi
fi

echo ""
echo "=============================================="
echo " All $ITERATIONS iteration(s) complete"
echo " Logs:    data/test_app_{1..N}.log"
echo " Reports: data/ai_report_{1..N}.md"
if [ "$ITERATIONS" -gt 1 ]; then
    echo "           data/ai_report_comparison.md"
fi
echo "=============================================="