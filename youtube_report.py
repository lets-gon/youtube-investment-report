#!/usr/bin/env python3
"""Create a Korean YouTube investment digest and optionally send it to Telegram."""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "24"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
MAX_COMMUNITY_POSTS = int(os.getenv("MAX_COMMUNITY_POSTS", "8"))


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
    Channel("월급쟁이부자들TV", "부동산", "https://www.youtube.com/@weolbu_official", "UCDSj40X9FFUAnx1nv7gQhcA"),
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
                "channel": channel.name,
                "kind": channel.kind,
                "title": title,
                "url": f"https://youtu.be/{video_id}",
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
    for post in posts:
        key = post["post_id"] or post["content"][:120]
        if key in seen or not is_recent_relative_time(post["time"]):
            continue
        seen.add(key)
        recent.append(
            {
                "channel": "asset.x2",
                "time": post["time"],
                "content": compact(post["content"], 2200),
                "post_id": post["post_id"],
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
        한국어로 투자 보고서를 작성해라.

        요구사항:
        - 주식 채널 4개와 부동산 채널 4개를 구분한다.
        - 부동산 채널은 이미 게스트 영상이 제외된 상태다. 제외/없음도 짧게 언급한다.
        - 영상을 보지 않은 사람도 메시지를 이해하게 제목/설명/게시물 내용을 바탕으로 핵심 논리를 빠짐없이 정리한다.
        - 확실히 데이터에 없는 내용은 추정이라고 표현한다.
        - 'N가지', '6가지 원인', '3가지 핵심'처럼 숫자로 묶인 설명은 각 항목을 하나씩 풀어쓴다.
        - 마지막은 Fact, Opnion, Insight, Recommendation 섹션의 checklist로 작성한다.
        - checklist 각 줄에는 대괄호로 출처 유튜버명만 남긴다. 예: [asset.x2] 6월 19일까지 보수적으로 접근
        - 고등학생이 모를 만한 용어는 각주 형식 [^1]으로 설명한다.
        - 'Opinion' 철자는 사용자가 선호한 'Opnion'으로 쓴다.
        - 투자 조언은 단정하지 말고 리스크 점검 중심으로 쓴다.

        데이터:
        {json.dumps(payload, ensure_ascii=False, indent=2)}
        """
    ).strip()


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


def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set; printing report only.", file=sys.stderr)
        return
    # Telegram's message limit is 4096 chars. Keep chunks comfortably below it.
    chunks = []
    while message:
        chunks.append(message[:3800])
        message = message[3800:]
    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}).encode(
            "utf-8"
        )
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as res:
            res.read()


def main() -> int:
    now = parse_now()
    start = now - timedelta(hours=WINDOW_HOURS)
    payload = {
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

    if os.getenv("DRY_RUN") == "1":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    report = call_openai(build_prompt(payload))
    print(report)
    send_telegram(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
