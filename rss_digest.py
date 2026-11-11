"""GeekNews RSS 다이제스트 생성기.

피드(최신 20건)를 실행할 때마다 digest/archive.json 에 누적하고,
아직 다이제스트에 실리지 않은 글 중 관심 주제에 맞는 것을 최대 5건 골라
digest/YYYY-MM-DD.md 로 저장한다. 기사 본문 페이지는 열지 않는다.
"""
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

FEED_URL = "https://news.hada.io/rss/news"
MAX_ITEMS = 5
MIN_TOPIC_SCORE = 2  # 본문에 한 번 스치듯 언급된 글은 제외
BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "digest"
ARCHIVE_PATH = OUT_DIR / "archive.json"

# 관심 주제 -> 매칭 키워드 (소문자 비교)
TOPICS = {
    "AI 에이전트": ["에이전트", "agent", "agentic", "mcp", "멀티에이전트"],
    "업무 자동화": ["자동화", "automation", "automate", "워크플로", "workflow", "rpa"],
    "클로드": ["클로드", "claude", "anthropic", "앤트로픽"],
}

ATOM = "{http://www.w3.org/2005/Atom}"
SKIP_SENTENCE = re.compile(r"^(안녕하세요|감사합니다|반갑습니다)")


def fetch_feed(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "rss-digest/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</li>", " . ", text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_entries(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    entries = []
    for e in root.findall(f"{ATOM}entry"):
        link_el = e.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        link = link.split("&utm_")[0]  # 추적 파라미터 제거
        entries.append({
            "id": (e.findtext(f"{ATOM}id") or link).strip(),
            "published": (e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated") or "").strip(),
            "title": (e.findtext(f"{ATOM}title") or "").strip(),
            "summary": strip_html(e.findtext(f"{ATOM}content") or e.findtext(f"{ATOM}summary") or ""),
            "link": link,
        })
    return entries


def match_topics(entry: dict) -> tuple[int, list[str]]:
    noise = re.compile(r"user[- ]?agent")  # HTTP User-Agent 오탐 제거
    title = noise.sub("", entry["title"].lower())
    body = noise.sub("", entry["summary"].lower())
    score, hit = 0, []
    for topic, words in TOPICS.items():
        # 제목 매칭 3점, 본문은 등장 횟수만큼(키워드당 최대 3점)
        t = sum(3 for w in words if w in title) + sum(min(body.count(w), 3) for w in words)
        if t >= MIN_TOPIC_SCORE:
            score += t
            hit.append(topic)
    return score, hit


def short_sentence(s: str, limit: int = 70) -> str:
    """문장을 짧게 다듬는다: 괄호/URL 제거, 길면 잘라 요점만 남긴다."""
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .…")
    if len(s) > limit:
        cut = s[:limit]
        cut = cut[: cut.rfind(" ")] if " " in cut else cut
        s = cut.rstrip(",·") + "…"
    return s.rstrip(".") + "."


def summarize(entry: dict, topics: list[str]) -> str:
    """피드 요약에서 핵심 문장 1개를 줄여 쓰고, 주제 연관성 문장을 덧붙여 두 문장으로 만든다."""
    parts = re.split(r"(?<=[.!?다요])\s+|\s\.\s", entry["summary"])
    parts = [p.strip() for p in parts if len(p.strip()) > 12 and not SKIP_SENTENCE.match(p.strip())]
    first = short_sentence(parts[0]) if parts else short_sentence(entry["title"])
    second = f"{', '.join(topics)} 관점에서 참고할 만한 소식이다."
    return f"{first} {second}"


def three_line_summary(picked: list[dict]) -> list[str]:
    topic_count: dict[str, int] = {}
    for p in picked:
        for t in p["topics"]:
            topic_count[t] = topic_count.get(t, 0) + 1
    dist = ", ".join(f"{t} {n}건" for t, n in sorted(topic_count.items(), key=lambda x: -x[1]))
    top = picked[0]["title"]
    return [
        f"누적된 글 중 아직 안 다룬 관심 주제 글 {len(picked)}건을 골랐다.",
        f"주제 분포: {dist}.",
        f"가장 연관도 높은 글은 「{top}」이다.",
    ]


def build_markdown(picked: list[dict], today: str) -> str:
    lines = [f"# GeekNews 다이제스트 {today}", ""]
    if not picked:
        return "해당 없음\n"
    lines += ["## 오늘의 세 줄 요약", ""]
    lines += [f"{i}. {s}" for i, s in enumerate(three_line_summary(picked), 1)]
    lines.append("")
    for p in picked:
        lines += [
            f"## {p['title']}",
            "",
            p["digest"],
            "",
            f"원문: {p['link']}",
            "",
        ]
    return "\n".join(lines)


def load_archive() -> dict:
    if ARCHIVE_PATH.exists():
        try:
            return json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 손상된 아카이브는 지우지 않고 백업해 둔다
            ARCHIVE_PATH.replace(ARCHIVE_PATH.with_suffix(".broken.json"))
    return {}


def save_archive(archive: dict) -> None:
    tmp = ARCHIVE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ARCHIVE_PATH)  # 중간에 끊겨도 기존 아카이브가 깨지지 않게


def write_index() -> None:
    """정적 뷰어(index.html)가 읽을 다이제스트 목록 digest/index.json 을 만든다."""
    files = sorted(
        (p.name for p in OUT_DIR.glob("*.md") if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", p.name)),
        reverse=True,
    )
    (OUT_DIR / "index.json").write_text(
        json.dumps({"digests": files, "archive": ARCHIVE_PATH.name}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def main() -> int:
    today = date.today().isoformat()
    OUT_DIR.mkdir(exist_ok=True)

    # 1) 피드 20건을 아카이브에 누적 (이미 있는 글은 건너뜀)
    archive = load_archive()
    added = 0
    for e in parse_entries(fetch_feed(FEED_URL)):
        if e["id"] not in archive:
            archive[e["id"]] = {**e, "first_seen": today, "digested_on": None}
            added += 1

    # 2) 아직 다이제스트에 안 실린 글(오늘 재실행 시엔 오늘 실린 글 포함)에서 선별
    scored = []
    for item in archive.values():
        score, topics = match_topics(item)
        item["topics"] = topics  # 뷰어의 아카이브 카드에서 주제 배지로 표시
        if item.get("digested_on") not in (None, today):
            continue
        if score:
            scored.append({**item, "topics": topics, "score": score})
    # 점수 높은 순, 동점이면 최신 글 우선
    scored.sort(key=lambda x: (x["score"], x["published"]), reverse=True)
    picked = scored[:MAX_ITEMS]
    for p in picked:
        p["digest"] = summarize(p, p["topics"])

    # 3) 실린 글 표시 (같은 날 재실행하면 오늘 표시를 다시 계산)
    picked_ids = {p["id"] for p in picked}
    for item in archive.values():
        if item.get("digested_on") == today and item["id"] not in picked_ids:
            item["digested_on"] = None
        if item["id"] in picked_ids:
            item["digested_on"] = today
    save_archive(archive)

    out_path = OUT_DIR / f"{today}.md"
    out_path.write_text(build_markdown(picked, today), encoding="utf-8")  # 같은 날짜면 덮어씀
    # 뷰어가 카드로 그릴 수 있게 같은 내용을 구조화된 JSON 으로도 저장
    out_path.with_suffix(".json").write_text(json.dumps({
        "date": today,
        "summary": three_line_summary(picked) if picked else [],
        "items": [
            {k: p[k] for k in ("title", "digest", "link", "topics", "published")}
            for p in picked
        ],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    write_index()
    print(f"아카이브 {len(archive)}건 (이번에 새로 {added}건 추가)")
    print(f"저장: {out_path} / 고른 기사 {len(picked)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
