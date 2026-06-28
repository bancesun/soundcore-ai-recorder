#!/usr/bin/env python3
"""Transcribe Soundcore recordings, create Obsidian notes, and archive files."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from hashlib import sha256
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".soundcore_manifest.json"
INBOX = ROOT / "downloads" / "inbox"
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".wav", ".aac", ".flac", ".ogg", ".webm"}
MAX_AUDIO_BYTES = 24 * 1024 * 1024
LETTER_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ\u4e00-\u9fff\u3040-\u30ff\u3130-\u318f\uac00-\ud7af]")
TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]+|[\u4e00-\u9fff]|[0-9]+")
VAD_SEGMENT_RE = re.compile(r"VAD segment \d+: start = ([0-9.]+), end = ([0-9.]+)")


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {"dates": {}, "files": {}}
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def save_manifest(data: dict) -> None:
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def recording_date(path: Path) -> str:
    match = re.search(r"(20\d{2})[-_.年]?(0[1-9]|1[0-2])[-_.月]?([0-2]\d|3[01])", path.stem)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"
    tz = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin"))
    return datetime.fromtimestamp(path.stat().st_mtime, tz).date().isoformat()


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def ensure_archive_available(archive_root: Path) -> None:
    configured = os.environ.get("NAS_ARCHIVE")
    if not configured:
        return
    archive_root = archive_root.expanduser()
    if archive_root.parts[:2] == ("/", "Volumes") and len(archive_root.parts) >= 3:
        volume_root = Path("/", "Volumes", archive_root.parts[2])
        if not volume_root.exists() or not volume_root.is_mount():
            raise RuntimeError(f"NAS volume is not mounted: {volume_root}")
    if not archive_root.exists():
        raise RuntimeError(f"NAS archive folder does not exist: {archive_root}")


def compressed_audio(path: Path, tmpdir: Path) -> Path:
    if os.environ.get("TRANSCRIBE_COMPRESS_AUDIO", "true").lower() not in {"1", "true", "yes"}:
        return path
    bitrate = os.environ.get("TRANSCRIBE_AUDIO_BITRATE", "32k")
    target = tmpdir / f"{path.stem}-whisper.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def split_audio(path: Path, tmpdir: Path) -> list[Path]:
    upload_source = prefilter_audio(path, tmpdir)
    if upload_source is None:
        return []
    upload_source = compressed_audio(upload_source, tmpdir)
    segment_seconds = int(os.environ.get("TRANSCRIBE_SEGMENT_SECONDS", "300"))
    if upload_source.stat().st_size <= MAX_AUDIO_BYTES and segment_seconds <= 0:
        return [upload_source]

    duration = ffprobe_duration(upload_source)
    size_parts = max(1, int(upload_source.stat().st_size / MAX_AUDIO_BYTES) + 1)
    time_parts = max(1, int(duration / segment_seconds) + 1)
    parts = max(size_parts, time_parts)
    segment_seconds = max(60, int(duration / parts) + 1)
    pattern = tmpdir / f"{upload_source.stem}-%03d.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(upload_source),
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            str(pattern),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(tmpdir.glob(f"{upload_source.stem}-*.mp3"))


def vad_available() -> bool:
    command = os.environ.get("VAD_COMMAND", "/usr/local/bin/whisper-vad-speech-segments")
    model = Path(os.environ.get("VAD_MODEL", str(ROOT / "models" / "whisper.cpp" / "ggml-silero-v6.2.0.bin"))).expanduser()
    return (Path(command).exists() or shutil.which(command) is not None) and model.exists()


def wav_for_vad(path: Path, tmpdir: Path) -> Path:
    target = tmpdir / f"{path.stem}-vad.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def vad_segments(path: Path) -> list[tuple[float, float]]:
    command = os.environ.get("VAD_COMMAND", "/usr/local/bin/whisper-vad-speech-segments")
    model = str(Path(os.environ.get("VAD_MODEL", str(ROOT / "models" / "whisper.cpp" / "ggml-silero-v6.2.0.bin"))).expanduser())
    result = subprocess.run(
        [
            command,
            "-f",
            str(path),
            "-vm",
            model,
            "-vt",
            os.environ.get("VAD_THRESHOLD", "0.45"),
            "--vad-min-speech-duration-ms",
            os.environ.get("VAD_MIN_SPEECH_MS", "300"),
            "--vad-min-silence-duration-ms",
            os.environ.get("VAD_MIN_SILENCE_MS", "700"),
            "--vad-speech-pad-ms",
            os.environ.get("VAD_SPEECH_PAD_MS", "250"),
            "-t",
            os.environ.get("VAD_THREADS", "4"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    segments: list[tuple[float, float]] = []
    for start, end in VAD_SEGMENT_RE.findall(result.stdout + "\n" + result.stderr):
        start_f = float(start)
        end_f = float(end)
        if end_f > start_f:
            segments.append((start_f, end_f))
    return segments


def write_concat_list(source: Path, segments: list[tuple[float, float]], tmpdir: Path) -> Path:
    list_file = tmpdir / "speech_segments.txt"
    lines: list[str] = []
    source_path = source.as_posix().replace("'", "'\\''")
    for start, end in segments:
        lines.append(f"file '{source_path}'")
        lines.append(f"inpoint {start:.3f}")
        lines.append(f"outpoint {end:.3f}")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list_file


def speech_only_audio(path: Path, tmpdir: Path) -> Path | None:
    source = wav_for_vad(path, tmpdir)
    segments = vad_segments(source)
    original_duration = ffprobe_duration(source)
    speech_duration = sum(end - start for start, end in segments)
    print(
        f"VAD kept {speech_duration / 60:.1f} of {original_duration / 60:.1f} minutes "
        f"({len(segments)} segment(s)) for {path.name}"
    )
    if not segments or speech_duration <= 0:
        return None
    target = tmpdir / f"{path.stem}-speech-only.wav"
    concat_list = write_concat_list(source, segments, tmpdir)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return target


def prefilter_audio(path: Path, tmpdir: Path) -> Path | None:
    mode = os.environ.get("TRANSCRIBE_PREFILTER", "none").lower()
    if mode in {"", "none", "false", "off"}:
        return path
    if mode == "vad":
        if not vad_available():
            print("VAD prefilter requested but unavailable; using full audio.")
            return path
        return speech_only_audio(path, tmpdir)
    if mode == "silence":
        target = tmpdir / f"{path.stem}-silence-trimmed.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-af",
                "silenceremove=start_periods=1:start_duration=0.3:start_threshold=-45dB:stop_periods=-1:stop_duration=1.0:stop_threshold=-45dB",
                str(target),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return target
    raise ValueError(f"Unknown TRANSCRIBE_PREFILTER: {mode}")


def split_audio_wav(path: Path, tmpdir: Path) -> list[Path]:
    segment_seconds = int(os.environ.get("TRANSCRIBE_SEGMENT_SECONDS", "300"))
    duration = ffprobe_duration(path)
    if segment_seconds <= 0 or duration <= segment_seconds:
        target = tmpdir / f"{path.stem}-whisper.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(target),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return [target]

    pattern = tmpdir / f"{path.stem}-whisper-%03d.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            str(pattern),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(tmpdir.glob(f"{path.stem}-whisper-*.wav"))


def local_whisper_available() -> bool:
    command = os.environ.get("LOCAL_WHISPER_COMMAND", "/opt/anaconda3/bin/whisper")
    return Path(command).exists() or shutil.which(command) is not None


def whisper_cpp_available() -> bool:
    command = os.environ.get("WHISPER_CPP_COMMAND", "/usr/local/bin/whisper-cli")
    model = os.environ.get("WHISPER_CPP_MODEL", str(ROOT / "models" / "whisper.cpp" / "ggml-medium-q5_0.bin"))
    command_exists = Path(command).exists() or shutil.which(command) is not None
    return command_exists and Path(model).expanduser().exists()


def transcribe_whisper_cpp(path: Path) -> str:
    command = os.environ.get("WHISPER_CPP_COMMAND", "/usr/local/bin/whisper-cli")
    model = str(Path(os.environ.get("WHISPER_CPP_MODEL", str(ROOT / "models" / "whisper.cpp" / "ggml-medium-q5_0.bin"))).expanduser())
    language = os.environ.get("WHISPER_CPP_LANGUAGE", "auto")
    threads = os.environ.get("WHISPER_CPP_THREADS", "6")
    fast = os.environ.get("WHISPER_CPP_FAST", "true").lower() in {"1", "true", "yes"}
    chunks: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for index, chunk in enumerate(split_audio_wav(path, tmpdir)):
            output_base = tmpdir / f"chunk-{index:03d}"
            args = [
                command,
                "-m",
                model,
                "-f",
                str(chunk),
                "-l",
                language,
                "-otxt",
                "-of",
                str(output_base),
                "-t",
                threads,
            ]
            if fast:
                args.extend(["-nf", "-bs", "1", "-bo", "1"])
            subprocess.run(args, check=True, capture_output=True, text=True)
            output_text = output_base.with_suffix(".txt")
            if output_text.exists():
                chunks.append(output_text.read_text(encoding="utf-8").strip())
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def transcribe_local(path: Path) -> str:
    command = os.environ.get("LOCAL_WHISPER_COMMAND", "/opt/anaconda3/bin/whisper")
    model = os.environ.get("LOCAL_WHISPER_MODEL", "medium")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        subprocess.run(
            [
                command,
                str(path),
                "--model",
                model,
                "--output_dir",
                str(tmpdir),
                "--output_format",
                "txt",
                "--verbose",
                "False",
                "--initial_prompt",
                "Wearable daily recording with Chinese, English, German, Italian, and background speech. Keep original languages.",
            ],
            check=True,
        )
        transcript_path = tmpdir / f"{path.stem}.txt"
        if not transcript_path.exists():
            matches = list(tmpdir.glob("*.txt"))
            if not matches:
                raise RuntimeError("Local Whisper did not produce a transcript file.")
            transcript_path = matches[0]
        return transcript_path.read_text(encoding="utf-8").strip()


def transcribe_openai(client: OpenAI, path: Path) -> str:
    model = os.environ.get("TRANSCRIBE_MODEL", "whisper-1")
    chunks: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for chunk in split_audio(path, Path(tmp)):
            with chunk.open("rb") as audio:
                text = client.audio.transcriptions.create(
                    model=model,
                    file=audio,
                    response_format="text",
                )
            chunks.append(str(text).strip())
    if not chunks:
        return ""
    return "\n\n".join(chunks).strip()


def transcribe(client: OpenAI | None, path: Path) -> str:
    use_local = os.environ.get("USE_LOCAL_WHISPER", "true").lower() in {"1", "true", "yes"}
    backend = os.environ.get("LOCAL_WHISPER_BACKEND", "whisper_cpp")
    if use_local and backend == "whisper_cpp" and whisper_cpp_available():
        return transcribe_whisper_cpp(path)
    if use_local and local_whisper_available():
        return transcribe_local(path)
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is missing and local Whisper is unavailable.")
    return transcribe_openai(client, path)


def summarize(client: OpenAI, date: str, transcript: str) -> str:
    language = os.environ.get("SUMMARY_LANGUAGE", "Chinese")
    model = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")
    detail_level = os.environ.get("SUMMARY_DETAIL_LEVEL", "detailed")
    min_chars = os.environ.get("SUMMARY_MIN_CHINESE_CHARS", "5000")
    speaker_instruction = ""
    if os.environ.get("SPEAKER_LABELS", "true").lower() in {"1", "true", "yes"}:
        speaker_instruction = """
