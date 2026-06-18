#!/usr/bin/env python3
"""Create a Korean YouTube investment digest and optionally send it to Telegram."""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import sqlite3
import sys
import textwrap
import time
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
OPENAI_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "12000"))
OPENAI_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "3"))
OUTPUT_ROOT = Path(os.getenv("OUTPUT_ROOT", "out"))
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
HTTPS_CONTEXT: Optional[ssl.SSLContext] = None

try:
    import certifi

    HTTPS_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    HTTPS_CONTEXT = None


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
    with urllib.request.urlopen(req, timeout=timeout, context=HTTPS_CONTEXT) as res:
        return res.read().decode("utf-8", "ignore")


def compact(value: str, limit: int = 700) -> str:
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
                "content": compact(post["content"], 900),
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


def prompt_payload(payload: dict) -> dict:
    """Trim raw source data before sending it to the model."""
    return {
        "report_id": payload["report_id"],
        "report_date": payload["report_date"],
        "window": payload["window"],
        "channels": payload["channels"],
        "videos": [
            {
                "source_id": video["source_id"],
                "item_type": video["item_type"],
                "channel": video["channel"],
                "kind": video["kind"],
                "title": compact(video["title"], 180),
                "url": video["url"],
                "published_kst": video.get("published_kst", ""),
                "description": compact(video.get("description", ""), 360),
            }
            for video in payload["videos"]
        ],
        "asset_x2_posts": [
            {
                "source_id": post["source_id"],
                "item_type": post["item_type"],
                "channel": post["channel"],
                "kind": post["kind"],
                "title": post["title"],
                "url": post["url"],
                "time": post.get("time", ""),
                "content": compact(post.get("content", ""), 520),
            }
            for post in payload["asset_x2_posts"]
        ],
        "fetch_errors": payload["fetch_errors"],
    }


def build_prompt(payload: dict) -> str:
    data = prompt_payload(payload)
    return textwrap.dedent(
        f"""
        아래 JSON은 YouTube RSS와 커뮤니티 게시물에서 수집한 최근 {WINDOW_HOURS}시간 투자 콘텐츠 데이터다.
        한국어 투자 보고서를 만들기 위한 구조화 JSON만 작성해라.

        요구사항:
        - 주식 채널 4개와 부동산 채널 4개를 구분한다.
        - 부동산 채널은 이미 게스트 영상이 제외된 상태다. 제외/없음도 짧게 언급한다.
        - 영상을 보지 않은 사람도 메시지를 이해하게 제목/설명/게시물 내용을 바탕으로 핵심 논리를 빠짐없이 정리한다.
        - 전체 출력은 반드시 끝까지 완성한다. 길게 쓰기보다 끊기지 않는 완성 JSON을 우선한다.
        - sections는 최대 3개, 각 paragraphs는 최대 2개로 제한한다.
        - item_summaries의 summary는 항목당 900자 이내로 쓰되 배경, 핵심 주장, 근거, 투자자가 이해할 결론을 모두 담는다.
        - facts/opnions/insights/recommendations/risks/keywords는 각각 최대 4개로 제한한다.
        - checklist의 Fact/Opnion/Insight/Recommendation은 각각 최대 5개로 제한한다.
        - keywords는 최대 12개, terms는 최대 10개로 제한한다.
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
        {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}
        """
    ).strip()


