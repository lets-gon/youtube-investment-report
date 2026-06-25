#!/usr/bin/env python3
"""Run the YouTube report with caption-first, Lillys-style summaries."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import textwrap
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from html import escape, unescape
from pathlib import Path

import youtube_report as yr


ORIGINAL_PROMPT_PAYLOAD = yr.prompt_payload
ORIGINAL_REPORT_JSON_SCHEMA = yr.report_json_schema
ORIGINAL_SOURCE_ITEMS = yr.source_items
ORIGINAL_NORMALIZE_OUTPUTS = yr.normalize_outputs
ORIGINAL_FALLBACK_STRUCTURED_REPORT = yr.fallback_structured_report
ORIGINAL_SAVE_SQLITE = yr.save_sqlite
ORIGINAL_RENDER_TELEGRAM_MESSAGES = yr.render_telegram_messages

MAX_TRANSCRIPT_CHARS = int(os.getenv("MAX_TRANSCRIPT_CHARS", "12000"))
PROMPT_TRANSCRIPT_CHARS = int(os.getenv("PROMPT_TRANSCRIPT_CHARS", "5000"))
TRANSCRIPT_LANGUAGES = [value.strip() for value in os.getenv("TRANSCRIPT_LANGUAGES", "ko,en").split(",") if value.strip()]


def compact(value: str, limit: int = 700) -> str:
    return " ".join((value or "").split())[:limit]


def caption_track_url(video_id: str, track: dict, *, fmt: str = "json3") -> str:
    params = {"v": video_id, "fmt": fmt, "lang": track.get("lang_code", "")}
    if track.get("name"):
        params["name"] = track["name"]
    if track.get("kind"):
        params["kind"] = track["kind"]
    return "https://video.google.com/timedtext?" + urllib.parse.urlencode(params)


def caption_track_score(track: dict) -> tuple[int, int, str]:
    lang = track.get("lang_code", "")
    base = lang.split("-")[0]
    try:
        lang_rank = TRANSCRIPT_LANGUAGES.index(lang)
    except ValueError:
        try:
            lang_rank = TRANSCRIPT_LANGUAGES.index(base)
        except ValueError:
            lang_rank = len(TRANSCRIPT_LANGUAGES) + 5
    return (lang_rank, 1 if track.get("kind") == "asr" else 0, track.get("name", ""))


def caption_language_from_name(name: str) -> str:
    for part in name.split("."):
        if part in TRANSCRIPT_LANGUAGES or part.split("-")[0] in TRANSCRIPT_LANGUAGES:
            return part
    return ""


def caption_file_score(path: Path) -> tuple[int, str]:
    language = caption_language_from_name(path.name)
    base = language.split("-")[0]
    try:
        rank = TRANSCRIPT_LANGUAGES.index(language)
    except ValueError:
        try:
            rank = TRANSCRIPT_LANGUAGES.index(base)
        except ValueError:
            rank = len(TRANSCRIPT_LANGUAGES) + 5
    return (rank, path.name)


def transcript_text_from_json3(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    chunks = []
    for event in data.get("events", []):
        for segment in event.get("segs", []) or []:
            text = segment.get("utf8", "")
            if text.strip():
                chunks.append(text)
    return compact(" ".join(chunks), MAX_TRANSCRIPT_CHARS)


def transcript_text_from_xml(raw: str) -> str:
    try:
        root = ET.fromstring(raw or "<transcript />")
    except ET.ParseError:
        return ""
    return compact(" ".join(unescape(node.text or "") for node in root.findall("text")), MAX_TRANSCRIPT_CHARS)


def transcript_text_from_vtt(raw: str) -> str:
    chunks = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line == "WEBVTT" or "-->" in line:
            continue
        if line.startswith(("Kind:", "Language:", "NOTE", "STYLE", "REGION")):
            continue
        line = unescape(re.sub(r"<[^>]+>", "", line)).strip()
        if line:
            chunks.append(line)
    return compact(" ".join(chunks), MAX_TRANSCRIPT_CHARS)


def transcript_text_from_file(path: Path) -> str:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".json3":
        return transcript_text_from_json3(raw)
    if path.suffix == ".vtt":
        return transcript_text_from_vtt(raw)
    if path.suffix == ".srv3":
        return transcript_text_from_xml(raw)
    return ""


def fetch_video_transcript_with_ytdlp(video_id: str, prior_error: str = "") -> dict:
    if not shutil.which("yt-dlp"):
        return {"text": "", "source": "none", "language": "", "error": prior_error or "yt-dlp not installed"}
    with tempfile.TemporaryDirectory() as temp_dir:
        command = [
            "yt-dlp",
            "--skip-download",
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            ",".join(TRANSCRIPT_LANGUAGES),
            "--sub-format",
            "json3/vtt/srv3",
            "-o",
            str(Path(temp_dir) / "%(id)s.%(ext)s"),
            f"https://youtu.be/{video_id}",
        ]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        except Exception as exc:
            return {"text": "", "source": "none", "language": "", "error": f"{prior_error}; yt-dlp {type(exc).__name__}: {exc}"}
        for path in sorted(Path(temp_dir).glob("*"), key=caption_file_score):
            if path.suffix not in {".json3", ".vtt", ".srv3"}:
                continue
            text = transcript_text_from_file(path)
            if text:
                return {"text": text, "source": "caption", "language": caption_language_from_name(path.name), "error": ""}
        detail = compact((completed.stderr or completed.stdout or "").strip(), 280)
        return {"text": "", "source": "none", "language": "", "error": "; ".join(x for x in [prior_error, detail] if x)}


def fetch_video_transcript(video_id: str) -> dict:
    if not video_id:
        return {"text": "", "source": "none", "language": "", "error": "missing video id"}
    try:
        track_xml = yr.request_text(f"https://video.google.com/timedtext?type=list&v={urllib.parse.quote(video_id)}", timeout=15)
        root = ET.fromstring(track_xml or "<transcript_list />")
        tracks = [track.attrib for track in root.findall("track")]
        if not tracks:
            return fetch_video_transcript_with_ytdlp(video_id, "no caption track")
        selected = sorted(tracks, key=caption_track_score)[0]
        raw = yr.request_text(caption_track_url(video_id, selected), timeout=20)
        text = transcript_text_from_json3(raw)
        if not text:
            raw = yr.request_text(caption_track_url(video_id, selected, fmt=""), timeout=20)
            text = transcript_text_from_xml(raw)
        source = "auto_caption" if selected.get("kind") == "asr" else "caption"
        return {"text": compact(text, MAX_TRANSCRIPT_CHARS), "source": source if text else "none", "language": selected.get("lang_code", ""), "error": "" if text else "empty caption text"}
    except Exception as exc:
        return fetch_video_transcript_with_ytdlp(video_id, f"{type(exc).__name__}: {exc}")


def evidence_level(source: str, description: str = "") -> str:
    if source == "caption":
        return "자막 기반"
    if source == "auto_caption":
        return "자동자막 기반"
    if description:
        return "설명란 기반"
    return "제목만 확인"


def fetch_videos(channel: yr.Channel, start: datetime, end: datetime) -> list[dict]:
    root = ET.fromstring(yr.request_text(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.channel_id}"))
    videos = []
    for entry in root.findall("atom:entry", yr.ATOM_NS):
        published = entry.findtext("atom:published", default="", namespaces=yr.ATOM_NS)
        if not published:
            continue
        published_at = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(yr.UTC)
        if not (start <= published_at <= end):
            continue
        title = entry.findtext("atom:title", default="", namespaces=yr.ATOM_NS)
        if channel.kind == "부동산" and yr.is_guest_video(title):
            continue
        video_id = entry.findtext("yt:videoId", default="", namespaces=yr.ATOM_NS)
        group = entry.find("media:group", yr.ATOM_NS)
        description = group.findtext("media:description", default="", namespaces=yr.ATOM_NS) if group is not None else ""
        transcript = fetch_video_transcript(video_id)
        videos.append(
            {
                "source_id": f"yt_{video_id}",
                "item_type": "video",
                "video_id": video_id,
                "channel": channel.name,
                "kind": channel.kind,
                "title": title,
                "url": f"https://youtu.be/{video_id}",
                "published_at": published_at.isoformat(),
                "published_kst": published_at.astimezone(yr.KST).strftime("%Y-%m-%d %H:%M KST"),
                "description": compact(description),
                "transcript": transcript.get("text", ""),
                "transcript_source": transcript.get("source", "none"),
                "transcript_language": transcript.get("language", ""),
                "transcript_error": transcript.get("error", ""),
                "evidence_level": evidence_level(transcript.get("source", "none"), description),
            }
        )
    return videos


def prompt_payload(payload: dict) -> dict:
    data = ORIGINAL_PROMPT_PAYLOAD(payload)
    for video, raw in zip(data["videos"], payload["videos"]):
        video.update(
            {
                "evidence_level": raw.get("evidence_level", "제목만 확인"),
                "transcript_source": raw.get("transcript_source", "none"),
                "transcript_language": raw.get("transcript_language", ""),
                "transcript_error": compact(raw.get("transcript_error", ""), 160),
                "transcript": compact(raw.get("transcript", ""), PROMPT_TRANSCRIPT_CHARS),
            }
        )
    for post in data["asset_x2_posts"]:
        post["evidence_level"] = "게시물 원문 기반"
        post["transcript_source"] = "community_post"
    return data


def build_prompt(payload: dict) -> str:
    data = prompt_payload(payload)
    return textwrap.dedent(
        f"""
        아래 JSON은 YouTube RSS, 자막/자동자막, 커뮤니티 게시물에서 수집한 최근 {yr.WINDOW_HOURS}시간 투자 콘텐츠 데이터다.
        Lillys AI처럼 영상을 보지 않아도 흐름을 이해할 수 있는 한국어 투자 요약 JSON만 작성해라.

        원칙:
        - transcript가 있으면 반드시 transcript를 최우선 근거로 사용한다.
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