Try to distinguish speakers from context and wording. Use labels such as:
- Me
- Speaker 1
- Speaker 2
- Unknown speaker

Do not invent identities. If the same person appears to continue speaking, keep the
same label. If speaker identity is uncertain, say so briefly.
""".strip()
    prompt = f"""
You are summarizing an all-day personal audio transcript for an Obsidian note.
Write in {language}.
The transcript may contain Chinese, English, German, Italian, and ambient public
speech. Preserve important names, phrases, decisions, and action items in their
original language when that is clearer.
The audio comes from a wearable recorder used by the note owner, so nearby speech by
the wearer is likely "Me" when the transcript context supports that.
{speaker_instruction}

Return detailed Markdown with these sections:
- Overview
- Detailed Timeline / Scenes
- Important Highlights
- Main Topic Deep Dive
- Secondary Signals
- Decisions / Commitments
- Action Items
- People / Speaker Notes
- Unclear or Noisy Parts

There is no fixed daily theme unless the user explicitly provides one for that
date. Infer the day's most important theme from the transcript and make
"Main Topic Deep Dive" about that theme. It may be medical, work, family,
logistics, finance, travel, relationships, or something else. Choose based on
importance, specificity, consequences, and future usefulness, not just frequency.
If two themes are clearly important, cover both in separate subsections. Use
"Secondary Signals" for meaningful but lower-priority context.
Only add extra topic-specific sections such as Health / Medical / Insurance,
Food / Family / Daily Life, Work, Travel, Finance, or Relationships when that
topic is genuinely important for this date. Do not include empty, routine, or
template-like sections just because they appeared in earlier notes.

