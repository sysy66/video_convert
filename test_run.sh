#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Configuration
# ============================================================
APP_DIR="/home/caolv/PycharmProjects/video_convert"
TEST_DURATION=3600            # max seconds per iteration
ZFS_DATASET="pool0/test1"
ZFS_SNAPSHOT="snap8"
API_URL="http://192.168.1.16:8001/v1/chat/completions"
API_MODEL="gemma-4-12B"
ITERATIONS=1
ARGS_LIST=()                  # per-iteration extra CLI args

# ============================================================
# Argument parsing
# ============================================================
usage() {
    echo "Usage: $0 [-n iterations] [-t duration] [--args \"...\"]..."
    echo "  -n N         Number of test iterations (default: 1)"
    echo "  -t SECONDS   Test duration per iteration (default: $TEST_DURATION)"
    echo "  --args \"...\"  Extra CLI args for the next iteration"
    echo ""
    echo "Examples:"
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

# Find a working Python interpreter
PYTHON_BIN=".venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
    echo "ERROR: no Python interpreter found"
    exit 1
fi
echo "  Python: $($PYTHON_BIN --version 2>&1)"

# Pad missing --args with empty strings so every iteration has a slot
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
    echo ""
    echo "  [1] Rolling back $ZFS_DATASET@$ZFS_SNAPSHOT ..."
    if zfs list -t snapshot -r "$ZFS_DATASET" 2>/dev/null | grep -q "@$ZFS_SNAPSHOT"; then
        zfs rollback -r "$ZFS_DATASET@$ZFS_SNAPSHOT"
        echo "    OK"
    else
        echo "    ERROR: snapshot $ZFS_DATASET@$ZFS_SNAPSHOT not found"
        return 1
    fi

    echo ""
    # --- 2. Remove any leftover cache, history DB, and old log ---
    echo ""
    echo "  [2] Cleaning cache and test state ..."
    rm -rf /mnt/transcode_cache/*
    rm -f "$hist_file"*
    rm -f "$log_file"
    echo "    OK"

    echo ""
    # --- 3. Launch transcoder with per-iteration args ---
    echo ""
    echo "  [3] Starting transcoder ..."
    # shellcheck disable=SC2086
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
            kill -KILL $PID 2>/dev/null || true
        fi
        wait $PID 2>/dev/null || true
    fi
    echo "    Stopped"
}

# ============================================================
# Main test loop — each iteration runs independently
# ============================================================
for i in $(seq 1 $ITERATIONS); do
    idx=$((i - 1))
    run_iteration "$i" "${ARGS_LIST[$idx]}"

    # --------------------------------------------------
    # Per-iteration AI analysis (with args context)
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