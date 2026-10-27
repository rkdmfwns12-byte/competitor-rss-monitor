
import csv
import json
import re
import time
import logging
from collections import Counter
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser
from xml.etree.ElementTree import Element, SubElement, ElementTree

import requests
import feedparser
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

DATA = Path("data/archive.json")
OUT = Path("docs")
OUT.mkdir(exist_ok=True)
DATA.parent.mkdir(exist_ok=True)

UA = "CompetitorResearchRSS/1.0 (+public research; contact repository owner)"
HEADERS = {"User-Agent": UA}
session = requests.Session()
session.headers.update(HEADERS)
robots_cache = {}

def allowed(url):
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    if root not in robots_cache:
        rp = RobotFileParser()
        rp.set_url(root + "/robots.txt")
        try:
            response = session.get(root + "/robots.txt", timeout=12)
            if response.status_code == 200:
                rp.parse(response.text.splitlines())
                robots_cache[root] = rp
            elif response.status_code == 404:
                robots_cache[root] = None
            else:
                raise RuntimeError(f"robots status {response.status_code}")
        except Exception as exc:
            logging.warning("robots 확인 실패: %s (%s)", root, exc)
            robots_cache[root] = False

    rp = robots_cache[root]
    if rp is False:
        return False
    return True if rp is None else rp.can_fetch(UA, url)

def get(url):
    if not allowed(url):
        raise RuntimeError(f"수집 허용 여부를 확인할 수 없거나 차단됨: {url}")
    response = session.get(url, timeout=20)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return response.text

def with_page(url, page):
    p = urlparse(url)
    query = dict(parse_qsl(p.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunparse(p._replace(query=urlencode(query)))

def parse_date(value):
    value = value.strip()
    for fmt in ("%y.%m.%d", "%Y.%m.%d", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return ""

def collect_cafe24(src):
    items = []
    base_host = urlparse(src["url"]).netloc

    for page in range(1, min(int(src.get("pages", 1)), 10) + 1):
        url = with_page(src["url"], page)
        html = get(url)
        soup = BeautifulSoup(html, "html.parser")

        # 쥬스온의 Cafe24 게시판 HTML 구조
        rows = soup.select("tr.xans-board-list")
        page_count = 0

        for tr in rows:
            anchor = tr.select_one(
                'td.subject a[href*="/article/"]'
            )
            if not anchor:
                continue

            title = anchor.get_text(" ", strip=True)
            link = urljoin(url, anchor.get("href", ""))

            if not title or urlparse(link).netloc != base_host:
                continue

            tds = tr.find_all("td", recursive=False)
            dates = [
                td.get_text(" ", strip=True)
                for td in tds
                if re.fullmatch(
                    r"\d{2,4}[.-]\d{2}[.-]\d{2}",
                    td.get_text(" ", strip=True)
                )
            ]
            published = parse_date(dates[0]) if dates else ""

            items.append({
                "source": src["name"],
                "title": title,
                "url": link,
                "published": published,
                "summary": "",
                "collected": datetime.now(timezone.utc).isoformat()
            })
            page_count += 1

        logging.info("%s page %s: %s posts", src["name"], page, page_count)
        if page_count == 0:
            if page == 1:
                raise RuntimeError("게시물 선택자와 HTML 구조 확인 필요")
            break
        time.sleep(2)

    return items

def collect_rss(src):
    xml = get(src["url"])
    feed = feedparser.parse(xml)
    if feed.bozo and not feed.entries:
        raise RuntimeError("RSS 파싱 오류")

    items = []
    for entry in feed.entries[:100]:
        link = entry.get("link", "")
        if not link:
            continue
        items.append({
            "source": src["name"],
            "title": entry.get("title", "제목 없음"),
            "url": link,
            "published": "",
            "summary": BeautifulSoup(
                entry.get("summary", ""), "html.parser"
            ).get_text(" ", strip=True)[:300],
            "collected": datetime.now(timezone.utc).isoformat()
        })
    return items

def make_rss(items, path):
    rss = Element("rss", version="2.0")
    channel = SubElement(rss, "channel")
    for tag, value in [
        ("title", "경쟁사 콘텐츠 모니터링"),
        ("link", "https://github.com"),
        ("description", "공개 게시물의 제목과 원본 링크")
    ]:
        SubElement(channel, tag).text = value

    for record in sorted(
        items,
        key=lambda x: x.get("published") or x["collected"],
        reverse=True
    )[:200]:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = (
            f'[{record["source"]}] {record["title"]}'
        )
        SubElement(item, "link").text = record["url"]
        SubElement(item, "guid", isPermaLink="false").text = record["url"]
        SubElement(item, "description").text = (
            record.get("summary") or record["title"]
        )
        dt_string = record.get("published") or record["collected"]
        try:
            dt = datetime.fromisoformat(dt_string.replace("Z", "+00:00"))
            SubElement(item, "pubDate").text = format_datetime(dt)
        except ValueError:
            pass

    ElementTree(rss).write(
        path, encoding="utf-8", xml_declaration=True
    )

def write_csv(path, rows, columns):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

def analyze(items):
    stop = {
        "그리고", "하지만", "이렇게", "이유", "방법", "확인",
        "하는", "있는", "없는", "대한", "위한", "이것",
        "그것", "정리", "알아보기", "완벽", "추천"
    }
    counts = Counter()
    by_source = Counter()

    for item in items:
        by_source[item["source"]] += 1
        for token in re.findall(r"[가-힣A-Za-z0-9]{2,}", item["title"]):
            if token not in stop:
                counts[(item["source"], token.lower())] += 1

    keyword_rows = [
        {"source": source, "keyword": word, "count": count}
        for (source, word), count in counts.most_common()
    ]

    write_csv(
        OUT / "keywords.csv",
        keyword_rows,
        ["source", "keyword", "count"]
    )
    write_csv(
        OUT / "sources_summary.csv",
        [{"source": s, "total_posts": n} for s, n in by_source.items()],
        ["source", "total_posts"]
    )

def main():
    sources = json.loads(Path("sources.json").read_text(encoding="utf-8"))
    old = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else []
    db = {item["url"]: item for item in old}
    successes = 0

    for src in sources:
        if not src.get("enabled", True):
            continue
        try:
            if src["type"] == "cafe24":
                incoming = collect_cafe24(src)
            elif src["type"] == "rss":
                incoming = collect_rss(src)
            else:
                raise ValueError("지원하지 않는 source type")

            for item in incoming:
                if item["url"] not in db:
                    db[item["url"]] = item
                elif item.get("published") and not db[item["url"]].get("published"):
                    db[item["url"]]["published"] = item["published"]

            successes += 1
            logging.info("%s: %s collected", src["name"], len(incoming))

        except Exception as exc:
            logging.error("%s 수집 실패: %s", src["name"], exc)

    if successes == 0:
        raise RuntimeError("모든 수집 대상 실패 - 기존 자료 유지")

    records = sorted(
        db.values(),
        key=lambda x: x.get("published") or x["collected"],
        reverse=True
    )
    DATA.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    make_rss(records, OUT / "feed.xml")
    write_csv(
        OUT / "articles.csv", records,
        ["source", "title", "url", "published", "collected", "summary"]
    )
    analyze(records)
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    logging.info("완료: 총 %s개 게시물", len(records))

if __name__ == "__main__":
    main()