If the inferred or user-provided important theme is a medical conversation,
especially a German doctor conversation, be much more detailed than usual.
Extract and explain:
- symptoms, history, diagnosis or suspected diagnosis
- examination findings, tests, measurements, imaging, lab work, and body parts
- medication names, dosage/frequency, treatment advice, restrictions, and warning signs
- appointments, referrals, follow-up timing, documents, insurance/payment details
- what the doctor said versus what Me or another person said
- exact German medical words when they are important, with a Chinese explanation
- uncertainties caused by noise or transcription errors

Rules:
- Do not invent links, filenames, people, diagnoses, or commitments.
- If a detail is unclear because the recording is noisy, say it is unclear.
- The archive paths will be added separately after your summary.
- This is a full-day wearable recording. Most speech may be blank, ambient,
  repeated, or unimportant. Focus only on meaningful personal context, decisions,
  commitments, health/medical details, logistics, money/insurance, names, and follow-ups.
- Ignore obvious transcription hallucinations, repeated filler, repeated goodbyes,
  repeated acknowledgements, background media, and fragments with no actionable meaning.
- Write at least twice as much detail as a short executive summary. Prefer specific
  observations, context, and quoted short phrases from the transcript when useful.
  Important Highlights should be detailed paragraphs or rich bullets, not one-line
  labels.
