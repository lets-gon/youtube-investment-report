#!/usr/bin/env python3
"""Create a Korean YouTube investment digest and optionally send it to Telegram."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "24"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
MAX_COMMUNITY_POSTS = int(os.getenv("MAX_COMMUNITY_POSTS", "8"))
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "out"))


@dataclass(frozen=True)
class Channel:
    name: str
    kind: str
    url: str
    channel_id: str
    include_community: bool = False


CHANNELS = [
    Channel(
        "힐링여행자",
        "주식",
        "https://www.youtube.com/@%ED%9E%90%EB%A7%81%EC%97%AC%ED%96%89%EC%9E%90",
        "UCHpGooMnVgnILywqrpqvZcQ",
    ),
    Channel("asset.x2", "주식", "https://www.youtube.com/@asset.x2", "UCpTC-SMFjA3EDRhZIKOcKuQ", True),
    Channel("잼투리", "주식", "https://www.youtube.com/@jamtoori", "UCz0vfs-XRMrpG78WtMVE1mg"),
    Channel("한경 글로벌마켓", "주식", "https://www.youtube.com/@hkglobalmarket", "UCWskYkV4c4S9D__rsfOl2JA"),
    Channel("부읽남TV", "부동산", "https://www.youtube.com/@buiknam_tv", "UC2QeHNJFfuQWB4cy3M-745g"),
    Channel("작가 송희구", "부동산", "https://www.youtube.com/@thewriter-song", "UCrxr7eBgbKdz0e1t5ax9kCg"),
    Channel(
        "새벽보기Live",
        "부동산",
        "https://www.youtube.com/@%EC%83%88%EB%B2%BD%EB%B3%B4%EA%B8%B0%EB%9D%BC%EC%9D%B4%EB%B8%8C",
        "UCcp1GsUZnKPf8AbbxAzUGfw",
    ),
    Channel("집코노미", "부동산", "https://www.youtube.com/@jipconomy", "UCAVdqlngIAxHtwlCA2hjv3A"),
]


ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


GUEST_PATTERNS = [
    r"\[[^\]]*(작가|대표|위원|기자|교수|전문가|애널리스트|소장|원장|회장|풀버전|[0-9]+부)[^\]]*\]",
    r"\([^)]*(작가|대표|교수|기자|전문가|애널리스트)[^)]*\)",
]


def request_text(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; youtube-report-bot/1.0)",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", "ignore")


def compact(value: str, limit: int = 1200) -> str:
    value = " ".join((value or "").split())
    return value[:limit]


def parse_now() -> datetime:
    override = os.getenv("NOW_UTC")
    if override:
        return datetime.fromisoformat(override.replace("Z", "+00:00")).astimezone(UTC)
    return datetime.now(UTC)


def fetch_videos(channel: Channel, start: datetime, end: datetime) -> list[dict]:
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.channel_id}"
    root = ET.fromstring(request_text(feed_url))
    videos = []
    for entry in root.findall("atom:entry", ATOM_NS):
        published = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
        if not published:
            continue
        published_at = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(UTC)
        if not (start <= published_at <= end):
            continue
        title = entry.findtext("atom:title", default="", namespaces=ATOM_NS)
        if channel.kind == "부동산" and is_guest_video(title):
            continue
        video_id = entry.findtext("yt:videoId", default="", namespaces=ATOM_NS)
        group = entry.find("media:group", ATOM_NS)
        description = ""
        if group is not None:
            description = group.findtext("media:description", default="", namespaces=ATOM_NS)
        videos.append(
            {
                "source_id": f"yt_{video_id}",
                "item_type": "video",
                "channel": channel.name,
                "kind": channel.kind,
                "title": title,
                "url": f"https://youtu.be/{video_id}",
                "published_at": published_at.isoformat(),
                "published_kst": published_at.astimezone(KST).strftime("%Y-%m-%d %H:%M KST"),
                "description": compact(description),
            }
        )
    return videos


def is_guest_video(title: str) -> bool:
    return any(re.search(pattern, title) for pattern in GUEST_PATTERNS)


def text_of(obj) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if "simpleText" in obj:
            return obj["simpleText"]
        if "runs" in obj:
            return "".join(run.get("text", "") for run in obj["runs"])
    return ""


def extract_json_after(source: str, marker: str) -> Optional[dict]:
    marker_index = source.find(marker)
    start = source.find("{", marker_index if marker_index >= 0 else 0)
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for index, char in enumerate(source[start:], start):
        if in_str:
            if esc:
                esc = False
            elif char == "\\":
                esc = True
            elif char == '"':
                in_str = False
        else:
            if char == '"':
                in_str = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(source[start : index + 1])
    return None


def walk_posts(obj, posts: list[dict]) -> None:
    if isinstance(obj, dict):
        if "backstagePostRenderer" in obj:
            renderer = obj["backstagePostRenderer"]
            posts.append(
                {
                    "time": text_of(renderer.get("publishedTimeText")),
                    "content": text_of(renderer.get("contentText")) or text_of(renderer.get("content")),
                    "post_id": renderer.get("postId") or "",
                }
            )
        for value in obj.values():
            walk_posts(value, posts)
    elif isinstance(obj, list):
        for value in obj:
            walk_posts(value, posts)


def is_recent_relative_time(label: str) -> bool:
    label = label.replace("(수정됨)", "").strip()
    if any(unit in label for unit in ["분 전", "시간 전"]):
        return True
    return label.startswith("1일 전")


def fetch_asset_posts() -> list[dict]:
    source = request_text("https://www.youtube.com/@asset.x2/community")
    data = extract_json_after(source, "var ytInitialData = ")
    if not data:
        return []
    posts: list[dict] = []
    walk_posts(data, posts)
    recent = []
    seen = set()
    for index, post in enumerate(posts, start=1):
        key = post["post_id"] or post["content"][:120]
        if key in seen or not is_recent_relative_time(post["time"]):
            continue
        seen.add(key)
        post_id = post["post_id"] or f"asset_x2_community_{index}"
        recent.append(
            {
                "source_id": f"post_{post_id}",
                "item_type": "community_post",
                "channel": "asset.x2",
                "kind": "주식",
                "title": "asset.x2 커뮤니티 게시물",
                "url": "https://www.youtube.com/@asset.x2/community",
                "time": post["time"],
                "content": compact(post["content"], 2200),
                "post_id": post_id,
            }
        )
        if len(recent) >= MAX_COMMUNITY_POSTS:
            break
    return recent


def fetch_videos_safely(channel: Channel, start: datetime, end: datetime, errors: list[dict]) -> list[dict]:
    try:
        return fetch_videos(channel, start, end)
    except Exception as exc:  # Keep one flaky feed from killing the full daily report.
        errors.append({"source": channel.name, "error": f"{type(exc).__name__}: {exc}"})
        return []


def fetch_asset_posts_safely(errors: list[dict]) -> list[dict]:
    try:
        return fetch_asset_posts()
    except Exception as exc:
        errors.append({"source": "asset.x2 community", "error": f"{type(exc).__name__}: {exc}"})
        return []


def build_prompt(payload: dict) -> str:
    return textwrap.dedent(
        f"""
        아래 JSON은 YouTube RSS와 커뮤니티 게시물에서 수집한 최근 {WINDOW_HOURS}시간 투자 콘텐츠 데이터다.
        한국어 투자 보고서를 만들기 위한 구조화 JSON만 작성해라.

        요구사항:
        - 주식 채널 4개와 부동산 채널 4개를 구분한다.
        - 부동산 채널은 이미 게스트 영상이 제외된 상태다. 제외/없음도 짧게 언급한다.
        - 영상을 보지 않은 사람도 메시지를 이해하게 제목/설명/게시물 내용을 바탕으로 핵심 논리를 빠짐없이 정리한다.
        - 확실히 데이터에 없는 내용은 추정이라고 표현한다.
        - 'N가지', '6가지 원인', '3가지 핵심'처럼 숫자로 묶인 설명은 각 항목을 하나씩 풀어쓴다.
        - 고등학생이 모를 만한 용어는 terms 배열에 쉬운 설명으로 분리한다.
        - 'Opinion' 철자는 사용자가 선호한 'Opnion'으로 쓴다.
        - 투자 조언은 단정하지 말고 리스크 점검 중심으로 쓴다.
        - 응답은 마크다운 없이 유효한 JSON 객체 하나만 반환한다.

        JSON 스키마:
        {{
          "report": {{
            "title": "YYYY-MM-DD 투자 브리핑",
            "overall_summary": "전체 핵심 요약",
            "stock_summary": "주식 요약",
            "real_estate_summary": "부동산 요약",
            "market_mood": "시장 분위기"
          }},
          "sections": [
            {{"title": "1. 한눈에 보는 핵심", "paragraphs": ["문단1", "문단2"]}}
          ],
          "item_summaries": [
            {{
              "source_id": "입력 데이터의 source_id",
              "summary": "해당 영상/게시물 요약",
              "facts": ["검증된 사실"],
              "opnions": ["출연자/채널 의견"],
              "insights": ["해석"],
              "recommendations": ["리스크 점검 중심 권고"],
              "risks": ["주의점"],
              "keywords": ["키워드"]
            }}
          ],
          "checklist": {{
            "Fact": [{{"source_channel": "채널명", "content": "내용"}}],
            "Opnion": [{{"source_channel": "채널명", "content": "내용"}}],
            "Insight": [{{"source_channel": "채널명", "content": "내용"}}],
            "Recommendation": [{{"source_channel": "채널명", "content": "내용"}}]
          }},
          "keywords": [{{"keyword": "키워드", "category": "주식 또는 부동산 또는 공통", "note": "짧은 설명"}}],
          "terms": [{{"term": "용어", "explanation": "고등학생도 이해할 설명", "source_context": "관련 맥락"}}],
          "telegram": {{
            "headline": "짧은 제목",
            "summary": "텔레그램에 보낼 짧은 요약",
            "keywords": ["키워드"],
            "risk_note": "주의점"
          }}
        }}

        데이터:
        {json.dumps(payload, ensure_ascii=False, indent=2)}
        """
    ).strip()


def plain_text_report(message: str) -> str:
    """Remove common markdown marks so Telegram shows a clean plain-text report."""
    lines = []
    for raw_line in message.splitlines():
        line = raw_line.strip()
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*]\s+", "· ", line)
        if re.fullmatch(r"-{3,}", line):
            continue
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = re.sub(r"\[\^(\d+)\]", r"(용어 설명 \1)", line)
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_json_document(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        raise RuntimeError("OpenAI response did not contain a JSON object")
    return json.loads(match.group(0))


def call_openai(prompt: str) -> str:
    api_key = re.sub(r"\s+", "", os.getenv("OPENAI_API_KEY", ""))
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    body = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "developer",
                "content": "너는 한국어 투자 콘텐츠 요약 보고서를 쓰는 분석가다. 과장보다 구조적 이해와 리스크 점검을 우선한다.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"OpenAI API error: {exc.code} {detail}") from exc
    chunks = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    chunks.append(content.get("text", ""))
    text = "\n".join(chunks).strip()
    if not text:
        raise RuntimeError("OpenAI response had no output_text")
    return text


def send_telegram(message: str, parse_mode: Optional[str] = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set; printing report only.", file=sys.stderr)
        return
    params = {"chat_id": chat_id, "text": message[:3900], "disable_web_page_preview": "true"}
    if parse_mode:
        params["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as res:
        res.read()


def report_paths(report_date: str) -> dict[str, Path]:
    year, month, day = report_date.split("-")
    return {
        "manifest": OUTPUT_ROOT / "manifest",
        "raw": OUTPUT_ROOT / "raw" / year / month / day,
        "normalized": OUTPUT_ROOT / "normalized" / year / month / day,
        "html": OUTPUT_ROOT / "reports" / "html" / year / month / day,
        "telegram": OUTPUT_ROOT / "reports" / "telegram" / year / month / day,
        "database": OUTPUT_ROOT / "database",
        "backup": OUTPUT_ROOT / "backups" / year / month / day,
    }


def write_json(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def source_items(payload: dict) -> list[dict]:
    rows = []
    for video in payload["videos"]:
        rows.append(
            {
                "item_id": video["source_id"],
                "item_type": "video",
                "category": video["kind"],
                "channel_name": video["channel"],
                "title": video["title"],
                "url": video["url"],
                "published_at": video.get("published_at"),
                "published_kst": video.get("published_kst"),
                "raw_description": video.get("description", ""),
            }
        )
    for post in payload["asset_x2_posts"]:
        rows.append(
            {
                "item_id": post["source_id"],
                "item_type": "community_post",
                "category": post["kind"],
                "channel_name": post["channel"],
                "title": post["title"],
                "url": post["url"],
                "published_at": None,
                "published_kst": post.get("time"),
                "raw_description": post.get("content", ""),
            }
        )
    return rows


def normalize_outputs(payload: dict, structured: dict) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    items_by_id = {item["item_id"]: item for item in source_items(payload)}
    ai_by_id = {item.get("source_id"): item for item in structured.get("item_summaries", [])}

    items = []
    insights = []
    keywords: dict[str, dict] = {}
    terms = []

    for item_id, item in items_by_id.items():
        ai = ai_by_id.get(item_id, {})
        merged = {**item, "summary": ai.get("summary", "")}
        items.append(merged)
        mapping = [
            ("Fact", ai.get("facts", [])),
            ("Opnion", ai.get("opnions", [])),
            ("Insight", ai.get("insights", [])),
            ("Recommendation", ai.get("recommendations", [])),
            ("Risk", ai.get("risks", [])),
        ]
        for insight_type, values in mapping:
            for content in values or []:
                insights.append(
                    {
                        "report_id": payload["report_id"],
                        "item_id": item_id,
                        "date": payload["report_date"],
                        "category": item["category"],
                        "source_channel": item["channel_name"],
                        "insight_type": insight_type,
                        "content": content,
                        "importance": None,
                        "confidence": "medium",
                        "action_required": 1 if insight_type in {"Recommendation", "Risk"} else 0,
                        "tags": ",".join(ai.get("keywords", []) or []),
                    }
                )
        for keyword in ai.get("keywords", []) or []:
            keywords[keyword] = {"keyword": keyword, "category": item["category"], "note": "", "date": payload["report_date"]}

    for insight_type, values in (structured.get("checklist") or {}).items():
        for value in values or []:
            insights.append(
                {
                    "report_id": payload["report_id"],
                    "item_id": None,
                    "date": payload["report_date"],
                    "category": "",
                    "source_channel": value.get("source_channel", ""),
                    "insight_type": insight_type,
                    "content": value.get("content", ""),
                    "importance": None,
                    "confidence": "medium",
                    "action_required": 1 if insight_type == "Recommendation" else 0,
                    "tags": "",
                }
            )

    for keyword in structured.get("keywords", []) or []:
        key = keyword.get("keyword")
        if key:
            keywords[key] = {**keyword, "date": payload["report_date"]}

    for term in structured.get("terms", []) or []:
        if term.get("term") and term.get("explanation"):
            terms.append(
                {
                    "report_id": payload["report_id"],
                    "date": payload["report_date"],
                    "term": term["term"],
                    "explanation": term["explanation"],
                    "source_context": term.get("source_context", ""),
                }
            )

    return items, insights, list(keywords.values()), terms


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS reports (
  report_id TEXT PRIMARY KEY,
  report_date TEXT NOT NULL,
  period_start TEXT NOT NULL,
  period_end TEXT NOT NULL,
  created_at TEXT NOT NULL,
  overall_summary TEXT,
  stock_summary TEXT,
  real_estate_summary TEXT,
  market_mood TEXT
);

CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL,
  category TEXT NOT NULL,
  channel_name TEXT NOT NULL,
  item_type TEXT NOT NULL,
  title TEXT NOT NULL,
  url TEXT,
  published_at TEXT,
  raw_description TEXT,
  summary TEXT,
  FOREIGN KEY (report_id) REFERENCES reports(report_id)
);

CREATE TABLE IF NOT EXISTS insights (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL,
  item_id TEXT,
  insight_type TEXT NOT NULL,
  source_channel TEXT,
  content TEXT NOT NULL,
  importance INTEGER,
  confidence TEXT,
  action_required INTEGER DEFAULT 0,
  tags TEXT,
  FOREIGN KEY (report_id) REFERENCES reports(report_id),
  FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE TABLE IF NOT EXISTS keywords (
  keyword TEXT PRIMARY KEY,
  category TEXT,
  first_seen TEXT,
  last_seen TEXT,
  count INTEGER DEFAULT 1,
  related_channels TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS terms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL,
  term TEXT NOT NULL,
  explanation TEXT NOT NULL,
  source_context TEXT,
  FOREIGN KEY (report_id) REFERENCES reports(report_id)
);

CREATE TABLE IF NOT EXISTS report_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id TEXT NOT NULL,
  file_type TEXT NOT NULL,
  file_name TEXT NOT NULL,
  drive_url TEXT,
  local_path TEXT,
  telegram_sent INTEGER DEFAULT 0,
  telegram_sent_at TEXT,
  FOREIGN KEY (report_id) REFERENCES reports(report_id)
);
"""


