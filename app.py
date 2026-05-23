import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import requests
from bs4 import BeautifulSoup, Tag
from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

TIMEOUT = 30

# ── Ad / noise patterns ──────────────────────────────────────────────
AD_CLASSES = re.compile(
    r"\b(ad|ads|advert|banner|popup|pop_up|overlay|modal|"
    r"sponsor|promotion|float|sidebar|footer|header|"
    r"recommend|related|comment|share|toolbar|nav|menu|"
    r"copyright|footer|bottom|topbar|side-tool|"
    r"广告|弹窗|底部|顶部)\b",
    re.I,
)
AD_IDS = re.compile(
    r"\b(ad|ads|banner|popup|overlay|modal|sidebox|广告|弹窗)\b",
    re.I,
)
AD_SRC = re.compile(
    r"(doubleclick|googlesyndication|google-analytics|"
    r"baidustatic|cnzz|hm\.baidu|tanx|exdynsrv|"
    r"amazon-adsystem|adnxs|cas\.pm|scorecardresearch)",
    re.I,
)


def _is_ad_element(tag: Tag) -> bool:
    if isinstance(tag, str):
        return False
    if tag.name in ("script", "style", "iframe", "ins"):
        return True
    tag_id = tag.get("id") or ""
    tag_class = " ".join(tag.get("class", []) or [])
    if AD_CLASSES.search(tag_class) or AD_IDS.search(tag_id):
        return True
    for attr in tag.attrs:
        if attr.startswith("data-") and AD_SRC.search(str(tag.get(attr, ""))):
            return True
    for attr in ("src", "href"):
        if AD_SRC.search(tag.get(attr, "")):
            return True
    return False