- The final summary body must target at least {min_chars} Chinese characters.
  Use complete paragraphs under each section, not only short bullets. For
  timeline scenes, explain what happened, why it mattered, what evidence in the
  transcript supports it, and what remained uncertain. If there is not enough
  meaningful material, still write a fuller note by separating confirmed facts,
  likely interpretations, weak signals, and noise/uncertainty instead of becoming
  terse.
- Keep action items practical and attach them to a likely person only when supported.
- If a theme appears repeatedly, explain how it developed across the day rather than
  listing it once.

Detail level: {detail_level}
Be selective, but do not be terse. The note should be useful for remembering the day.

Date: {date}

Transcript:
{transcript[:120000]}
""".strip()
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=6500,
    )
    return response.choices[0].message.content.strip()


def split_text_segments(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    raw_segments = re.split(r"(?<=[。！？!?；;.!?])\s+|[\r\n]+", normalized)
    segments: list[str] = []
    for segment in raw_segments:
        segment = segment.strip()
        if not segment:
            continue
        if len(segment) <= 420:
            segments.append(segment)
            continue
        for start in range(0, len(segment), 360):
            chunk = segment[start : start + 420].strip()
            if chunk:
                segments.append(chunk)
    return segments


def repeated_token_ratio(segment: str) -> float:
    tokens = TOKEN_RE.findall(segment.lower())
    if len(tokens) < 8:
        return 0.0
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return max(counts.values()) / len(tokens)


def unique_char_ratio(segment: str) -> float:
    letters = LETTER_RE.findall(segment.lower())
    if not letters:
        return 0.0
    return len(set(letters)) / len(letters)


def is_low_signal_segment(segment: str) -> bool:
    min_chars = int(os.environ.get("SUMMARY_MIN_SEGMENT_CHARS", "18"))
    stripped = segment.strip()
    if len(stripped) < min_chars:
        return True
    letters = LETTER_RE.findall(stripped)
    if len(letters) < 8:
        return True
    if stripped.count("...") >= 3 or stripped.count("…") >= 3:
        return True
    if repeated_token_ratio(stripped) > 0.42:
        return True
    if len(stripped) > 80 and unique_char_ratio(stripped) < 0.16:
        return True
    lowered = stripped.lower()
    low_value_phrases = [
        "subtitles",
        "字幕",
        "good good good",
        "ok ok ok",
        "tschüss tschüss",
        "ongelooflijk ongelooflijk",
    ]
    return any(phrase in lowered for phrase in low_value_phrases)


def segment_score(segment: str) -> int:
    score = 0
    keywords = [
        "医生",
        "医院",
        "检查",
        "保险",
        "报销",
        "MRT",
        "MRI",
        "CT",
        "EKG",
        "Ultraschall",
        "Herz",
        "Arzt",
        "Versicherung",
        "Krankenkasse",
        "Diagnose",
        "Befund",
        "Beschwerden",
        "Symptom",
        "Symptome",
        "Schmerzen",
        "Entzündung",
        "Medikament",
        "Tablette",
        "Dosierung",
        "Therapie",
        "Behandlung",
        "Überweisung",
        "Termin",
        "Hausarzt",
        "Facharzt",
        "Praxis",
        "Rezept",
        "Blut",
        "Blutdruck",
        "Röntgen",
        "Notfall",
        "Operation",
        "Narkose",
        "Allianz",
        "Montag",
        "下周",
        "明天",
        "需要",
        "联系",
        "费用",
    ]
    lowered = segment.lower()
    for keyword in keywords:
        if keyword.lower() in lowered:
            score += 5
    if re.search(r"\b\d{1,2}[:.]\d{2}\b|\b\d+\s*(euro|欧|元|天|小时|minutes|min)\b", lowered):
        score += 3
    return score


def filter_transcript_for_summary(text: str) -> tuple[str, dict[str, int]]:
    max_chars = int(os.environ.get("SUMMARY_MAX_TRANSCRIPT_CHARS", "30000"))
    segments = split_text_segments(text)
    if os.environ.get("SUMMARY_FILTER_NOISE", "true").lower() not in {"1", "true", "yes"}:
        trimmed = text[:max_chars]
        return trimmed, {
            "segments_total": len(segments),
            "segments_kept": len(segments),
            "segments_dropped": 0,
            "chars_original": len(text),
            "chars_kept": len(trimmed),
        }

    kept: list[str] = []
    dropped = 0
    for segment in segments:
        if is_low_signal_segment(segment):
            dropped += 1
            continue
        kept.append(segment)

    if os.environ.get("SUMMARY_AGGRESSIVE_FILTER", "true").lower() in {"1", "true", "yes"}:
        min_signal_chars = int(os.environ.get("SUMMARY_MIN_SIGNAL_CHARS", "2500"))
        signal_segments = [segment for segment in kept if segment_score(segment) > 0]
        if sum(len(segment) for segment in signal_segments) >= min_signal_chars:
            dropped += len(kept) - len(signal_segments)
            kept = signal_segments

    scored = sorted(enumerate(kept), key=lambda item: (-segment_score(item[1]), item[0]))
    selected: list[tuple[int, str]] = []
    used = 0
    for index, segment in scored:
        extra = len(segment) + 2
        if used + extra > max_chars:
            continue
        selected.append((index, segment))
        used += extra
    selected.sort(key=lambda item: item[0])
    filtered = "\n".join(segment for _, segment in selected).strip()
    return filtered, {
        "segments_total": len(segments),
        "segments_kept": len(selected),
        "segments_dropped": dropped + max(0, len(kept) - len(selected)),
        "chars_original": len(text),
        "chars_kept": len(filtered),
    }


def pending_summary() -> str:
    return """## Summary

