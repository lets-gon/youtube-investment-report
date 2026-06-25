#!/usr/bin/env python3
"""Run caption-based summaries, falling back to audio transcription when captions are unavailable."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import urllib.request
from pathlib import Path

import caption_report as cr
import youtube_report as yr


ORIGINAL_EVIDENCE_LEVEL = cr.evidence_level
ENABLE_AUDIO_TRANSCRIPTION = os.getenv("ENABLE_AUDIO_TRANSCRIPTION", "1") != "0"
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
MAX_AUDIO_FILE_MB = int(os.getenv("MAX_AUDIO_FILE_MB", "24"))
AUDIO_TRANSCRIPTION_TIMEOUT = int(os.getenv("AUDIO_TRANSCRIPTION_TIMEOUT", "180"))


def transcribe_audio_file(path: Path, api_key: str) -> str:
    boundary = "codex_audio_boundary"
    body = bytearray()
    for name, value in [("model", OPENAI_TRANSCRIBE_MODEL), ("response_format", "json")]:
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode())
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions",
        data=bytes(body),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=AUDIO_TRANSCRIPTION_TIMEOUT, context=yr.HTTPS_CONTEXT) as res:
        data = json.loads(res.read().decode("utf-8"))
    return data.get("text", "")


def fetch_video_audio_transcript(video_id: str, prior_error: str = "") -> dict:
    if not ENABLE_AUDIO_TRANSCRIPTION:
        return {"text": "", "source": "none", "language": "", "error": prior_error or "audio transcription disabled"}
    api_key = re.sub(r"\s+", "", os.getenv("OPENAI_API_KEY", ""))
    if not api_key:
        return {"text": "", "source": "none", "language": "", "error": prior_error or "OPENAI_API_KEY is not set for audio transcription"}
    if not shutil.which("yt-dlp"):
        return {"text": "", "source": "none", "language": "", "error": prior_error or "yt-dlp not installed"}
    with tempfile.TemporaryDirectory() as temp_dir:
        command = [
            "yt-dlp",
            "--no-playlist",
            "--max-filesize",
            f"{MAX_AUDIO_FILE_MB}M",
            "-f",
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "-o",
            str(Path(temp_dir) / "%(id)s.%(ext)s"),
            f"https://youtu.be/{video_id}",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
        except Exception as exc:
            return {"text": "", "source": "none", "language": "", "error": f"{prior_error}; audio download {type(exc).__name__}: {exc}"}
        files = [path for path in Path(temp_dir).glob("*") if path.is_file()]
        if not files:
            detail = cr.compact((completed.stderr or completed.stdout or "").strip(), 280)
            return {"text": "", "source": "none", "language": "", "error": "; ".join(x for x in [prior_error, detail or "audio download produced no file"] if x)}
        audio_path = max(files, key=lambda path: path.stat().st_size)
        if audio_path.stat().st_size > MAX_AUDIO_FILE_MB * 1024 * 1024:
            return {"text": "", "source": "none", "language": "", "error": f"{prior_error}; audio file exceeded {MAX_AUDIO_FILE_MB}MB"}
        try:
            text = transcribe_audio_file(audio_path, api_key)
        except Exception as exc:
            return {"text": "", "source": "none", "language": "", "error": f"{prior_error}; audio transcription {type(exc).__name__}: {exc}"}
        if not text:
            return {"text": "", "source": "none", "language": "", "error": f"{prior_error}; audio transcription returned empty text"}
        return {"text": cr.compact(text, cr.MAX_TRANSCRIPT_CHARS), "source": "audio_transcription", "language": "", "error": ""}


def fetch_video_transcript_with_ytdlp(video_id: str, prior_error: str = "") -> dict:
    if not shutil.which("yt-dlp"):
        return fetch_video_audio_transcript(video_id, prior_error or "yt-dlp not installed")
    with tempfile.TemporaryDirectory() as temp_dir:
        command = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            ",".join(cr.TRANSCRIPT_LANGUAGES),
            "--sub-format",
            "json3/vtt/srv3",
            "-o",
            str(Path(temp_dir) / "%(id)s.%(ext)s"),
            f"https://youtu.be/{video_id}",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        except Exception as exc:
            return fetch_video_audio_transcript(video_id, f"{prior_error}; yt-dlp {type(exc).__name__}: {exc}")
        for path in sorted(Path(temp_dir).glob("*"), key=cr.caption_file_score):
            if path.suffix not in {".json3", ".vtt", ".srv3"}:
                continue
            text = cr.transcript_text_from_file(path)
            if text:
                return {"text": text, "source": "caption", "language": cr.caption_language_from_name(path.name), "error": ""}
        detail = cr.compact((completed.stderr or completed.stdout or "").strip(), 280)
        return fetch_video_audio_transcript(video_id, "; ".join(x for x in [prior_error, detail] if x))


def evidence_level(source: str, description: str = "") -> str:
    if source == "audio_transcription":
        return "음성 전사 기반"
    return ORIGINAL_EVIDENCE_LEVEL(source, description)


def build_prompt(payload: dict) -> str:
    data = cr.prompt_payload(payload)
    return textwrap.dedent(
        f"""
        아래 JSON은 YouTube RSS, 자막/자동자막, 자막이 없을 때의 음성 전사, 커뮤니티 게시물에서 수집한 최근 {yr.WINDOW_HOURS}시간 투자 콘텐츠 데이터다.
        Lillys AI처럼 영상을 보지 않아도 흐름을 이해할 수 있는 한국어 투자 요약 JSON만 작성해라.

        원칙:
        - transcript가 있으면 반드시 transcript를 최우선 근거로 사용한다.
        - transcript_source가 audio_transcription이면 자막이 아니라 음성 전사 기반임을 source_basis에 표시한다.
        - transcript가 없으면 description, content, title 순서로만 요약하고 근거 수준을 source_basis에 명시한다.
        - 제목만 보고 영상 내용을 추측하지 않는다.
        - 각 영상은 배경 -> 핵심 논리 -> 결론 -> 투자자가 확인할 점이 이어지게 쓴다.
        - item_summaries.summary는 1200자 이내, narrative_flow는 3~5문장, key_points는 3~6개, investor_takeaway는 2~4개로 쓴다.
        - Fact/Opnion/Insight/Recommendation/Risk를 구분하고, 투자 조언은 단정 대신 리스크 점검 중심으로 쓴다.
        - 고등학생이 모를 용어는 terms에 쉽게 설명한다.
        - 응답은 마크다운 없이 유효한 JSON 객체 하나만 반환한다.

        데이터:
        {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}
        """
    ).strip()


def main() -> int:
    cr.fetch_video_transcript_with_ytdlp = fetch_video_transcript_with_ytdlp
    cr.evidence_level = evidence_level
    cr.build_prompt = build_prompt
    return cr.main()


if __name__ == "__main__":
    raise SystemExit(main())
