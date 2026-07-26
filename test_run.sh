#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/caolv/PycharmProjects/video_convert"
TEST_DURATION=3000
ZFS_DATASET="pool0/test1"
ZFS_SNAPSHOT="snap8"
API_URL="http://192.168.1.16:8001/v1/chat/completions"
API_MODEL="gemma-4-12B"

cd "$APP_DIR"

PYTHON_BIN=".venv/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python"
if ! command -v "$PYTHON_BIN" &>/dev/null; then
    echo "ERROR: no Python interpreter found"
    exit 1
fi
echo "  Python: $($PYTHON_BIN --version 2>&1)"

echo "=============================================="
echo " Video Transcoder Test Suite"
echo " $(date)"
echo "=============================================="

# --- 1. Rollback ZFS ---
echo ""
echo "[1/6] Rolling back $ZFS_DATASET@$ZFS_SNAPSHOT ..."
if zfs list -t snapshot -r "$ZFS_DATASET" 2>/dev/null | grep -q "@$ZFS_SNAPSHOT"; then
    zfs rollback -r "$ZFS_DATASET@$ZFS_SNAPSHOT"
    echo "  OK"
else
    echo "  ERROR: snapshot $ZFS_DATASET@$ZFS_SNAPSHOT not found"
    exit 1
fi

# --- 2. Clean state ---
echo ""
echo "[2/6] Cleaning cache and test state ..."
rm -rf /mnt/transcode_cache/*
rm -f data/test_history.db*
rm -f data/test_app.log
echo "  OK"

# --- 3. Start transcoder ---
echo ""
echo "[3/6] Starting transcoder ..."
$PYTHON_BIN main.py -L "test1=/mnt/test1:/mnt/test1:/mnt/transcode_cache" --log "data/test_app.log" --history "data/test_history.db" &
PID=$!
echo "  PID=$PID"
sleep 1
if ! kill -0 $PID 2>/dev/null; then
    echo "  ERROR: transcoder failed to start"
    wait $PID 2>/dev/null || true
    exit 1
fi

# --- 4. Wait with timeout, exit early if process dies ---
echo ""
echo "[4/6] Waiting up to ${TEST_DURATION}s (PID $PID) ..."
WAIT_SECONDS=0
START_TS=$(date +%s)
while [ $WAIT_SECONDS -lt $TEST_DURATION ]; do
    if ! kill -0 $PID 2>/dev/null; then
        wait $PID 2>/dev/null || true
        END_TS=$(date +%s)
        ELAPSED=$((END_TS - START_TS))
        echo "  Transcoder exited naturally after ${ELAPSED}s"
        break
    fi
    sleep 10
    WAIT_SECONDS=$((WAIT_SECONDS + 10))
done

# --- 5. Stop if still running ---
if kill -0 $PID 2>/dev/null; then
    echo ""
    echo "[5/6] Timeout reached after ${TEST_DURATION}s, stopping PID $PID ..."
    kill -TERM $PID
    for i in $(seq 1 30); do
        if ! kill -0 $PID 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if kill -0 $PID 2>/dev/null; then
        echo "  WARNING: PID $PID still alive, sending SIGKILL"
        kill -KILL $PID 2>/dev/null || true
    fi
    wait $PID 2>/dev/null || true
fi
echo "  Stopped"

# --- 6. Save log and analyze ---
echo ""
echo "[6/6] Analyzing log with AI ..."
if [ ! -s data/test_app.log ]; then
    echo "  WARNING: test_app.log is empty, skipping AI analysis"
    exit 0
fi

RESPONSE_FILE="data/ai_report.md"

if $PYTHON_BIN src/ai_analyze.py \
    "$API_URL" "$API_MODEL" \
    "$APP_DIR/data/test_app.log" \
    "$APP_DIR/$RESPONSE_FILE" 2>/dev/null; then
    echo ""
    echo "=== AI Analysis Report ==="
    cat "$APP_DIR/$RESPONSE_FILE"
    echo ""
    echo "=========================="
    echo "Report saved to $RESPONSE_FILE"
else
    echo "  WARNING: API call failed, analysis skipped"
    echo "# AI Report - API call failed at $(date)" > "$APP_DIR/$RESPONSE_FILE"
fi

echo ""
echo "=============================================="
echo " Test complete: log saved to data/test_app.log"
echo "=============================================="