def report_json_schema() -> dict:
    schema = ORIGINAL_REPORT_JSON_SCHEMA()
    item_schema = schema["properties"]["item_summaries"]["items"]
    item_schema["properties"].update(
        {
            "source_basis": {"type": "string"},
            "narrative_flow": {"type": "array", "items": {"type": "string"}},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "investor_takeaway": {"type": "array", "items": {"type": "string"}},
        }
    )
    for key in ["source_basis", "narrative_flow", "key_points", "investor_takeaway"]:
        if key not in item_schema["required"]:
            item_schema["required"].insert(1, key)
    return schema


def source_items(payload: dict) -> list[dict]:
    rows = ORIGINAL_SOURCE_ITEMS(payload)
    raw_by_id = {item["source_id"]: item for item in payload["videos"] + payload["asset_x2_posts"]}
    for row in rows:
        raw = raw_by_id.get(row["item_id"], {})
        row.update(
            {
                "transcript": raw.get("transcript") or raw.get("content", ""),
                "transcript_source": raw.get("transcript_source", "community_post" if row["item_type"] == "community_post" else "none"),
                "transcript_language": raw.get("transcript_language", "ko" if row["item_type"] == "community_post" else ""),
                "evidence_level": raw.get("evidence_level", "게시물 원문 기반" if row["item_type"] == "community_post" else "제목만 확인"),
            }
        )
    return rows


