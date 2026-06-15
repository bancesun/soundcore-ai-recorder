# Soundcore AI Recorder to Obsidian

[English](README.md) | [中文](README.zh-CN.md)

Download Soundcore AI recordings, remove long ambient/noise sections, transcribe them, summarize each day into an Obsidian Markdown note, and archive the original audio plus full transcripts to a NAS or local folder.

This project was built for daily wearable recordings that may contain Chinese, English, German, Italian, background street conversations, and long stretches of silence or non-speech.

## What It Does

- Opens `https://ai.soundcore.com/home` with a persistent Chrome profile.
- Downloads Soundcore recordings that have not been archived yet.
- Checks the previous lookback window so missed days can be backfilled.
- Uses local VAD to keep mostly conversation before sending audio to OpenAI Whisper.
- Transcribes audio with OpenAI Whisper, or optionally local whisper.cpp.
- Writes one Obsidian note per day.
- Stores original recordings and full transcripts in an archive folder, such as a mounted NAS.
- Can run automatically every evening on macOS with LaunchAgent.

## What Is Not Uploaded

The repository intentionally ignores runtime and private data:

- `.env`
- `.browser-profile/`
- `.soundcore_manifest.json`
- `downloads/`
- `archive/`
- `transcripts/`
- `logs/`
- `models/`
- `.venv/`

Do not commit real API keys, browser profiles, audio files, transcripts, NAS URLs containing passwords, or personal notes.

## Requirements

- macOS is recommended for the included LaunchAgent and NAS auto-mount flow.
- Python 3.11+
- Chrome
- ffmpeg
- OpenAI API key
- Soundcore account login
- Optional but recommended: Homebrew `whisper-cpp` for local VAD

Install system tools:

```bash
brew install ffmpeg whisper-cpp
```

Create a Python environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

If you use `SOUNDCORE_BROWSER_CHANNEL=chrome`, install Google Chrome separately.

## Configuration

Create your private config:

```bash
cp .env.example .env
```

Edit `.env`.

Important settings:

```bash
OBSIDIAN_VAULT=/path/to/your/Obsidian/vault/全天AI录音
NAS_ARCHIVE=/Volumes/media/全天AI录音
OPENAI_API_KEY=<your-openai-api-key>
NAS_URL=smb://username@nas-host.local/media
NAS_MOUNT_POINT=/Volumes/media
```

### OpenAI API Key

1. Go to https://platform.openai.com/api-keys.
2. Create an API key.
3. Put it in `.env` as `OPENAI_API_KEY=...`.
4. Make sure your OpenAI account has billing enabled.

The default setup uses:

- `whisper-1` for transcription
- `gpt-4o-mini` for summaries

### Soundcore Login

The first run opens Chrome with a persistent profile stored in `.browser-profile/soundcore`.

Run:

```bash
source .venv/bin/activate
python scripts/soundcore_download.py
```

If Soundcore asks you to log in, complete Apple/Soundcore login in the opened Chrome window. After that, future runs reuse the saved browser session.

Do not commit `.browser-profile/`; it contains login state.

### NAS Passwords

Use an SMB URL without a password:

```bash
NAS_URL=smb://username@nas-host.local/media
NAS_MOUNT_POINT=/Volumes/media
```

When Finder asks for the password, save it to macOS Keychain. The script calls:

```bash
open "$NAS_URL"
```

macOS handles the credential lookup. Do not put NAS passwords in `.env`.

## VAD and Cost Control

For all-day recordings, most audio can be silence, room noise, walking, traffic, or background speech. The default config uses local VAD:

```bash
TRANSCRIBE_PREFILTER=vad
TRANSCRIBE_COMPRESS_AUDIO=true
TRANSCRIBE_AUDIO_BITRATE=32k
```

This creates a temporary speech-only file, then uploads only that shorter file to OpenAI Whisper. The original audio is still archived unchanged.

Download the Silero VAD model used by whisper.cpp:

```bash
mkdir -p models/whisper.cpp
curl -L -o models/whisper.cpp/ggml-silero-v6.2.0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-silero-v6.2.0.bin
```

If VAD is unavailable, the script falls back to full audio and prints a warning.

## Run Manually

Download new recordings:

```bash
source .venv/bin/activate
python scripts/soundcore_download.py
```

Transcribe, summarize, and archive:

```bash
source .venv/bin/activate
python scripts/process_recordings.py
```

Or run the whole workflow:

```bash
python scripts/run_daily.py
```

## Daily Automation on macOS

Install a LaunchAgent:

```bash
python scripts/install_launch_agent.py --hour 22 --minute 30
```

Check status:

```bash
launchctl print gui/$(id -u)/com.soundcore-ai-recorder.daily
```

View logs:

```bash
tail -f logs/daily.out.log
tail -f logs/daily.err.log
```

The Mac must be awake and logged into the user session. You can add a wake schedule:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 22:25:00
```

## Obsidian Output

Each day creates one file:

```text
YYYY-MM-DD.md
```

The note includes:

- frontmatter
- overview
- detailed timeline/scenes
- important highlights
- main topic deep dive
- secondary signals
- decisions and commitments
- action items
- speaker notes
- medical/insurance section when relevant
- archive paths for audio and full transcripts

There is no fixed daily theme. The summary prompt asks the model to infer what mattered most that day.

## Archive Layout

Archive output is grouped by year and date:

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

The local manifest `.soundcore_manifest.json` tracks processed files so duplicate downloads are skipped.

## Local Transcription Option

OpenAI Whisper is usually better for noisy multilingual wearable audio. If you want to use local whisper.cpp:

```bash
USE_LOCAL_WHISPER=true
LOCAL_WHISPER_BACKEND=whisper_cpp
WHISPER_CPP_COMMAND=/usr/local/bin/whisper-cli
WHISPER_CPP_MODEL=models/whisper.cpp/ggml-medium-q5_0.bin
```

Download a model:

```bash
mkdir -p models/whisper.cpp
curl -L -o models/whisper.cpp/ggml-medium-q5_0.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin
```

## Troubleshooting

If Soundcore login is required:

```bash
python scripts/soundcore_download.py
```

Then log in inside the opened Chrome window.

If NAS mounting fails:

- Open the SMB share manually in Finder once.
- Save credentials to Keychain.
- Confirm `NAS_MOUNT_POINT` matches the mounted volume.

If OpenAI calls fail:

- Confirm `OPENAI_API_KEY` is set.
- Confirm billing is enabled.
- Try one shorter audio file first.

If the daily job does not run:

```bash
launchctl print gui/$(id -u)/com.soundcore-ai-recorder.daily
```

If needed, reinstall:

```bash
python scripts/install_launch_agent.py --hour 22 --minute 30
```

## Privacy

Wearable daily recordings are highly sensitive. Review your local laws and obtain consent where required. Be careful before sending audio to any cloud transcription service. Use local transcription if your privacy requirements are stricter than OpenAI processing allows.
