# 🚀 项目路线图：视频自动化转码系统

本项目旨在通过实践学习不同编程语言的特性。我们将从 Python 开始，最终过渡到 Golang。

## 🏗️ 解决方案概略与设计

### 1. 核心模块架构
*   **文件监控 (Watchdog)**：定时扫描或监听 `input` 文件夹，识别新视频。
*   **转码引擎 (FFmpeg + QSV)**：调用底层 FFmpeg，利用 `h264_qsv` 或 `hevc_qsv` 驱动 1370PE 核显进行高效率硬解硬编。
*   **状态记录 (Log/JSON)**：使用轻量级 `history.json` 文件记录转码状态，防止重复处理。
*   **Web 界面 (Web UI)**：提供可视化列表，支持在浏览器中播放（统一转码为兼容性最好的 MP4 格式）。

### 2. 1370PE 核显转码核心命令
在 Debian VM 中，利用 Intel QSV 转码的标准命令格式如下：

```bash
ffmpeg -hwaccel qsv -c:v h264_qsv -i input.mkv -c:v h264_qsv -b:v 5M -c:a aac output.mp4
```
(注：实际脚本中我们会做输入格式自适应，确保顺畅触发硬解码和硬编码。)

## 📅 开发阶段规划

### 阶段一：转码核心与文件扫描
*   **核心功能**：编写后台脚本，实现文件夹自动扫描、FFmpeg 核显转码调用及日志记录。
*   **目标**：建立稳健的自动化处理基础。

### 阶段二：Web 浏览界面
*   **核心功能**：基于 Flask 或 FastAPI 框架构建极简 Web UI。
*   **目标**：实现类似 Tdarr 的界面，支持查看转码历史并在线播放视频。

### 阶段三：Golang 版本重构
*   **核心功能**：在 Python 逻辑稳定后，使用 Golang 进行重写。
*   **目标**：对比 Python 与 Go 的并发模型（Goroutine）及语法特性。

---

## 🛠️ 环境确认与数据收集

在开始编写阶段一的代码前，请确认以下 PVE Debian VM 环境细节：

1.  **FFmpeg QSV 支持**：
    *   请确认是否已安装支持 Intel QSV 加速的 FFmpeg 版本。
    *   *验证命令*：`ffmpeg -encoders | grep qsv` (检查是否有 `h264_qsv`)。
2.  **显卡设备路径**：
    *   核显在虚拟机中的设备节点通常为 `/dev/dri/renderD128`。
    *   *权限要求*：确保运行用户拥有访问该节点的权限（建议加入 `video` 或 `render` 组）。
3.  **目录结构配置**：
    *   请提供您希望使用的路径（例如：输入目录 `/opt/video/input`，输出目录 `/opt/video/output`，日志文件位置）。

---