def normalize_outputs(payload: dict, structured: dict):
    items, insights, keywords, terms = ORIGINAL_NORMALIZE_OUTPUTS(payload, structured)
    ai_by_id = {item.get("source_id"): item for item in structured.get("item_summaries", [])}
    for item in items:
        ai = ai_by_id.get(item["item_id"], {})
        item.update(
            {
                "source_basis": ai.get("source_basis", item.get("evidence_level", "")),
                "narrative_flow": ai.get("narrative_flow", []) or [],
                "key_points": ai.get("key_points", []) or [],
                "investor_takeaway": ai.get("investor_takeaway", []) or [],
            }
        )
    return items, insights, keywords, terms


def fallback_structured_report(payload: dict, reason: str) -> dict:
    structured = ORIGINAL_FALLBACK_STRUCTURED_REPORT(payload, reason)
    raw_items = {item["item_id"]: item for item in source_items(payload)}
    for summary in structured.get("item_summaries", []):
        item = raw_items.get(summary.get("source_id"), {})
        basis = item.get("evidence_level", "제목/설명란 기반")
        summary["source_basis"] = basis
        summary["narrative_flow"] = [summary.get("summary", ""), "OpenAI 오류로 상세 흐름 분석은 보류됐다.", "다음 정상 실행 때 자막 기반 상세 요약으로 갱신된다."]
        summary["key_points"] = [item.get("title", "수집 항목")]
        summary["investor_takeaway"] = ["상세 투자 판단은 정상 요약 또는 원문 확인 후 진행한다."]
    return structured


