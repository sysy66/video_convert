#!/usr/bin/env bash
# 重新生成合成测试视频（320x240、1 秒、10fps 的 testsrc 测试图案），供集成测试使用。
# 需系统 ffmpeg 支持 libx264 / libx265。
# 注意：QSV 最小编码分辨率为 320x240，更小（如 64x64）会被 hevc_qsv/av1_qsv
# 拒绝，报错 Current resolution is unsupported。
set -euo pipefail

cd "$(dirname "$0")"

# 1. H.264 + AAC 音频（有音频流，测音频编码/passthrough 路径）
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=10:duration=1" \
  -f lavfi -i "sine=frequency=440:duration=1" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest \
  synth_320x240_1s_h264_aac.mp4

# 2. H.264 纯视频（无音频流，QSV 转码输入）
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=10:duration=1" \
  -c:v libx264 -pix_fmt yuv420p -an \
  synth_320x240_1s_h264.mp4

# 3. HEVC 8bit（probe 第二 codec + VMAF 失真参考）
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=10:duration=1" \
  -c:v libx265 -pix_fmt yuv420p -an \
  synth_320x240_1s_hevc.mp4

# 4. HEVC 10bit Main10（QSV p010le 输入路径）
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=10:duration=1" \
  -c:v libx265 -pix_fmt yuv420p10le -profile:v main10 -an \
  synth_320x240_1s_hevc10bit.mkv

# 5. MPEG-4（旧编码 + AVI 容器）
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=10:duration=1" \
  -c:v mpeg4 -q:v 5 -an \
  synth_320x240_1s_mpeg4.avi

# 6. VP9（WebM 容器）
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=320x240:rate=10:duration=1" \
  -c:v libvpx-vp9 -pix_fmt yuv420p -b:v 200k -an \
  synth_320x240_1s_vp9.webm