def save_sqlite(db_path: Path, payload: dict, structured: dict, items: list[dict], insights: list[dict], keywords: list[dict], terms: list[dict], html_path: Path, telegram_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        report = structured.get("report", {})
        conn.execute(
            """
            INSERT OR REPLACE INTO reports
            (report_id, report_date, period_start, period_end, created_at, overall_summary, stock_summary, real_estate_summary, market_mood)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["report_id"],
                payload["report_date"],
                payload["window"]["start_kst"],
                payload["window"]["end_kst"],
                datetime.now(KST).isoformat(),
                report.get("overall_summary", ""),
                report.get("stock_summary", ""),
                report.get("real_estate_summary", ""),
                report.get("market_mood", ""),
            ),
        )
        for item in items:
            conn.execute(
                """
                INSERT OR REPLACE INTO items
                (item_id, report_id, category, channel_name, item_type, title, url, published_at, raw_description, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["item_id"],
                    payload["report_id"],
                    item["category"],
                    item["channel_name"],
                    item["item_type"],
                    item["title"],
                    item.get("url"),
                    item.get("published_at"),
                    item.get("raw_description", ""),
                    item.get("summary", ""),
                ),
            )
        conn.execute("DELETE FROM insights WHERE report_id = ?", (payload["report_id"],))
        for insight in insights:
            conn.execute(
                """
                INSERT INTO insights
                (report_id, item_id, insight_type, source_channel, content, importance, confidence, action_required, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["report_id"],
                    insight.get("item_id"),
                    insight["insight_type"],
                    insight.get("source_channel", ""),
                    insight["content"],
                    insight.get("importance"),
                    insight.get("confidence"),
                    insight.get("action_required", 0),
                    insight.get("tags", ""),
                ),
            )
        for keyword in keywords:
            key = keyword.get("keyword")
            if not key:
                continue
            conn.execute(
                """
                INSERT INTO keywords (keyword, category, first_seen, last_seen, count, related_channels, note)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(keyword) DO UPDATE SET
                  last_seen = excluded.last_seen,
                  count = count + 1,
                  category = COALESCE(excluded.category, category),
                  note = COALESCE(excluded.note, note)
                """,
                (key, keyword.get("category", ""), payload["report_date"], payload["report_date"], "", keyword.get("note", "")),
            )
        conn.execute("DELETE FROM terms WHERE report_id = ?", (payload["report_id"],))
        for term in terms:
            conn.execute(
                "INSERT INTO terms (report_id, term, explanation, source_context) VALUES (?, ?, ?, ?)",
                (payload["report_id"], term["term"], term["explanation"], term.get("source_context", "")),
            )
        conn.execute("DELETE FROM report_files WHERE report_id = ?", (payload["report_id"],))
        for file_type, path in [("html", html_path), ("telegram_html", telegram_path), ("sqlite", db_path)]:
            conn.execute(
                """
                INSERT INTO report_files (report_id, file_type, file_name, local_path, telegram_sent)
                VALUES (?, ?, ?, ?, ?)
                """,
                (payload["report_id"], file_type, path.name, str(path), 1 if file_type == "telegram_html" else 0),
            )


def render_full_html(payload: dict, structured: dict, items: list[dict], insights: list[dict], keywords: list[dict], terms: list[dict]) -> str:
    report = structured.get("report", {})
    section_html = []
    for section in structured.get("sections", []) or []:
        paragraphs = "\n".join(f"<p>{escape(str(p))}</p>" for p in section.get("paragraphs", []) or [])
        section_html.append(f"<section><h2>{escape(section.get('title', ''))}</h2>{paragraphs}</section>")
    item_html = []
    for item in items:
        item_html.append(
            f"""
            <article class="item">
              <h3>{escape(item['channel_name'])} · {escape(item['title'])}</h3>
              <p class="meta">{escape(item['category'])} / {escape(item.get('published_kst') or '')}</p>
              <p>{escape(item.get('summary') or item.get('raw_description') or '수집된 요약 없음')}</p>
              <p><a href="{escape(item.get('url') or '')}">원문 보기</a></p>
            </article>
            """
        )
    insight_html = "\n".join(
        f"<li><b>{escape(i['insight_type'])}</b> · {escape(i.get('source_channel') or '')}: {escape(i['content'])}</li>"
        for i in insights
        if i.get("content")
    )
    keyword_html = "\n".join(f"<li>{escape(k.get('keyword', ''))} <span>{escape(k.get('category', ''))}</span></li>" for k in keywords)
    term_html = "\n".join(f"<dt>{escape(t['term'])}</dt><dd>{escape(t['explanation'])}</dd>" for t in terms)
    return textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="ko">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>{escape(report.get('title') or payload['report_id'])}</title>
          <style>
            body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #18212f; background: #f5f7fb; line-height: 1.65; }}
            main {{ max-width: 980px; margin: 0 auto; padding: 32px 18px 56px; }}
            header {{ padding: 28px 0; border-bottom: 3px solid #27364f; }}
            h1 {{ font-size: 28px; margin: 0 0 10px; }}
            h2 {{ margin-top: 34px; padding-top: 8px; border-top: 1px solid #d9dfeb; }}
            h3 {{ margin-bottom: 4px; }}
            section, .item {{ background: white; border: 1px solid #dde3ee; border-radius: 8px; padding: 18px; margin: 16px 0; }}
            .meta {{ color: #667085; font-size: 14px; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }}
            a {{ color: #1957b8; }}
            dt {{ font-weight: 700; margin-top: 12px; }}
            dd {{ margin-left: 0; }}
          </style>
        </head>
        <body>
          <main>
            <header>
              <h1>{escape(report.get('title') or payload['report_id'])}</h1>
              <p>{escape(payload['window']['start_kst'])} ~ {escape(payload['window']['end_kst'])}</p>
              <p>{escape(report.get('overall_summary', ''))}</p>
            </header>
            {''.join(section_html)}
            <section>
              <h2>채널별 상세</h2>
              {''.join(item_html) or '<p>수집된 항목이 없습니다.</p>'}
            </section>
            <section>
              <h2>Fact / Opnion / Insight / Recommendation</h2>
              <ul>{insight_html}</ul>
            </section>
            <section class="grid">
              <div>
                <h2>반복 키워드</h2>
                <ul>{keyword_html}</ul>
              </div>
              <div>
                <h2>용어 설명</h2>
                <dl>{term_html}</dl>
              </div>
            </section>
          </main>
        </body>
        </html>
        """
    )


def render_telegram_html(payload: dict, structured: dict, html_path: Path) -> str:
    report = structured.get("report", {})
    telegram = structured.get("telegram", {})
    keywords = telegram.get("keywords") or [k.get("keyword") for k in structured.get("keywords", [])[:6]]
    keyword_text = " / ".join(str(k) for k in keywords if k)
    drive_url = os.getenv("REPORT_HTML_URL", "")
    detail_line = (
        f'<a href="{escape(drive_url)}">상세 HTML 리포트 보기</a>'
        if drive_url
        else f"상세 HTML 리포트: {escape(str(html_path))}"
    )
    return textwrap.dedent(
        f"""\
        <b>{escape(telegram.get('headline') or report.get('title') or payload['report_id'])}</b>

        <b>핵심 요약</b>
        {escape(telegram.get('summary') or report.get('overall_summary', ''))}

        <b>오늘의 키워드</b>
        {escape(keyword_text)}

        <b>주의점</b>
        {escape(telegram.get('risk_note') or report.get('market_mood', ''))}

        <b>상세 리포트</b>
        {detail_line}
        """
    ).strip()


def main() -> int:
    now = parse_now()
    start = now - timedelta(hours=WINDOW_HOURS)
    report_date = now.astimezone(KST).strftime("%Y-%m-%d")
    report_id = f"report_{report_date}"
    paths = report_paths(report_date)
    payload = {
        "report_id": report_id,
        "report_date": report_date,
        "window": {
            "start_kst": start.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            "end_kst": now.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        },
        "channels": [{"name": c.name, "kind": c.kind, "url": c.url} for c in CHANNELS],
        "videos": [],
        "asset_x2_posts": [],
        "fetch_errors": [],
    }
    for channel in CHANNELS:
        payload["videos"].extend(fetch_videos_safely(channel, start, now, payload["fetch_errors"]))
    payload["asset_x2_posts"] = fetch_asset_posts_safely(payload["fetch_errors"])

    expected_sources = len(CHANNELS) + 1  # 8 video feeds + asset.x2 community.
    if len(payload["fetch_errors"]) >= expected_sources:
        print("All YouTube sources failed; refusing to generate an unverified empty report.", file=sys.stderr)
        print(json.dumps(payload["fetch_errors"], ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    raw_path = paths["raw"] / "raw_sources.json"
    write_json(raw_path, payload)

    if os.getenv("DRY_RUN") == "1":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    structured = extract_json_document(call_openai(build_prompt(payload)))
    items, insights, keywords, terms = normalize_outputs(payload, structured)

    html_path = paths["html"] / "report.html"
    telegram_path = paths["telegram"] / "telegram.html"
    db_path = paths["database"] / "investment_insights.sqlite"
    schema_path = paths["database"] / "schema.sql"

    write_json(paths["normalized"] / "structured_report.json", structured)
    write_jsonl(paths["normalized"] / "items.jsonl", items)
    write_jsonl(paths["normalized"] / "insights.jsonl", insights)
    write_jsonl(paths["normalized"] / "keywords.jsonl", keywords)
    write_jsonl(paths["normalized"] / "terms.jsonl", terms)

    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_full_html(payload, structured, items, insights, keywords, terms), encoding="utf-8")
    telegram_html = render_telegram_html(payload, structured, html_path)
    telegram_path.parent.mkdir(parents=True, exist_ok=True)
    telegram_path.write_text(telegram_html + "\n", encoding="utf-8")

    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(SCHEMA_SQL.strip() + "\n", encoding="utf-8")
    save_sqlite(db_path, payload, structured, items, insights, keywords, terms, html_path, telegram_path)

    paths["backup"].mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, paths["backup"] / "investment_insights.sqlite")

    manifest_row = {
        "report_id": report_id,
        "date": report_date,
        "raw_path": str(raw_path),
        "normalized_path": str(paths["normalized"]),
        "html_path": str(html_path),
        "telegram_html_path": str(telegram_path),
        "sqlite_path": str(db_path),
        "created_at": datetime.now(KST).isoformat(),
        "channels": [channel["name"] for channel in payload["channels"]],
    }
    append_jsonl(paths["manifest"] / "reports_manifest.jsonl", manifest_row)
    for item in items:
        append_jsonl(paths["manifest"] / "items_manifest.jsonl", {**manifest_row, **item})
    write_json(paths["manifest"] / "latest.json", manifest_row)

    print(telegram_html)
    send_telegram(telegram_html, parse_mode="HTML")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
