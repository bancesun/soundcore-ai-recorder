# Soundcore AI Recorder to Obsidian

[English](README.md) | [中文](README.zh-CN.md)

这个项目可以自动下载 Soundcore AI 录音，过滤掉大量空白、环境声和无意义片段，转写成文字，然后每天生成一篇 Obsidian Markdown 笔记。同时，它会把原始录音和完整 transcript 归档到 NAS 或本地文件夹。

它适合全天候穿戴式录音场景：录音里可能混有中文、英文、德文、意大利文、街边背景对话，以及很长时间的安静、噪音或非语音内容。

## 功能

- 用持久化 Chrome Profile 打开 `https://ai.soundcore.com/home`。
- 下载还没有归档过的 Soundcore 录音。
- 自动检查最近一段时间内是否有漏掉的日期，支持补下载。
- 先用本地 VAD 保留主要语音，再把较短音频发给 OpenAI Whisper，减少费用。
- 使用 OpenAI Whisper 转写，也可以选择本地 whisper.cpp。
- 每天生成一个 Obsidian Markdown 文件。
- 把原始音频和完整 transcript 归档到 NAS 或本地目录。
- 在 macOS 上可以通过 LaunchAgent 每天晚上自动运行。

## 不会上传到 GitHub 的内容

仓库已经通过 `.gitignore` 排除了运行时数据和隐私数据：

- `.env`
- `.browser-profile/`
- `.soundcore_manifest.json`
- `downloads/`
- `archive/`
- `transcripts/`
- `logs/`
- `models/`
- `.venv/`

请不要提交真实 API Key、浏览器登录状态、录音文件、transcript、带密码的 NAS URL 或个人 Obsidian 笔记。

## 环境要求

- 推荐 macOS，因为本项目包含 LaunchAgent 和 NAS 自动挂载流程。
- Python 3.11+
- Chrome
- ffmpeg
- OpenAI API Key
- Soundcore 账号登录
- 可选但推荐：Homebrew 安装 `whisper-cpp`，用于本地 VAD

安装系统工具：

```bash
brew install ffmpeg whisper-cpp
```

创建 Python 虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

如果你配置 `SOUNDCORE_BROWSER_CHANNEL=chrome`，请另外安装 Google Chrome。

## 配置

复制示例配置：

```bash
cp .env.example .env
```

然后编辑 `.env`。

最重要的配置如下：

```bash
OBSIDIAN_VAULT=/path/to/your/Obsidian/vault/全天AI录音
NAS_ARCHIVE=/Volumes/media/全天AI录音
OPENAI_API_KEY=<your-openai-api-key>
NAS_URL=smb://username@nas-host.local/media
NAS_MOUNT_POINT=/Volumes/media
```

### OpenAI API Key

1. 打开 https://platform.openai.com/api-keys。
2. 创建一个 API Key。
3. 写入 `.env`：`OPENAI_API_KEY=...`。
4. 确认 OpenAI 账号已经开启 billing。

默认模型：

- `whisper-1` 用于转写
- `gpt-4o-mini` 用于每天的 Summary

不要把真实 API Key 提交到 GitHub。

### Soundcore 登录

第一次运行会打开 Chrome，并使用 `.browser-profile/soundcore` 作为持久化浏览器 Profile。

运行：

```bash
source .venv/bin/activate
python scripts/soundcore_download.py
```

如果 Soundcore 要求登录，请在打开的 Chrome 窗口里完成 Apple/Soundcore 登录。之后脚本会复用这个浏览器登录状态。

不要提交 `.browser-profile/`，里面包含登录状态。

### NAS 密码

建议在 `.env` 中只写不带密码的 SMB URL：

```bash
NAS_URL=smb://username@nas-host.local/media
NAS_MOUNT_POINT=/Volumes/media
```

第一次在 Finder 中挂载 NAS 时，把密码保存到 macOS Keychain。脚本会调用：

```bash
open "$NAS_URL"
```

macOS 会自动从 Keychain 读取密码。不要把 NAS 密码写入 `.env`。

## VAD 和费用控制