Summary pending. Add `OPENAI_API_KEY` to `.env` and rerun summarization, or summarize this transcript manually.

## Important Highlights

Pending.

## Speaker Notes

Pending. Whisper transcription does not provide reliable speaker diarization by itself.

## Decisions / Commitments

Pending.

## Action Items

Pending.

## People / Topics

Pending.

## Raw Transcript Link

See archive paths below.
"""


def empty_filter_stats() -> dict[str, int]:
    return {
        "segments_total": 0,
        "segments_kept": 0,
        "segments_dropped": 0,
        "chars_original": 0,
        "chars_kept": 0,
    }


def combined_transcript(entries: list[dict], filtered: bool = False) -> tuple[str, dict[str, int]]:
    parts: list[str] = []
    aggregate = empty_filter_stats()
    for entry in entries:
        transcript_path = Path(entry["transcript_archive"])
        if not transcript_path.exists():
            continue
        label = entry.get("original_name") or transcript_path.name
        transcript = transcript_path.read_text(encoding="utf-8").strip()
        if filtered:
            transcript, stats = filter_transcript_for_summary(transcript)
        else:
            stats = {
                "segments_total": len(split_text_segments(transcript)),
                "segments_kept": len(split_text_segments(transcript)),
                "segments_dropped": 0,
                "chars_original": len(transcript),
                "chars_kept": len(transcript),
            }
        for key, value in stats.items():
            aggregate[key] += value
        if transcript:
            parts.append(f"## Recording: {label}\n\n{transcript}")
    return "\n\n".join(parts).strip(), aggregate


def write_daily_note(vault: Path, date: str, entries: list[dict], summary: str, filter_stats: dict[str, int] | None = None) -> Path:
    vault.mkdir(parents=True, exist_ok=True)
    note = vault / f"{date}.md"
    audio_lines = "\n".join(f"- `{entry['audio_archive']}`" for entry in entries)
    transcript_lines = "\n".join(f"- `{entry['transcript_archive']}`" for entry in entries)
    year, month, _ = date.split("-")
    local_topics = ["[[每日录音]]"]
    summary_input = ""
    if filter_stats:
        summary_input = f"""