def fallback_structured_report(payload: dict, reason: str) -> dict:
    """Create a deterministic report when the OpenAI API is temporarily unavailable."""
    items = source_items(payload)
    stock_items = [item for item in items if item["category"] == "주식"]
    real_estate_items = [item for item in items if item["category"] == "부동산"]

    def item_summary(item: dict) -> dict:
        base = compact(item.get("raw_description") or item.get("title", ""), 260)
        summary = f"{item['title']}"
        if base and base != item["title"]:
            summary = f"{summary}: {base}"
        return {
            "source_id": item["item_id"],
            "summary": compact(summary, 320),
            "facts": [f"{item['channel_name']}에서 '{item['title']}' 항목이 수집됐다."],
            "opnions": [],
            "insights": ["OpenAI 한도 오류로 제목과 설명 기반의 임시 요약만 생성됐다."],
            "recommendations": ["상세 투자 판단은 다음 정상 리포트 또는 원문 확인 후 진행한다."],
            "risks": ["AI 분석이 완료되지 않은 임시 리포트다."],
            "keywords": [item["channel_name"], item["category"]],
        }

    stock_channels = ", ".join(sorted({item["channel_name"] for item in stock_items})) or "신규 항목 없음"
    real_estate_channels = ", ".join(sorted({item["channel_name"] for item in real_estate_items})) or "신규 항목 없음"
    return {
        "report": {
            "title": f"{payload['report_date']} 투자 브리핑",
            "overall_summary": "OpenAI 사용량 제한으로 전체 AI 요약은 생성되지 않았다. 대신 수집된 영상/게시물 목록과 원문 기반 임시 요약을 저장했다.",
            "stock_summary": f"주식 항목은 {len(stock_items)}개 수집됐다. 출처: {stock_channels}",
            "real_estate_summary": f"부동산 항목은 {len(real_estate_items)}개 수집됐다. 출처: {real_estate_channels}",
            "market_mood": "OpenAI 한도 오류로 시장 분위기 해석은 보류한다.",
        },
        "sections": [
            {
                "title": "1. 한눈에 보는 핵심",
                "paragraphs": [
                    f"최근 {WINDOW_HOURS}시간 기준 수집은 완료됐지만 OpenAI API가 제한을 반환했다.",
                    "이번 리포트는 자동화 중단을 막기 위한 임시 저장본이며, 다음 정상 실행 때 AI 분석본으로 갱신된다.",
                ],
            }
        ],
        "item_summaries": [item_summary(item) for item in items],
        "checklist": {
            "Fact": [{"source_channel": "자동화", "content": f"수집 항목 {len(items)}개가 저장됐다."}],
            "Opnion": [{"source_channel": "자동화", "content": "OpenAI 한도 문제로 의견 분석은 생성하지 않았다."}],
            "Insight": [{"source_channel": "자동화", "content": "자동화는 유지됐지만 AI 분석 품질은 일시적으로 낮아졌다."}],
            "Recommendation": [{"source_channel": "자동화", "content": "OpenAI 사용 한도 상향 또는 입력 토큰 축소 상태를 확인한다."}],
        },
        "keywords": [
            {"keyword": "OpenAI 한도", "category": "공통", "note": "API 429 발생"},
            {"keyword": "임시 리포트", "category": "공통", "note": "원문 기반 저장본"},
        ],
        "terms": [
            {
                "term": "429",
                "explanation": "API를 너무 많이 쓰거나 허용된 토큰 한도를 넘었을 때 나오는 오류 코드다.",
                "source_context": compact(reason, 220),
            }
        ],
        "telegram": {
            "headline": f"{payload['report_date']} 투자 브리핑 임시 저장",
            "summary": "OpenAI 사용량 제한으로 AI 상세 요약은 실패했지만, 수집 데이터와 임시 리포트는 저장했다.",
            "keywords": ["OpenAI 한도", "임시 리포트"],
            "risk_note": "오늘 메시지는 제목/설명 기반 임시 요약이므로 투자 판단에는 원문 확인이 필요하다.",
        },
    }


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