def _clean_soup(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(_is_ad_element):
        tag.decompose()
    for tag in soup.find_all(True):
        style = (tag.get("style") or "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            tag.decompose()


def _find_comic_images(soup: BeautifulSoup, base_url: str) -> list[dict]:
    raw = []
    for img in soup.find_all("img"):
        src = (
            img.get("src")
            or img.get("data-src")
            or img.get("data-original")
            or img.get("data-lazy-src")
            or ""
        ).strip()
        if not src or src.startswith("data:"):
            continue
        src = urllib.parse.urljoin(base_url, src)
        alt = (img.get("alt") or "").strip().lower()
        cls = " ".join(img.get("class", []) or [])
        # Skip obvious non-comic images
        if any(kw in alt for kw in ("logo", "avatar", "icon", "ad", "头像", "广告", "banner")):
            continue
        if any(kw in cls for kw in ("logo", "avatar", "icon", "ad-", "banner", "qr")):
            continue
        raw.append({"el": img, "src": src, "alt": alt, "cls": cls})

    if not raw:
        return []

    # Find the densest image cluster by checking shared ancestors
    # Score each image by how many siblings (nearby images) share its container
    from collections import Counter
    parent_counts = Counter()
    for item in raw:
        p = item["el"].parent
        if p:
            parent_counts[id(p)] += 1
            # Also count grandparent for wider clustering
            gp = p.parent
            if gp:
                parent_counts[id(gp)] += 1

    # Find the best container (the one shared by the most images)
    best_parent_id = parent_counts.most_common(1)[0][0] if parent_counts else None

    # Collect images that belong to the best container's subtree
    def _in_subtree(el, target_id):
        p = el.parent
        for _ in range(6):  # check up to 6 levels up
            if p is None:
                return False
            if id(p) == target_id:
                return True
            p = p.parent
        return False

    if best_parent_id:
        clustered = [item for item in raw if _in_subtree(item["el"], best_parent_id)]
        # Fall back to all images if clustering is too aggressive
        if len(clustered) < 3:
            clustered = raw
    else:
        clustered = raw

    return [{"src": item["src"], "alt": item["alt"]} for item in clustered]


def _find_nav_links(soup: BeautifulSoup, base_url: str) -> dict:
    nav = {"prev": None, "next": None, "chapters": []}
    prev_p = re.compile(r"(上一页|上一章|prev|previous|←|<)", re.I)
    next_p = re.compile(r"(下一页|下一章|next|→|>|下一话)", re.I)
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = urllib.parse.urljoin(base_url, a["href"])
        if prev_p.search(text):
            nav["prev"] = href
        elif next_p.search(text):
            nav["next"] = href
        if any(kw in text.lower() for kw in ("chapter", "ch.", "章", "话")):
            nav["chapters"].append({"title": text, "url": href})
    return nav


def _fetch_page(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except requests.RequestException:
        return None


# ── JavaScript unpacker (Dean Edwards packer) ────────────────────────

def _decoder(c: int, a: int) -> str:
    result = ""
    while True:
        digit = c % a
        result = (chr(digit + 29) if digit > 35 else str(digit) if digit < 10 else chr(digit + 87)) + result
        c = c // a
        if c == 0:
            break
    return result


def _unpack_js(packed: str) -> str:
    m = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{(.+?)\}\((.+?)\)\)",
        packed, re.DOTALL,
    )
    if not m:
        return packed
    args_str = m.group(2)
    split_pos = args_str.rfind(".split('|')")
    if split_pos == -1:
        return packed
    prefix = args_str[:split_pos].strip()
    sq1 = prefix.find("'")
    if sq1 == -1:
        return packed
    i = sq1 + 1
    while i < len(prefix):
        if prefix[i] == "\\":
            i += 2
            continue
        if prefix[i] == "'":
            break
        i += 1
    sq1_end = i
    packed_str = prefix[sq1 + 1:sq1_end].replace("\\'", "'")
    rest = prefix[sq1_end + 1:].strip().lstrip(",").strip()
    parts = rest.split(",", 2)
    if len(parts) < 3:
        return packed
    try:
        radix = int(parts[0].strip())
        count = int(parts[1].strip())
    except ValueError:
        return packed
    words_str = parts[2].strip()
    if words_str.startswith("'") and words_str.endswith("'"):
        words = words_str[1:-1].split("|")
    else:
        return packed
    d = {}
    for j in range(count):
        key = _decoder(j, radix)
        word = words[j] if j < len(words) else ""
        d[key] = word if word else key
    result = packed_str
    for key in sorted(d.keys(), key=len, reverse=True):
        if not key or not key.strip():
            continue
        result = re.sub(r"\b" + re.escape(key) + r"\b", d[key], result)
    return result


# ══════════════════════════════════════════════════════════════════════
#  SEARCH — 多源搜索
# ══════════════════════════════════════════════════════════════════════

def _search_mangabz(keyword: str) -> list[dict]:
    """搜索 mangabz.com"""
    url = f"https://www.mangabz.com/search?title={urllib.parse.quote(keyword)}"
    html = _fetch_page(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.select(".mh-item"):
        a = item.select_one(".mh-item-detali .title a") or item.select_one("a[href]")
        if not a:
            continue
        title = a.get_text(strip=True) or a.get("title", "")
        href = a.get("href", "")
        if not href:
            continue
        comic_url = "https://www.mangabz.com" + href if href.startswith("/") else href
        cover = ""
        cover_img = item.select_one(".mh-cover")
        if cover_img:
            cover = cover_img.get("src", "")
        status = ""
        status_el = item.select_one(".chapter span")
        if status_el:
            status = status_el.get_text(strip=True)
        results.append({
            "title": title,
            "cover": cover,
            "url": comic_url,
            "source": "mangabz",
            "author": "",
            "status": status,
        })
    return results


def _search_baozimh(keyword: str) -> list[dict]:
    """搜索 baozimh.com"""
    url = f"https://www.baozimh.com/search?q={urllib.parse.quote(keyword)}"
    html = _fetch_page(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    for card in soup.select(".comics-card"):
        a = card.select_one(".comics-card__poster") or card.select_one("a[href]")
        if not a:
            continue
        href = a.get("href", "")
        if not href:
            continue
        comic_url = "https://www.baozimh.com" + href if href.startswith("/") else href
        title = ""
        title_el = card.select_one(".comics-card__title h3") or card.select_one("h3")
        if title_el:
            title = title_el.get_text(strip=True)
        cover = ""
        img = card.select_one("amp-img") or card.select_one("img")
        if img:
            cover = img.get("src", "")
        author = ""
        author_el = card.select_one(".tags")
        if author_el:
            author = author_el.get_text(strip=True)
        results.append({
            "title": title,
            "cover": cover,
            "url": comic_url,
            "source": "baozimh",
            "author": author,
            "status": "",
        })
    return results


def _search_mangacopy(keyword: str) -> list[dict]:
    """搜索 mangacopy.com (JSON API)"""
    api = (
        f"https://mangacopy.com/api/kb/web/searchci/comics"
        f"?offset=0&platform=2&limit=12&q={urllib.parse.quote(keyword)}&q_type="
    )
    try:
        resp = requests.get(api, headers={
            **HEADERS, "Accept": "application/json",
        }, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return []
    if data.get("code") != 200:
        return []
    results = []
    for item in data.get("results", {}).get("list", []):
        title = item.get("name", "")
        path_word = item.get("path_word", "")
        if not path_word:
            continue
        comic_url = f"https://mangacopy.com/comic/{path_word}"
        cover = item.get("cover", "")
        authors = item.get("author", [])
        author = authors[0].get("name", "") if authors else ""
        results.append({
            "title": title,
            "cover": cover,
            "url": comic_url,
            "source": "mangacopy",
            "author": author,
            "status": "",
        })
    return results


def _search_manhuaren(keyword: str) -> list[dict]:
    """搜索 manhuaren.com (JSON API)"""
    api = f"https://www.manhuaren.com/search.ashx?t={urllib.parse.quote(keyword)}&language=1&isremovehtml=1"
    try:
        resp = requests.get(api, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        items = resp.json()
    except (requests.RequestException, ValueError):
        return []
    if not isinstance(items, list):
        return []
    results = []
    for item in items:
        title = item.get("Title", "")
        slug = item.get("Url", "")
        if not slug:
            continue
        comic_url = f"https://www.manhuaren.com/manhua-{slug}/"
        last_ch = item.get("LastPartName", "")
        results.append({
            "title": title,
            "cover": "",
            "url": comic_url,
            "source": "manhuaren",
            "author": "",
            "status": last_ch,
        })
    return results


def _search_dm5(keyword: str) -> list[dict]:
    """搜索 dm5.com"""
    url = f"https://www.dm5.com/search?title={urllib.parse.quote(keyword)}"
    html = _fetch_page(url)
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    results = []
    for item in soup.select(".mh-item"):
        a = item.select_one(".mh-item-detali .title a") or item.select_one("a[href]")
        if not a:
            continue
        title = a.get_text(strip=True) or a.get("title", "")
        href = a.get("href", "")
        if not href:
            continue
        comic_url = "https://www.dm5.com" + href if href.startswith("/") else href
        cover = ""
        cover_img = item.select_one("img")
        if cover_img:
            cover = cover_img.get("src", "")
        results.append({
            "title": title,
            "cover": cover,
            "url": comic_url,
            "source": "dm5",
            "author": "",
            "status": "",
        })
    return results


def _relevance_score(title: str, keyword: str) -> float:
    """计算标题与关键词的相关度分数 (0~1, 越高越相关)"""
    t = title.lower().strip()
    k = keyword.lower().strip()
    if not t or not k:
        return 0
    # 完全匹配
    if t == k:
        return 1.0
    # 开头匹配
    if t.startswith(k):
        return 0.95
    # 包含关键词
    if k in t:
        return 0.8
    # 关键词的每个字都在标题中 (适用于中文)
    if all(ch in t for ch in k if ch.strip()):
        return 0.6
    # 部分匹配 — 计算重叠字符比例
    overlap = sum(1 for ch in k if ch in t and ch.strip())
    return 0.3 * (overlap / len(k)) if k else 0


def _search_all(keyword: str) -> list[dict]:
    """并行搜索所有源，按相关度排序"""
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(_search_mangabz, keyword): "mangabz",
            pool.submit(_search_baozimh, keyword): "baozimh",
            pool.submit(_search_mangacopy, keyword): "mangacopy",
            pool.submit(_search_manhuaren, keyword): "manhuaren",
            pool.submit(_search_dm5, keyword): "dm5",
        }
        all_results = []
        for future in futures:
            try:
                all_results.extend(future.result())
            except Exception:
                pass
    # 按相关度排序
    all_results.sort(key=lambda r: _relevance_score(r["title"], keyword), reverse=True)
    return all_results


# ══════════════════════════════════════════════════════════════════════
#  CHAPTERS — 章节列表
# ══════════════════════════════════════════════════════════════════════

def _get_chapters_mangabz(detail_url: str) -> dict:
    """从 mangabz 详情页获取章节列表"""
    html = _fetch_page(detail_url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")

    title = ""
    title_el = soup.select_one(".detail-info-title")
    if title_el:
        title = title_el.get_text(strip=True)

    cover = ""
    cover_el = soup.select_one(".detail-info-cover")
    if cover_el:
        cover = cover_el.get("src", "")

    author = ""
    tip_el = soup.select_one(".detail-info-tip")
    if tip_el:
        author_m = re.search(r"作者[：:]\s*(.+?)(?:\s|$)", tip_el.get_text())
        if author_m:
            author = author_m.group(1).strip()

    chapters = []
    for a in soup.select(".detail-list-form-item"):
        ch_title = a.get_text(strip=True)
        href = a.get("href", "")
        if href:
            ch_url = "https://www.mangabz.com" + href if href.startswith("/") else href
            chapters.append({"title": ch_title, "url": ch_url})

    # 可能有更多章节通过 AJAX 加载，尝试获取
    mid_m = re.search(r"var\s+MANGABZ_COMIC_MID\s*=\s*(\d+)", html)
    if mid_m:
        mid = mid_m.group(1)
        try:
            ajax_url = f"https://www.mangabz.com/template-{mid}-s2/"
            resp = requests.get(ajax_url, headers={
                **HEADERS, "Referer": detail_url,
                "X-Requested-With": "XMLHttpRequest",
            }, timeout=TIMEOUT)
            if resp.ok:
                ajax_soup = BeautifulSoup(resp.text, "lxml")
                for a in ajax_soup.select(".detail-list-form-item"):
                    ch_title = a.get_text(strip=True)
                    href = a.get("href", "")
                    if href:
                        ch_url = "https://www.mangabz.com" + href if href.startswith("/") else href
                        if ch_url not in {c["url"] for c in chapters}:
                            chapters.append({"title": ch_title, "url": ch_url})
        except requests.RequestException:
            pass

    return {
        "title": title,
        "cover": cover,
        "author": author,
        "source": "mangabz",
        "url": detail_url,
        "chapters": chapters,
    }


def _get_chapters_baozimh(detail_url: str) -> dict:
    """从 baozimh 详情页获取章节列表"""
    html = _fetch_page(detail_url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")

    title = ""
    for sel in (".comics-detail__title", "meta[property='og:novel:book_name']", "meta[property='og:title']", "h1"):
        el = soup.select_one(sel)
        if el:
            title = el.get_text(strip=True) if el.name != "meta" else el.get("content", "")
            if title:
                break

    cover = ""
    og_img = soup.select_one("meta[property='og:image']")
    if og_img:
        cover = og_img.get("content", "")
    if not cover:
        img = soup.select_one(".comics-detail__poster img") or soup.select_one("amp-img")
        if img:
            cover = img.get("src", "")

    author = ""
    og_author = soup.select_one("meta[property='og:novel:author']")
    if og_author:
        author = og_author.get("content", "")

    chapters = []
    for a in soup.select(".comics-chapters__item"):
        ch_title = a.get_text(strip=True)
        href = a.get("href", "")
        if href:
            ch_url = "https://www.baozimh.com" + href if href.startswith("/") else href
            chapters.append({"title": ch_title, "url": ch_url})

    return {
        "title": title,
        "cover": cover,
        "author": author,
        "source": "baozimh",
        "url": detail_url,
        "chapters": chapters,
    }


def _get_chapters_mangacopy(detail_url: str) -> dict:
    """从 mangacopy 获取章节列表"""
    html = _fetch_page(detail_url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")

    title = ""
    og_title = soup.select_one("meta[property='og:title']")
    if og_title:
        title = og_title.get("content", "")
    if not title:
        h1 = soup.select_one("h1") or soup.select_one(".comic-title")
        if h1:
            title = h1.get_text(strip=True)

    cover = ""
    og_img = soup.select_one("meta[property='og:image']")
    if og_img:
        cover = og_img.get("content", "")

    author = ""
    og_author = soup.select_one("meta[property='og:novel:author']")
    if og_author:
        author = og_author.get("content", "")

    chapters = []
    for a in soup.select("a[href*='/chapter/']"):
        ch_title = a.get_text(strip=True)
        href = a.get("href", "")
        if href:
            ch_url = "https://mangacopy.com" + href if href.startswith("/") else href
            chapters.append({"title": ch_title, "url": ch_url})

    return {
        "title": title,
        "cover": cover,
        "author": author,
        "source": "mangacopy",
        "url": detail_url,
        "chapters": chapters,
    }


def _get_chapters_manhuaren(detail_url: str) -> dict:
    """从 manhuaren.com 详情页获取章节列表"""
    html = _fetch_page(detail_url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    title = ""
    title_el = soup.select_one(".detail-info-title") or soup.select_one("h1")
    if title_el:
        title = title_el.get_text(strip=True)
    cover = ""
    cover_el = soup.select_one(".detail-info-cover") or soup.select_one(".cover img")
    if cover_el:
        cover = cover_el.get("src", "")
    author = ""
    tip = soup.select_one(".detail-info-tip")
    if tip:
        m = re.search(r"作者[：:]\s*(.+?)(?:\s|$)", tip.get_text())
        if m:
            author = m.group(1).strip()
    chapters = []
    for a in soup.select(".detail-list-form-item, .chapteritem a, a[href*='/m']"):
        ch_title = a.get_text(strip=True)
        href = a.get("href", "")
        if href and ch_title:
            ch_url = "https://www.manhuaren.com" + href if href.startswith("/") else href
            if ch_url not in {c["url"] for c in chapters}:
                chapters.append({"title": ch_title, "url": ch_url})
    return {"title": title, "cover": cover, "author": author, "source": "manhuaren", "url": detail_url, "chapters": chapters}


def _get_chapters_dm5(detail_url: str) -> dict:
    """从 dm5.com 详情页获取章节列表"""
    html = _fetch_page(detail_url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    title = ""
    title_el = soup.select_one(".detail-info-title") or soup.select_one("title")
    if title_el:
        title = title_el.get_text(strip=True)
    cover = ""
    cover_el = soup.select_one(".detail-info-cover")
    if cover_el:
        cover = cover_el.get("src", "")
    author = ""
    tip = soup.select_one(".detail-info-tip")
    if tip:
        m = re.search(r"作者[：:]\s*(.+?)(?:\s|$)", tip.get_text())
        if m:
            author = m.group(1).strip()
    chapters = []
    for a in soup.select(".detail-list-form-item"):
        ch_title = a.get_text(strip=True)
        href = a.get("href", "")
        if href:
            ch_url = "https://www.dm5.com" + href if href.startswith("/") else href
            if ch_url not in {c["url"] for c in chapters}:
                chapters.append({"title": ch_title, "url": ch_url})
    return {"title": title, "cover": cover, "author": author, "source": "dm5", "url": detail_url, "chapters": chapters}


def _get_chapters(detail_url: str) -> dict:
    """自动识别源并获取章节列表"""
    host = urllib.parse.urlparse(detail_url).hostname or ""
    if "mangabz" in host:
        return _get_chapters_mangabz(detail_url)
    elif "baozimh" in host:
        return _get_chapters_baozimh(detail_url)
    elif "mangacopy" in host or "mangafun" in host:
        return _get_chapters_mangacopy(detail_url)
    elif "manhuaren" in host or "dm5" in host:
        return _get_chapters_manhuaren(detail_url)
    # 通用: 尝试从 HTML 提取章节链接
    html = _fetch_page(detail_url)
    if not html:
        return {}
    soup = BeautifulSoup(html, "lxml")
    title = ""
    title_tag = soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)
    chapters = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if any(kw in text for kw in ("第", "话", "章", "chapter", "Chapter")):
            href = a["href"]
            ch_url = urllib.parse.urljoin(detail_url, href)
            chapters.append({"title": text, "url": ch_url})
    return {
        "title": title,
        "cover": "",
        "author": "",
        "source": "unknown",
        "url": detail_url,
        "chapters": chapters,
    }


# ══════════════════════════════════════════════════════════════════════
#  CHAPTER READER — 章节图片抓取
# ══════════════════════════════════════════════════════════════════════

def _handle_dm5_style(url: str) -> dict | None:
    """Handle dm5.com / manhuaren.com pages — similar JS-based image loading."""
    html = _fetch_page(url)
    if not html:
        return None
    # dm5/manhuaren use DM5_CID and DM5_IMAGE_COUNT
    cid_m = re.search(r"var\s+DM5_CID\s*=\s*(\d+)", html)
    count_m = re.search(r"var\s+DM5_IMAGE_COUNT\s*=\s*(\d+)", html)
    title_m = re.search(r"var\s+DM5_CTITLE\s*=\s*\"([^\"]+)\"", html)
    if not cid_m:
        # Try mangabz-style as fallback
        cid_m = re.search(r"var\s+MANGABZ_CID\s*=\s*(\d+)", html)
        count_m = re.search(r"var\s+MANGABZ_IMAGE_COUNT\s*=\s*(\d+)", html)
        title_m = re.search(r"var\s+MANGABZ_CTITLE\s*=\s*\"([^\"]+)\"", html)
    if not cid_m:
        return None
    cid = cid_m.group(1)
    total_pages = int(count_m.group(1)) if count_m else 1
    title = title_m.group(1) if title_m else "漫画阅读"
    all_images = []
    for page in range(1, total_pages + 1):
        # dm5 style: chapterfun.ashx
        api_url = f"https://{urllib.parse.urlparse(url).hostname}/chapterfun.ashx?cid={cid}&page={page}&key=&language=1"
        try:
            resp = requests.get(api_url, headers={
                **HEADERS, "Referer": url, "X-Requested-With": "XMLHttpRequest",
            }, timeout=TIMEOUT)
            resp.raise_for_status()
            unpacked = _unpack_js(resp.text)
            # Extract image URLs from unpacked JS
            imgs = re.findall(r'"(https?://[^"]+)"', unpacked)
            if not imgs:
                # Try path-style: var d=[...]
                paths_m = re.search(r'var\s+d\s*=\s*\[(.*?)\]', unpacked)
                if paths_m:
                    paths = re.findall(r'"(/[^"]+)"', paths_m.group(1))
                    # Try to find the base URL from the page
                    base_m = re.search(r'(https?://[^/]+\.cdndm5\.com/\d+/\d+)', html)
                    if not base_m:
                        base_m = re.search(r'(https?://[^"]+/\d+/\d+)', unpacked)
                    if base_m:
                        base = base_m.group(1).rstrip("/")
                        imgs = [base + p for p in paths]
            all_images.extend(imgs)
        except requests.RequestException:
            pass
    if not all_images:
        return None
    seen = set()
    all_images = [x for x in all_images if not (x in seen or seen.add(x))]
    # Navigation
    nav = {"prev": None, "next": None, "chapters": []}
    prev_m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>.*?上一', html)
    next_m = re.search(r'<a[^>]*href="([^"]*)"[^>]*>.*?下一', html)
    if prev_m:
        nav["prev"] = urllib.parse.urljoin(url, prev_m.group(1))
    if next_m:
        nav["next"] = urllib.parse.urljoin(url, next_m.group(1))
    return {"title": title, "url": url, "images": all_images, "nav": nav}


def _handle_mangabz(url: str) -> dict | None:
    """Handle mangabz.com pages which load images via JS API."""
    html = _fetch_page(url)
    if not html:
        return None
    cid_m = re.search(r"var\s+MANGABZ_CID\s*=\s*(\d+)", html)
    count_m = re.search(r"var\s+MANGABZ_IMAGE_COUNT\s*=\s*(\d+)", html)
    title_m = re.search(r"var\s+MANGABZ_CTITLE\s*=\s*\"([^\"]+)\"", html)
    prev_m = re.search(r'<a\s+href="(/m\d+/)"[^>]*><img[^>]*icon_shangyizhang', html)
    next_m = re.search(r'<a\s+href="(/m\d+/)"[^>]*><img[^>]*icon_xiayizhang', html)
    if not cid_m:
        return None
    cid = cid_m.group(1)
    total_pages = int(count_m.group(1)) if count_m else 1
    title = title_m.group(1) if title_m else "漫画阅读"
    all_images = []
    for page in range(1, total_pages + 1):
        api_url = f"https://www.mangabz.com/chapterimage.ashx?cid={cid}&page={page}"
        try:
            resp = requests.get(api_url, headers={
                **HEADERS, "Referer": url, "X-Requested-With": "XMLHttpRequest",
            }, timeout=TIMEOUT)
            resp.raise_for_status()
            unpacked = _unpack_js(resp.text)
            base_m = re.search(r'pix="([^"]+)"', unpacked)
            key_m = re.search(r"key='([^']+)'", unpacked)
            paths_m = re.search(r'pvalue=\[(.*?)\]', unpacked)
            if base_m and paths_m:
                base = base_m.group(1).rstrip("/")
                img_key = key_m.group(1) if key_m else ""
                paths = re.findall(r'"(/[^"]+\.jpg)"', paths_m.group(1))
                for p in paths:
                    full = base + p
                    if img_key:
                        full += f"?cid={cid}&key={img_key}&uk="
                    all_images.append(full)
        except requests.RequestException:
            pass
    if not all_images:
        return None
    seen = set()
    all_images = [x for x in all_images if not (x in seen or seen.add(x))]
    nav = {"prev": None, "next": None, "chapters": []}
    if prev_m:
        nav["prev"] = "https://www.mangabz.com" + prev_m.group(1)
    if next_m:
        nav["next"] = "https://www.mangabz.com" + next_m.group(1)
    return {"title": title, "url": url, "images": all_images, "nav": nav}


# ══════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/catalog")
def catalog():
    return render_template("catalog.html")


@app.route("/reader")
def reader():
    return render_template("reader.html")


@app.route("/api/search")
def api_search():
    keyword = request.args.get("q", "").strip()
    if not keyword:
        return jsonify({"error": "请输入关键词"}), 400
    results = _search_all(keyword)
    return jsonify({"keyword": keyword, "results": results})


@app.route("/api/chapters")
def api_chapters():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "请输入漫画地址"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    result = _get_chapters(url)
    if not result:
        return jsonify({"error": "无法获取章节列表"}), 400
    return jsonify(result)


@app.route("/fetch", methods=["POST"])
def fetch_comic():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "请输入漫画网址"}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urllib.parse.urlparse(url).hostname or ""
    if "mangabz" in host:
        result = _handle_mangabz(url)
        if result:
            return jsonify(result)
    if "manhuaren" in host or "dm5" in host:
        result = _handle_dm5_style(url)
        if result:
            return jsonify(result)
    html = _fetch_page(url)
    if not html:
        return jsonify({"error": "无法访问该网址，请检查地址是否正确"}), 400
    soup = BeautifulSoup(html, "lxml")
    _clean_soup(soup)
    images = _find_comic_images(soup, url)
    nav = _find_nav_links(soup, url)
    if not images:
        return jsonify({
            "error": "未能从该页面提取到漫画图片。可能原因：\n"
                     "1. 该网站使用 JavaScript 动态加载图片\n"
                     "2. 网址不是漫画阅读页\n"
                     "3. 网站有反爬机制",
        }), 400
    title_tag = soup.find("title")
    page_title = title_tag.get_text(strip=True) if title_tag else "漫画阅读"
    return jsonify({
        "title": page_title, "url": url,
        "images": [img["src"] for img in images], "nav": nav,
    })


@app.route("/proxy-image")
def proxy_image():
    url = request.args.get("url", "")
    if not url:
        return "", 400
    try:
        resp = requests.get(url, headers={
            **HEADERS, "Referer": request.args.get("ref", ""),
        }, timeout=TIMEOUT)
        resp.raise_for_status()
        r = send_file(
            BytesIO(resp.content),
            mimetype=resp.headers.get("Content-Type", "image/jpeg"),
        )
        r.headers["Cache-Control"] = "private, max-age=86400"
        return r
    except requests.RequestException:
        return "", 502


if __name__ == "__main__":
    print("=" * 50)
    print("  漫画阅读器已启动")
    print("  访问地址: http://127.0.0.1:5000")
    print("=" * 50)
    app.run(debug=True, host="127.0.0.1", port=5000)