全天录音里通常有大量安静片段、房间噪音、走路声、交通声或背景对话。默认配置会先用本地 VAD 过滤音频：

```bash
TRANSCRIBE_PREFILTER=vad
TRANSCRIBE_COMPRESS_AUDIO=true
TRANSCRIBE_AUDIO_BITRATE=32k
```

脚本会生成一个临时的 speech-only 音频，再把这个较短音频上传到 OpenAI Whisper。原始录音仍然会完整归档，不会被改动。

下载 whisper.cpp 使用的 Silero VAD 模型：

```bash
mkdir -p models/whisper.cpp
curl -L -o models/whisper.cpp/ggml-silero-v6.2.0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-silero-v6.2.0.bin
```

如果 VAD 不可用，脚本会打印警告，并退回到上传完整音频。

## 手动运行

下载新的 Soundcore 录音：

```bash
source .venv/bin/activate
python scripts/soundcore_download.py
```

转写、总结、归档：

```bash
source .venv/bin/activate
python scripts/process_recordings.py
```

或者一次跑完整流程：

```bash
python scripts/run_daily.py
```

## macOS 每日自动运行

安装 LaunchAgent：

```bash
python scripts/install_launch_agent.py --hour 22 --minute 30
```

查看状态：

```bash
launchctl print gui/$(id -u)/com.soundcore-ai-recorder.daily
```

查看日志：

```bash
tail -f logs/daily.out.log
tail -f logs/daily.err.log
```

注意：Mac 需要开机，并且进入你的用户会话。你也可以设置自动唤醒：

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 22:25:00
```

## Obsidian 输出

每天会生成一个文件：

```text
YYYY-MM-DD.md
```

笔记包含：

- frontmatter
- 概述
- 详细时间线 / 场景
- 重要亮点
- 主要主题深入分析
- 次要信号
- 决策和承诺
- 行动项
- 说话者备注
- 如果当天相关，会包含医疗 / 保险部分
- 原始音频和完整 transcript 的归档路径

没有固定的每日主题。Summary prompt 会让模型根据当天内容自动判断什么最重要。

## 归档结构

归档会按年份和日期分组：

```text
NAS_ARCHIVE/
  recordings/
    2026/
      2026-06-15/
        recording.ogg
  transcripts/
    2026/
      2026-06-15/
        recording.txt
```

本地 `.soundcore_manifest.json` 会记录已经处理过的文件，避免重复下载和重复处理。

## 本地转写选项

对嘈杂、多语言、全天候录音来说，OpenAI Whisper 通常效果更好。如果你想使用本地 whisper.cpp：

```bash
USE_LOCAL_WHISPER=true
LOCAL_WHISPER_BACKEND=whisper_cpp
WHISPER_CPP_COMMAND=/usr/local/bin/whisper-cli
WHISPER_CPP_MODEL=models/whisper.cpp/ggml-medium-q5_0.bin
```

下载 whisper.cpp 模型：

```bash
mkdir -p models/whisper.cpp
curl -L -o models/whisper.cpp/ggml-medium-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin
```

## 常见问题

如果 Soundcore 需要重新登录：

```bash
python scripts/soundcore_download.py
```

然后在打开的 Chrome 窗口中登录。

如果 NAS 自动挂载失败：

- 先在 Finder 中手动打开 SMB 共享。
- 把密码保存到 Keychain。
- 确认 `NAS_MOUNT_POINT` 和实际挂载路径一致。

如果 OpenAI 调用失败：

- 确认 `.env` 中设置了 `OPENAI_API_KEY`。
- 确认 OpenAI billing 已启用。
- 先用一段短音频测试。

如果每日任务没有运行：

```bash
launchctl print gui/$(id -u)/com.soundcore-ai-recorder.daily
```

必要时重新安装：

```bash
python scripts/install_launch_agent.py --hour 22 --minute 30
```

## 隐私提醒

全天穿戴式录音非常敏感。请遵守当地法律，并在需要时取得相关人员同意。把音频发送到任何云端转写服务之前，都应该认真考虑隐私风险。如果你的隐私要求高于 OpenAI 处理方式所能满足的范围，请使用本地转写。