def report_json_schema() -> dict:
    string_array = {"type": "array", "items": {"type": "string"}}
    checklist_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_channel": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["source_channel", "content"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "report": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "overall_summary": {"type": "string"},
                    "stock_summary": {"type": "string"},
                    "real_estate_summary": {"type": "string"},
                    "market_mood": {"type": "string"},
                },
                "required": ["title", "overall_summary", "stock_summary", "real_estate_summary", "market_mood"],
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "paragraphs": string_array,
                    },
                    "required": ["title", "paragraphs"],
                },
            },
            "item_summaries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "source_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "facts": string_array,
                        "opnions": string_array,
                        "insights": string_array,
                        "recommendations": string_array,
                        "risks": string_array,
                        "keywords": string_array,
                    },
                    "required": ["source_id", "summary", "facts", "opnions", "insights", "recommendations", "risks", "keywords"],
                },
            },
            "checklist": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "Fact": {"type": "array", "items": checklist_item},
                    "Opnion": {"type": "array", "items": checklist_item},
                    "Insight": {"type": "array", "items": checklist_item},
                    "Recommendation": {"type": "array", "items": checklist_item},
                },
                "required": ["Fact", "Opnion", "Insight", "Recommendation"],
            },
            "keywords": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "keyword": {"type": "string"},
                        "category": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["keyword", "category", "note"],
                },
            },
            "terms": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "term": {"type": "string"},
                        "explanation": {"type": "string"},
                        "source_context": {"type": "string"},
                    },
                    "required": ["term", "explanation", "source_context"],
                },
            },
            "telegram": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "headline": {"type": "string"},
                    "summary": {"type": "string"},
                    "keywords": string_array,
                    "risk_note": {"type": "string"},
                },
                "required": ["headline", "summary", "keywords", "risk_note"],
            },
        },
        "required": ["report", "sections", "item_summaries", "checklist", "keywords", "terms", "telegram"],
    }


def call_openai(prompt: str) -> str:
    api_key = re.sub(r"\s+", "", os.getenv("OPENAI_API_KEY", ""))
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    body = {
        "model": OPENAI_MODEL,
        "max_output_tokens": OPENAI_MAX_OUTPUT_TOKENS,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "investment_report",
                "strict": True,
                "schema": report_json_schema(),
            }
        },
        "input": [
            {
                "role": "developer",
                "content": "너는 한국어 투자 콘텐츠 요약 보고서를 쓰는 분석가다. 과장보다 구조적 이해와 리스크 점검을 우선한다.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    data = None
    for attempt in range(OPENAI_MAX_RETRIES + 1):
        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90, context=HTTPS_CONTEXT) as res:
                data = json.loads(res.read().decode("utf-8"))
                break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            if exc.code == 429 and attempt < OPENAI_MAX_RETRIES:
                retry_after = exc.headers.get("retry-after")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else min(60, 2**attempt * 10)
                print(f"OpenAI rate limit hit; retrying in {delay}s ({attempt + 1}/{OPENAI_MAX_RETRIES}).", file=sys.stderr)
                time.sleep(delay)
                continue
            raise RuntimeError(f"OpenAI API error: {exc.code} {detail}") from exc
    if data is None:
        raise RuntimeError("OpenAI request failed without a response")
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
    with urllib.request.urlopen(req, timeout=30, context=HTTPS_CONTEXT) as res:
        res.read()


def send_telegram_messages(messages: list[str], parse_mode: Optional[str] = None) -> None:
    for index, message in enumerate(messages):
        send_telegram(message, parse_mode=parse_mode)
        if index < len(messages) - 1:
            time.sleep(0.8)


def request_json(
    url: str,
    *,
    method: str = "GET",
    token: str,
    body: Optional[bytes] = None,
    content_type: str = "application/json",
) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=45, context=HTTPS_CONTEXT) as res:
        return json.loads(res.read().decode("utf-8"))


def drive_access_token() -> Optional[str]:
    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not credentials_json or not GOOGLE_DRIVE_FOLDER_ID:
        return None
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("Google Drive upload requires google-auth. Install requirements.txt first.") from exc

    credentials_info = json.loads(credentials_json)
    if credentials_info.get("type") == "service_account":
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
    else:
        credentials = Credentials.from_authorized_user_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
    credentials.refresh(Request())
    return credentials.token