EXTRA_ITEM_COLUMNS = {
    "transcript": "ALTER TABLE items ADD COLUMN transcript TEXT",
    "transcript_source": "ALTER TABLE items ADD COLUMN transcript_source TEXT",
    "transcript_language": "ALTER TABLE items ADD COLUMN transcript_language TEXT",
    "evidence_level": "ALTER TABLE items ADD COLUMN evidence_level TEXT",
    "source_basis": "ALTER TABLE items ADD COLUMN source_basis TEXT",
    "narrative_flow": "ALTER TABLE items ADD COLUMN narrative_flow TEXT",
    "key_points": "ALTER TABLE items ADD COLUMN key_points TEXT",
    "investor_takeaway": "ALTER TABLE items ADD COLUMN investor_takeaway TEXT",
}


def save_sqlite(db_path: Path, payload: dict, structured: dict, items: list[dict], insights: list[dict], keywords: list[dict], terms: list[dict], html_path: Path, telegram_path: Path) -> None:
    ORIGINAL_SAVE_SQLITE(db_path, payload, structured, items, insights, keywords, terms, html_path, telegram_path)
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        for column, statement in EXTRA_ITEM_COLUMNS.items():
            if column not in columns:
                conn.execute(statement)
        for item in items:
            conn.execute(
                """
                UPDATE items
                SET transcript = ?, transcript_source = ?, transcript_language = ?, evidence_level = ?,
                    source_basis = ?, narrative_flow = ?, key_points = ?, investor_takeaway = ?
                WHERE item_id = ?
                """,
                (
                    item.get("transcript", ""),
                    item.get("transcript_source", ""),
                    item.get("transcript_language", ""),
                    item.get("evidence_level", ""),
                    item.get("source_basis", ""),
                    json.dumps(item.get("narrative_flow", []) or [], ensure_ascii=False),
                    json.dumps(item.get("key_points", []) or [], ensure_ascii=False),
                    json.dumps(item.get("investor_takeaway", []) or [], ensure_ascii=False),
                    item["item_id"],
                ),
            )


def as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
            if isinstance(loaded, list):
                return [str(item) for item in loaded if str(item).strip()]
        except json.JSONDecodeError:
            return [value]
    return []


def list_html(values) -> str:
    rows = as_list(values)
    return "<ul>" + "".join(f"<li>{escape(row)}</li>" for row in rows) + "</ul>" if rows else "<p>정리된 항목 없음</p>"


def render_full_html(payload: dict, structured: dict, items: list[dict], insights: list[dict], keywords: list[dict], terms: list[dict]) -> str:
    report = structured.get("report", {})
    item_html = []
    for item in items:
        item_html.append(
            f"""
            <article class="item">
              <h3>{escape(item['channel_name'])} · {escape(item['title'])}</h3>
              <p class="meta">{escape(item['category'])} / {escape(item.get('published_kst') or '')} / 요약 근거: {escape(item.get('source_basis') or item.get('evidence_level') or '')}</p>
              <h4>핵심 요약</h4><p>{escape(item.get('summary') or item.get('raw_description') or '수집된 요약 없음')}</p>
              <h4>상세 흐름</h4>{list_html(item.get('narrative_flow'))}
              <h4>주요 포인트</h4>{list_html(item.get('key_points'))}
              <h4>투자자 체크</h4>{list_html(item.get('investor_takeaway'))}
              <details><summary>수집 근거 원문 보기</summary><p>{escape(compact(item.get('transcript') or item.get('raw_description') or '', 3000))}</p></details>
              <p><a href="{escape(item.get('url') or '')}">원문 보기</a></p>
            </article>
            """
        )
    section_html = "".join(
        f"<section><h2>{escape(section.get('title', ''))}</h2>{''.join(f'<p>{escape(str(p))}</p>' for p in section.get('paragraphs', []) or [])}</section>"
        for section in structured.get("sections", []) or []
    )
    insight_html = "".join(f"<li><b>{escape(i['insight_type'])}</b> · {escape(i.get('source_channel') or '')}: {escape(i['content'])}</li>" for i in insights if i.get("content"))
    keyword_html = "".join(f"<li>{escape(k.get('keyword', ''))} <span>{escape(k.get('category', ''))}</span></li>" for k in keywords)
    term_html = "".join(f"<dt>{escape(t['term'])}</dt><dd>{escape(t['explanation'])}</dd>" for t in terms)
    return textwrap.dedent(
        f"""\
        <!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{escape(report.get('title') or payload['report_id'])}</title>
        <style>
        body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#18212f;background:#f5f7fb;line-height:1.65}}
        main{{max-width:980px;margin:0 auto;padding:32px 18px 56px}} header{{padding:28px 0;border-bottom:3px solid #27364f}}
        h1{{font-size:28px;margin:0 0 10px}} h2{{margin-top:34px;padding-top:8px;border-top:1px solid #d9dfeb}} h3{{margin-bottom:4px}} h4{{margin:18px 0 6px}}
        section,.item{{background:white;border:1px solid #dde3ee;border-radius:8px;padding:18px;margin:16px 0}} .meta{{color:#667085;font-size:14px}}
        details{{margin-top:14px;background:#f7f9fc;border:1px solid #e5eaf3;border-radius:6px;padding:10px 12px}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}} a{{color:#1957b8}} dt{{font-weight:700;margin-top:12px}} dd{{margin-left:0}}
        </style></head><body><main>
        <header><h1>{escape(report.get('title') or payload['report_id'])}</h1><p>{escape(payload['window']['start_kst'])} ~ {escape(payload['window']['end_kst'])}</p><p>{escape(report.get('overall_summary', ''))}</p></header>
        {section_html}
        <section><h2>채널별 상세</h2>{''.join(item_html) or '<p>수집된 항목이 없습니다.</p>'}</section>
        <section><h2>Fact / Opnion / Insight / Recommendation</h2><ul>{insight_html}</ul></section>
        <section class="grid"><div><h2>반복 키워드</h2><ul>{keyword_html}</ul></div><div><h2>용어 설명</h2><dl>{term_html}</dl></div></section>
        </main></body></html>
        """
    )