## Summary Input

- Full transcript characters: {filter_stats.get("chars_original", 0)}
- Characters sent to summary: {filter_stats.get("chars_kept", 0)}
- Segments kept/dropped: {filter_stats.get("segments_kept", 0)}/{filter_stats.get("segments_dropped", 0)}
"""
    body = f"""---
date: {date}
title: {date} 全天 AI 录音
source: soundcore
recordings: {len(entries)}
tags:
  - 全天AI录音
  - daily-note
  - wearable-recording
  - {year}
  - {year}-{month}
---

# {date} 全天 AI 录音

> [!info] Daily wearable recording
> Full audio and transcripts are archived on NAS. This note summarizes only the filtered high-signal parts.

{summary}

## Obsidian Links

- Month: [[{year}-{month}]]
- Year: [[{year}]]
- Topics: {" ".join(local_topics)}

{summary_input}

## Archive

### Audio

{audio_lines}

### Full Transcripts

{transcript_lines}
"""
    note.write_text(body, encoding="utf-8")
    return note


def rebuild_daily_note(client: OpenAI | None, manifest: dict, date: str) -> Path | None:
    entries = [
        entry
        for entry in manifest.get("files", {}).values()
        if entry.get("date") == date
    ]
    if not entries:
        return None
    vault = Path(os.environ.get("OBSIDIAN_VAULT", str(ROOT))).expanduser()
    transcript, filter_stats = combined_transcript(entries, filtered=True)
    if client is not None and transcript:
        print(f"Rebuilding daily summary for {date} from {len(entries)} recording(s)...")
        summary = summarize(client, date, transcript)
    else:
        summary = pending_summary()
    note = write_daily_note(vault, date, entries, summary, filter_stats)
    for entry in entries:
        entry["note"] = str(note)
    save_manifest(manifest)
    return note


def process_file(client: OpenAI | None, path: Path, manifest: dict) -> None:
    file_key = file_digest(path)
    if file_key in manifest.get("files", {}):
        print(f"Skipping already processed file: {path.name}")
        path.unlink()
        return

    date = recording_date(path)
    vault = Path(os.environ.get("OBSIDIAN_VAULT", str(ROOT))).expanduser()
    archive_root = Path(os.environ.get("NAS_ARCHIVE") or str(ROOT / "archive")).expanduser()
    ensure_archive_available(archive_root)
    audio_dir = archive_root / "recordings" / date[:4] / date
    transcript_dir = archive_root / "transcripts" / date[:4] / date
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)

    print(f"Transcribing {path.name} as {date}...")
    transcript = transcribe(client, path)

    transcript_archive = transcript_dir / f"{path.stem}.txt"
    transcript_archive.write_text(transcript + "\n", encoding="utf-8")

    audio_archive = audio_dir / path.name
    if not audio_archive.exists():
        shutil.move(str(path), str(audio_archive))
    else:
        path.unlink()

    manifest.setdefault("files", {})[file_key] = {
        "date": date,
        "original_name": path.name,
        "audio_archive": str(audio_archive),
        "transcript_archive": str(transcript_archive),
        "note": "",
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
    manifest.setdefault("dates", {}).setdefault(date, []).append(str(audio_archive))
    save_manifest(manifest)
    note = rebuild_daily_note(client, manifest, date)
    print(f"Done: {note}")


def main() -> None:
    load_env()
    use_local = os.environ.get("USE_LOCAL_WHISPER", "true").lower() in {"1", "true", "yes"}
    can_transcribe_locally = use_local and local_whisper_available()
    if not os.environ.get("OPENAI_API_KEY") and not can_transcribe_locally:
        raise SystemExit("OPENAI_API_KEY is missing and local Whisper is unavailable.")
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is missing. I can transcribe locally, but summary generation will be skipped.")
    INBOX.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    client = OpenAI() if os.environ.get("OPENAI_API_KEY") else None
    files = sorted(path for path in INBOX.iterdir() if path.suffix.lower() in AUDIO_EXTENSIONS)
    if not files:
        print(f"No audio files found in {INBOX}")
        if os.environ.get("REBUILD_EXISTING_DAILY", "false").lower() in {"1", "true", "yes"}:
            for date in sorted(manifest.get("dates", {})):
                note = rebuild_daily_note(client, manifest, date)
                if note:
                    print(f"Rebuilt: {note}")
        return
    for path in files:
        process_file(client, path, manifest)


if __name__ == "__main__":
    main()