class GoogleDriveArchive:
    def __init__(self, token: str, root_folder_id: str):
        self.token = token
        self.root_folder_id = root_folder_id
        self.folder_cache: dict[tuple[str, str], str] = {}

    def ensure_folder(self, parent_id: str, name: str) -> str:
        cache_key = (parent_id, name)
        if cache_key in self.folder_cache:
            return self.folder_cache[cache_key]
        query = (
            f"name = '{self.escape_query(name)}' and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            f"'{parent_id}' in parents and trashed = false"
        )
        params = urllib.parse.urlencode(
            {
                "q": query,
                "fields": "files(id,name)",
                "pageSize": "1",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
        )
        data = request_json(f"https://www.googleapis.com/drive/v3/files?{params}", token=self.token)
        files = data.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            data = request_json(
                "https://www.googleapis.com/drive/v3/files?fields=id,name&supportsAllDrives=true",
                method="POST",
                token=self.token,
                body=json.dumps(metadata).encode("utf-8"),
            )
            folder_id = data["id"]
        self.folder_cache[cache_key] = folder_id
        return folder_id

    def ensure_path(self, relative_parent: Path) -> str:
        folder_id = self.root_folder_id
        for part in relative_parent.parts:
            folder_id = self.ensure_folder(folder_id, part)
        return folder_id

    def find_file(self, parent_id: str, name: str) -> Optional[str]:
        query = f"name = '{self.escape_query(name)}' and '{parent_id}' in parents and trashed = false"
        params = urllib.parse.urlencode(
            {
                "q": query,
                "fields": "files(id,name)",
                "pageSize": "1",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
        )
        data = request_json(f"https://www.googleapis.com/drive/v3/files?{params}", token=self.token)
        files = data.get("files", [])
        return files[0]["id"] if files else None

    def upload_file(self, local_path: Path, relative_path: Path) -> dict:
        parent_id = self.ensure_path(relative_path.parent)
        existing_id = self.find_file(parent_id, relative_path.name)
        metadata = {"name": relative_path.name}
        if existing_id is None:
            metadata["parents"] = [parent_id]
        content = local_path.read_bytes()
        mime_type = guess_mime_type(local_path)
        boundary = "codex_drive_boundary"
        body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
        if existing_id:
            url = f"https://www.googleapis.com/upload/drive/v3/files/{existing_id}?uploadType=multipart&fields=id,name,webViewLink&supportsAllDrives=true"
            method = "PATCH"
        else:
            url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink&supportsAllDrives=true"
            method = "POST"
        data = request_json(
            url,
            method=method,
            token=self.token,
            body=body,
            content_type=f"multipart/related; boundary={boundary}",
        )
        return {
            "file_id": data["id"],
            "name": data["name"],
            "drive_path": str(relative_path),
            "url": data.get("webViewLink", f"https://drive.google.com/file/d/{data['id']}/view"),
        }

    @staticmethod
    def escape_query(value: str) -> str:
        return value.replace("\\", "\\\\").replace("'", "\\'")


def guess_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".sqlite": "application/vnd.sqlite3",
        ".sql": "application/sql",
        ".txt": "text/plain",
    }.get(suffix, "application/octet-stream")


def upload_output_to_drive(root: Path) -> list[dict]:
    token = drive_access_token()
    if not token:
        print("Google Drive upload skipped; GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_DRIVE_FOLDER_ID is not set.", file=sys.stderr)
        return []
    archive = GoogleDriveArchive(token, GOOGLE_DRIVE_FOLDER_ID)
    uploaded = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            uploaded.append(archive.upload_file(path, path.relative_to(root)))
    return uploaded


def update_drive_urls_in_sqlite(db_path: Path, report_id: str, uploaded: list[dict]) -> None:
    urls_by_name = {item["name"]: item["url"] for item in uploaded}
    with sqlite3.connect(db_path) as conn:
        for file_name, drive_url in urls_by_name.items():
            conn.execute(
                "UPDATE report_files SET drive_url = ? WHERE report_id = ? AND file_name = ?",
                (drive_url, report_id, file_name),
            )


def upload_selected_drive_files(paths: list[Path], root: Path) -> list[dict]:
    token = drive_access_token()
    if not token:
        return []
    archive = GoogleDriveArchive(token, GOOGLE_DRIVE_FOLDER_ID)
    uploaded = []
    for path in paths:
        if path.exists() and path.is_file():
            uploaded.append(archive.upload_file(path, path.relative_to(root)))
    return uploaded


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


def render_telegram_messages(payload: dict, structured: dict, html_path: Path) -> list[str]:
    report = structured.get("report", {})
    telegram = structured.get("telegram", {})
    keywords = telegram.get("keywords") or [k.get("keyword") for k in structured.get("keywords", [])[:6]]
    keyword_text = " / ".join(str(k) for k in keywords if k)
    drive_url = os.getenv("REPORT_HTML_URL", "")
    detail_line = (
        f'<a href="{escape(drive_url)}">상세 HTML 리포트 보기</a>'
        if drive_url
        else "상세 HTML 리포트는 GitHub Actions 실행 결과의 artifact에서 확인할 수 있습니다."
    )
    item_lookup = {item["item_id"]: item for item in source_items(payload)}
    summaries = structured.get("item_summaries", []) or []
    summary_lookup = {summary.get("source_id"): summary for summary in summaries}

    def shorten(value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def shorten_complete(value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        candidate = text[:limit].rstrip()
        sentence_end = max(candidate.rfind("."), candidate.rfind("!"), candidate.rfind("?"), candidate.rfind("다."), candidate.rfind("요."))
        if sentence_end >= int(limit * 0.55):
            return candidate[: sentence_end + 1].rstrip()
        return candidate.rstrip(" ,.;:") + "..."

    def category_lines(category: str, limit: int = 8) -> str:
        lines = []
        for summary in summaries:
            item = item_lookup.get(summary.get("source_id"))
            if not item or item["category"] != category:
                continue
            text = summary.get("summary") or item.get("raw_description") or "요약 없음"
            lines.append(f"- {item['channel_name']}: {shorten(text, 260)}")
            if len(lines) >= limit:
                break
        return "\n".join(lines) if lines else "- 최근 24시간 기준으로 정리할 신규 항목이 없습니다."

    def checklist_lines(section: str, limit: int = 4) -> str:
        rows = []
        for row in (structured.get("checklist") or {}).get(section, []) or []:
            source = row.get("source_channel") or "출처 미상"
            content = shorten(row.get("content", ""), 220)
            if content:
                rows.append(f"- {section}: {content} ({source})")
            if len(rows) >= limit:
                break
        return "\n".join(rows) if rows else f"- {section}: 정리된 항목 없음"

    def item_detail(item: dict) -> str:
        summary = summary_lookup.get(item["item_id"], {})
        title = item.get("title", "")
        body = summary.get("summary") or item.get("raw_description") or "수집된 요약 없음"
        context_parts = [body]
        for key in ["insights", "recommendations", "risks"]:
            context_parts.extend(summary.get(key, []) or [])
        story = shorten_complete(" ".join(context_parts), 1250)
        blocks = [
            f"<b>{escape(item['channel_name'])}</b>",
            f"제목: {escape(shorten(title, 180))}",
            f"핵심 내용:\n{escape(story)}",
        ]
        if item.get("url"):
            blocks.append(f'<a href="{escape(item["url"])}">원문 보기</a>')
        return "\n\n".join(blocks)

    def detail_message(category: str, title: str) -> str:
        category_items = [item for item in item_lookup.values() if item["category"] == category]
        if not category_items:
            return f"<b>{escape(title)}</b>\n최근 24시간 기준 신규 항목이 없습니다."
        parts = [f"<b>{escape(title)}</b>"]
        current = parts[0]
        messages = []
        for item in category_items:
            detail = item_detail(item)
            candidate = current + "\n\n" + detail
            if len(candidate) > 3400 and current != parts[0]:
                messages.append(current)
                current = parts[0] + "\n\n" + detail
            else:
                current = candidate
        messages.append(current)
        return "\n\n".join(messages)

    conclusion = shorten(telegram.get("summary") or report.get("overall_summary", ""), 760)
    stock_lines = category_lines("주식")
    real_estate_lines = category_lines("부동산")
    checklist_text = "\n".join(
        [
            checklist_lines("Fact"),
            checklist_lines("Opnion"),
            checklist_lines("Insight"),
            checklist_lines("Recommendation"),
        ]
    )
    risk_note = shorten(telegram.get("risk_note") or report.get("market_mood", ""), 420)
    overview = "\n\n".join(
        [
            f"<b>[투자 브리핑] {escape(payload['report_date'])}</b>",
            f"<b>1. 한 줄 결론</b>\n{escape(conclusion)}",
            f"<b>2. 주식 핵심</b>\n{escape(stock_lines)}",
            f"<b>3. 부동산 핵심</b>\n{escape(real_estate_lines)}",
            f"<b>4. 오늘의 체크리스트</b>\n{escape(checklist_text)}",
            f"<b>5. 오늘의 키워드</b>\n{escape(keyword_text)}",
            f"<b>6. 주의점</b>\n{escape(risk_note)}",
            f"<b>7. 상세 리포트</b>\n{detail_line}",
        ]
    ).strip()
    stock_detail = detail_message("주식", "주식 콘텐츠 상세")
    real_estate_detail = detail_message("부동산", "부동산 콘텐츠 상세")
    checklist = "\n\n".join(
        [
            f"<b>체크리스트</b>\n{escape(checklist_text)}",
            f"<b>용어 설명</b>\n"
            + escape(
                "\n".join(
                    f"- {term.get('term')}: {shorten(term.get('explanation', ''), 220)}"
                    for term in (structured.get("terms") or [])[:8]
                )
                or "- 정리된 용어 없음"
            ),
            f"<b>상세 리포트</b>\n{detail_line}",
        ]
    ).strip()
    messages = [overview, stock_detail, real_estate_detail, checklist]
    split_messages = []
    for message in messages:
        if len(message) <= 3900:
            split_messages.append(message)
            continue
        paragraphs = message.split("\n\n")
        header = paragraphs[0] if paragraphs and paragraphs[0].startswith("<b>") else ""
        current = ""
        for paragraph in paragraphs:
            candidate = paragraph if not current else current + "\n\n" + paragraph
            if len(candidate) > 3400 and current:
                split_messages.append(current)
                current = f"{header}\n\n{paragraph}" if header and paragraph != header else paragraph
            else:
                current = candidate
        if current:
            split_messages.append(current)
    return split_messages


def render_telegram_html(payload: dict, structured: dict, html_path: Path) -> str:
    return "\n\n--- 다음 텔레그램 메시지 ---\n\n".join(render_telegram_messages(payload, structured, html_path))


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

    try:
        structured = extract_json_document(call_openai(build_prompt(payload)))
    except Exception as exc:
        print(f"OpenAI report generation failed; creating fallback report. {type(exc).__name__}: {exc}", file=sys.stderr)
        structured = fallback_structured_report(payload, f"{type(exc).__name__}: {exc}")
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
    telegram_messages = render_telegram_messages(payload, structured, html_path)
    telegram_html = "\n\n--- 다음 텔레그램 메시지 ---\n\n".join(telegram_messages)
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

    drive_uploads = upload_output_to_drive(OUTPUT_ROOT)
    if drive_uploads:
        report_html_url = next((item["url"] for item in drive_uploads if item["drive_path"] == str(html_path.relative_to(OUTPUT_ROOT))), "")
        if report_html_url:
            os.environ["REPORT_HTML_URL"] = report_html_url
            telegram_messages = render_telegram_messages(payload, structured, html_path)
            telegram_html = "\n\n--- 다음 텔레그램 메시지 ---\n\n".join(telegram_messages)
            telegram_path.write_text(telegram_html + "\n", encoding="utf-8")
        write_json(paths["manifest"] / "drive_uploads.json", drive_uploads)
        manifest_row["google_drive"] = {
            "folder_id": GOOGLE_DRIVE_FOLDER_ID,
            "uploaded_count": len(drive_uploads),
            "html_url": report_html_url,
            "files": drive_uploads,
        }
        write_json(paths["manifest"] / "latest.json", manifest_row)
        update_drive_urls_in_sqlite(db_path, report_id, drive_uploads)
        upload_selected_drive_files(
            [db_path, paths["manifest"] / "latest.json", paths["manifest"] / "drive_uploads.json", telegram_path],
            OUTPUT_ROOT,
        )

    print(telegram_html)
    send_telegram_messages(telegram_messages, parse_mode="HTML")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