def render_telegram_messages(payload: dict, structured: dict, html_path: Path) -> list[str]:
    messages = ORIGINAL_RENDER_TELEGRAM_MESSAGES(payload, structured, html_path)
    item_lookup = {item["item_id"]: item for item in source_items(payload)}
    summaries = structured.get("item_summaries", []) or []
    detail_messages = []
    for category, title in [("주식", "주식 콘텐츠 상세"), ("부동산", "부동산 콘텐츠 상세")]:
        blocks = [f"<b>{escape(title)}</b>"]
        for summary in summaries:
            item = item_lookup.get(summary.get("source_id"))
            if not item or item["category"] != category:
                continue
            body = compact(summary.get("summary") or item.get("raw_description") or "", 1100)
            block = [
                f"<b>{escape(item['channel_name'])}</b>",
                f"요약 근거: {escape(summary.get('source_basis') or item.get('evidence_level') or '')}",
                f"제목: {escape(compact(item.get('title', ''), 180))}",
                f"영상 내용 요약:\n{escape(body)}",
            ]
            if summary.get("key_points"):
                block.append(escape("핵심 포인트:\n" + "\n".join(f"- {compact(point, 180)}" for point in summary["key_points"][:4])))
            takeaways = summary.get("investor_takeaway") or summary.get("recommendations") or []
            if takeaways:
                block.append(escape("투자자 체크:\n" + "\n".join(f"- {compact(point, 180)}" for point in takeaways[:3])))
            if item.get("url"):
                block.append(f'<a href="{escape(item["url"])}">원문 보기</a>')
            candidate = "\n\n".join(block)
            if len("\n\n".join(blocks + [candidate])) > 3400 and len(blocks) > 1:
                detail_messages.append("\n\n".join(blocks))
                blocks = [f"<b>{escape(title)}</b>", candidate]
            else:
                blocks.append(candidate)
        detail_messages.append("\n\n".join(blocks))
    return [messages[0], *detail_messages, messages[-1]]


def install_patches() -> None:
    yr.fetch_videos = fetch_videos
    yr.prompt_payload = prompt_payload
    yr.build_prompt = build_prompt
    yr.report_json_schema = report_json_schema
    yr.source_items = source_items
    yr.normalize_outputs = normalize_outputs
    yr.fallback_structured_report = fallback_structured_report
    yr.save_sqlite = save_sqlite
    yr.render_full_html = render_full_html
    yr.render_telegram_messages = render_telegram_messages


def main() -> int:
    install_patches()
    return yr.main()


if __name__ == "__main__":
    raise SystemExit(main())
