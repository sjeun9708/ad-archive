"""브랜드 공식몰에서 광고 배너와 상품 상세페이지를 자동으로 수집해요.

- 광고 배너: 클릭하면 어딘가로 이동하는(링크가 달린) 큰 이미지만 모아요.
  배너를 눌렀을 때 가는 주소(link_url)도 같이 저장해서, 사이트의 '원본 보기'가 그 상품/기획전으로 가요.
- 상세페이지: 진짜 '상품 페이지'(구매·장바구니 버튼이 있는 페이지)에서
  상품 설명 영역의 긴 이미지들만 골라 이어 붙여요. 상단 사진·추천 상품·리뷰는 빼요.
- 배너가 상품 페이지로 연결되면, 그 상품의 상세페이지를 먼저 찍고 배너와 짝지어요.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import sync_playwright, Page, Error as PWError

from common import (
    KST, region_of, mall_label,
    log, today, polite_wait, robots_allowed, open_image, dhash, is_duplicate,
    make_id, save_web_image,
)

MAX_PDP_HEIGHT = 40000
DEBUG_DIR = Path(__file__).resolve().parents[1] / "data" / "debug"
LAST_ERROR = {"reason": ""}
PDP_STATS = {"시도": 0, "성공": 0, "구매버튼 없음": 0, "설명 이미지 부족": 0, "같은 페이지": 0}

# ── 페이지 안에서 실행하는 스크립트 ─────────────────────
# 링크가 달린 큰 이미지 (= 광고 배너 후보)
JS_BANNERS = """(minW) => {
  // 이미지가 페이지 어디에 걸려 있는지로 광고 위치를 알아내요
  const slotOf = (img, link, top, ratio) => {
    let names = '', fixed = false;
    for (let el = img, d = 0; el && d < 12; el = el.parentElement, d++) {
      names += ' ' + (el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '');
      const pos = getComputedStyle(el).position;
      if (pos === 'fixed') fixed = true;
    }
    names = names.toLowerCase();
    if (fixed || /popup|pop_up|modal|layer_?pop|lightbox|dimmed/.test(names)) return '팝업';
    if (/instagram\\.com/.test(link) || /insta|sns|feed|ugc|social|review_?photo/.test(names)) return '인스타 연동';
    if (/prd|product|goods|item_?list|itemlist|prdlist|xans-product/.test(names) && !/banner|visual|event|promotion/.test(names)) return '상품 목록';
    if (top < 1100 && /main|visual|hero|swiper|slick|slide|carousel|banner|kv|rolling/.test(names)) return '메인 배너';
    if (ratio > 3.5) return '띠배너';
    return '중간 배너';
  };
  return Array.from(document.images)
    .filter(i => i.naturalWidth >= minW && i.naturalHeight >= 150)
    .map(i => {
      const a = i.closest('a[href]');
      const r = i.getBoundingClientRect();
      const top = r.top + window.scrollY, link = a ? a.href : '';
      return {src: i.currentSrc || i.src, w: i.naturalWidth, h: i.naturalHeight, alt: i.alt || '',
              link, top, slot: slotOf(i, link, top, i.naturalWidth / i.naturalHeight)};
    });
}"""

SLOTS = ["메인 배너", "팝업", "띠배너", "중간 배너", "인스타 연동"]

# 상품 상세 설명 영역 찾기: 큰 이미지들이 가장 많이 모여 있는 '가장 안쪽' 영역
JS_DETAIL_IMAGES = """(minW) => {
  const imgs = Array.from(document.images).filter(i => i.naturalWidth >= minW && i.naturalHeight >= 200);
  const score = new Map();
  for (const im of imgs) {
    const h = im.naturalHeight * (860 / im.naturalWidth);
    let el = im.parentElement;
    for (let d = 0; d < 12 && el && el !== document.documentElement; d++, el = el.parentElement)
      score.set(el, (score.get(el) || 0) + h);
  }
  let max = 0; for (const v of score.values()) max = Math.max(max, v);
  let best = null, bestDepth = -1;
  for (const [el, v] of score) {
    if (v < max * 0.6) continue;
    let depth = 0; for (let p = el; p; p = p.parentElement) depth++;
    if (depth > bestDepth) { best = el; bestDepth = depth; }
  }
  if (!best) return {total: 0, srcs: []};
  const srcs = Array.from(best.querySelectorAll('img'))
    .filter(i => i.naturalWidth >= minW && i.naturalHeight >= 200)
    .map(i => i.currentSrc || i.src);
  return {total: score.get(best), srcs};
}"""

# 상품 페이지인지: 구매/장바구니 버튼 같은 글자가 있는지
JS_IS_PRODUCT = """() => {
  const t = (document.body ? document.body.innerText : '').toLowerCase();
  return ['구매하기', '바로구매', '장바구니', '바로 구매', '구매 하기', '선물하기', '주문하기',
          'add to cart', 'buy now', 'add to bag', 'buy it now', 'checkout']
    .some(w => t.includes(w));
}"""

JS_LINKS = """() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"""

# '베스트' 메뉴 찾기 (잘 팔리는 상품이 모인 페이지)
JS_BEST_LINK = """() => {
  const words = ['베스트', 'best', '랭킹', 'ranking', '인기'];
  for (const a of document.querySelectorAll('a[href]')) {
    const t = (a.innerText || a.getAttribute('title') || '').trim().toLowerCase();
    if (t && t.length <= 12 && words.some(w => t.includes(w)) && !a.href.startsWith('javascript')) return a.href;
  }
  return '';
}"""

JS_PAGE_ID = """() => {
  const og = document.querySelector('meta[property="og:site_name"]');
  return [document.title || '', og ? og.content : '', location.host,
          (document.body ? document.body.innerText : '').slice(0, 1500)].join(' ');
}"""

# 상품 상세 주소 모양 (카페24, 샵바이, 쇼피파이, 고도몰 등)
PRODUCT_URL = re.compile(
    r"(/product/[^/?#]+/\d+|[?&]product_no=\d+|/products/[^/?#]+/?($|\?)|[?&]goodsno=\w+"
    r"|/goods/(view|detail)|/product/detail|/item/\d+|/prd/\d+|/goods/\d+)", re.I)
NOT_PRODUCT = re.compile(r"(list|category|search|cart|login|member|board|review|event_list)", re.I)


def is_product_url(u: str) -> bool:
    return bool(PRODUCT_URL.search(u)) and not NOT_PRODUCT.search(urlparse(u).path)


def same_site(a: str, b: str) -> bool:
    ha = urlparse(a).netloc.lower().removeprefix("www.")
    hb = urlparse(b).netloc.lower().removeprefix("www.")
    return bool(ha) and (ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha))


# ── 공통 도우미 ───────────────────────────────
def debug_shot(page: Page, name: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        img = open_image(page.screenshot())
        if img is not None:
            img.thumbnail((900, 900))
            img.save(DEBUG_DIR / f"{name}.jpg", "JPEG", quality=70)
        log(f"  화면 확인용 캡처 저장: data/debug/{name}.jpg")
    except PWError:
        pass


def scroll_through(page: Page, steps: int = 8, pause: int = 700) -> None:
    """아래로 천천히 내려서 늦게 뜨는(lazy) 이미지까지 불러와요."""
    last = 0
    for _ in range(steps):
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(pause)
        h = page.evaluate("document.body.scrollHeight")
        if h == last:
            break
        last = h


def download(page: Page, src: str) -> Image.Image | None:
    if not src or src.startswith("data:"):
        return None
    try:
        r = page.request.get(src, timeout=20000)
        return open_image(r.body()) if r.ok else None
    except PWError:
        return None


def new_item(img: Image.Image, *, brand: str, channel: str, source_name: str,
             page_url: str, src: str, width: int, quality: int = 80,
             extra: dict | None = None) -> dict:
    iid = make_id(channel, src, today())
    return {
        "id": iid, "brand": brand, "channel": channel, "source_name": source_name,
        "page_url": page_url, "image_src": src, "collected_at": today(),
        "hash": dhash(img), "image": save_web_image(img, iid, width, quality),
        "analysis": None, **(extra or {}),
    }


# ── 광고 배너 ─────────────────────────────────
def collect_banners(page: Page, url: str, cfg: dict, known: list[str], *,
                    brand: str, source_name: str) -> list[dict]:
    c = cfg["collect"]
    page.evaluate("window.scrollTo(0, 0)")
    scroll_through(page, steps=4)
    seen, found = set(), []
    cands = sorted(page.evaluate(JS_BANNERS, c["min_banner_width"]), key=lambda i: i["top"])
    for im in cands:
        link = im["link"]
        if im["slot"] == "상품 목록":
            continue                           # 상품 진열 사진은 광고가 아니라서 빼요
        if im["slot"] not in ("팝업", "인스타 연동") and (
                im["src"] in seen or not link or link.startswith("javascript") or link.rstrip("/#") == url.rstrip("/#")):
            continue                           # 클릭해도 아무 데도 안 가는 이미지는 광고가 아님
        if im["src"] in seen:
            continue
        ratio = im["w"] / im["h"]
        if not 0.4 <= ratio <= 5:
            continue
        seen.add(im["src"])
        img = download(page, im["src"])
        if img is None:
            continue
        h = dhash(img)
        if is_duplicate(h, known):
            continue
        known.append(h)
        found.append(new_item(img, brand=brand, channel="공식몰", source_name=source_name,
                              page_url=url, src=im["src"], width=900,
                              extra={"alt": im["alt"][:200], "link_url": link, "slot": im["slot"]}))
        if len(found) >= c["max_banners_per_page"]:
            break
    log(f"  광고 배너 {len(found)}개")
    return found


# ── 상세페이지 ────────────────────────────────
def stitch(images: list[Image.Image], width: int = 860) -> Image.Image:
    resized = [im.resize((width, round(im.height * width / im.width)), Image.LANCZOS) for im in images]
    total = min(sum(i.height for i in resized), MAX_PDP_HEIGHT)
    canvas = Image.new("RGB", (width, total), "white")
    y = 0
    for im in resized:
        if y >= total:
            break
        canvas.paste(im, (0, y))
        y += im.height
    return canvas


def collect_pdp(page: Page, url: str, cfg: dict, known: list[str], *,
                brand: str, source_name: str) -> dict | None:
    if not page.evaluate(JS_IS_PRODUCT):
        log("  상품 페이지가 아니라서 건너뜀 (구매 버튼 없음)")
        PDP_STATS["구매버튼 없음"] += 1
        return None
    for label in ("상품정보 더보기", "상세정보 펼쳐보기", "상품설명 더보기", "상세정보 더보기", "더보기"):
        try:
            page.get_by_role("button", name=label, exact=True).first.click(timeout=1200)
            page.wait_for_timeout(800)
            break
        except PWError:
            continue
    scroll_through(page, steps=40, pause=450)

    detail = page.evaluate(JS_DETAIL_IMAGES, 600)
    if detail["total"] < 2500 or not detail["srcs"]:
        log("  상품 설명 이미지가 거의 없어서 건너뜀")
        PDP_STATS["설명 이미지 부족"] += 1
        return None
    parts = [p for p in (download(page, s) for s in dict.fromkeys(detail["srcs"])) if p is not None]
    if not parts:
        return None
    long_img = stitch(parts)

    h = dhash(long_img)
    if is_duplicate(h, known, threshold=2):
        log("  지난번과 같은 상세페이지라 건너뜀")
        PDP_STATS["같은 페이지"] += 1
        return None
    known.append(h)
    log(f"  상세페이지 캡처 (설명 이미지 {len(parts)}장, 세로 {long_img.height}px)")
    return new_item(long_img, brand=brand, channel="상세페이지", source_name=source_name,
                    page_url=url, src=url, width=600, quality=72,
                    extra={"page_title": page.title()[:120], "height_px": long_img.height,
                           "link_url": url})


# ── 사이트 열기 ───────────────────────────────
def url_variants(url: str) -> list[str]:
    """주소가 조금 다를 때를 대비해 www 유무, .co.kr/.com을 바꿔서도 시도해요."""
    u = urlparse(url)
    host = u.netloc
    hosts = [host, host[4:] if host.startswith("www.") else "www." + host]
    for h in list(hosts):
        if h.endswith(".co.kr"):
            hosts.append(h[:-6] + ".com")
        elif h.endswith(".com"):
            hosts.append(h[:-4] + ".co.kr")
    out = []
    for h in hosts:
        v = u._replace(netloc=h).geturl()
        if v not in out:
            out.append(v)
    return out


def safe_goto(page: Page, url: str, cfg: dict) -> bool:
    LAST_ERROR["reason"] = ""
    if not url:
        return False
    if not robots_allowed(url, cfg):
        LAST_ERROR["reason"] = "robots.txt에서 수집 금지"
        return False
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWError as e:
        msg = str(e).splitlines()[0]
        log(f"  페이지를 열지 못함 ({url}): {msg}")
        LAST_ERROR["reason"] = ("주소가 존재하지 않음" if "NAME_NOT_RESOLVED" in msg
                                else "응답 없음(시간 초과)" if "Timeout" in msg else "접속 오류")
        return False
    if resp is not None and resp.status >= 400:
        log(f"  페이지가 없음 (응답 {resp.status}): {url}")
        LAST_ERROR["reason"] = "접속 차단" if resp.status in (401, 403, 429) else f"페이지 없음({resp.status})"
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWError:
        pass                                    # 뒤에서 계속 뭔가 불러오는 사이트도 있어서 그냥 진행
    return True


def open_site(page: Page, url: str, cfg: dict) -> str:
    for i, v in enumerate(url_variants(url)):
        if i:
            log(f"  다른 주소로 다시 시도: {v}")
        if safe_goto(page, v, cfg):
            return v
    return ""


def is_official(page: Page, b: dict) -> bool:
    names = [b["name"].split(" (")[0], *b.get("aliases", [])]
    try:
        text = page.evaluate(JS_PAGE_ID).lower().replace(" ", "")
    except PWError:
        return False
    return any(n.lower().replace(" ", "") in text for n in names if n)


# ── 실행 ──────────────────────────────────────
def run(cfg: dict, known_hashes: list[str], recent_pdp: dict[str, str] | None = None,
        problems: list[str] | None = None) -> list[dict]:
    """recent_pdp: 최근 7일 안에 이미 확인한 상세페이지 주소 → 다시 안 찍어요."""
    if recent_pdp is None:
        recent_pdp = {}
    if problems is None:
        problems = []
    week_ago = (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d")
    pdp_budget = cfg["collect"].get("max_pdp_per_run", 15)
    limit = cfg["collect"].get("max_minutes", 60) * 60
    started = time.time()
    items: list[dict] = []

    brands = cfg.get("brands", [])
    if brands:                                   # 매일 시작 브랜드를 바꿔서 골고루
        k = datetime.now(KST).timetuple().tm_yday % len(brands)
        brands = brands[k:] + brands[:k]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ko-KR",
                                  user_agent=cfg["collect"]["user_agent"])
        page = ctx.new_page()

        for bi, b in enumerate(brands):
            if time.time() - started > limit:
                rest = ", ".join(x["name"].split(" (")[0] for x in brands[bi:])
                log(f"⏱ 수집 시간 {limit // 60}분이 지나 여기서 멈추고 저장해요. 남은 브랜드는 다음 실행 때: {rest}")
                break
            short = b["name"].split(" (")[0]
            brand_for_items = "" if b.get("detect_brand") else b["name"]
            category = b.get("category", "화장품")
            for entry in b.get("official_pages", []):
                url = entry.get("url", "") if isinstance(entry, dict) else entry
                if not url:
                    continue
                region = region_of(url, entry.get("region", "") if isinstance(entry, dict) else "")
                label = mall_label(short, region)
                log(f"[공식몰] {label} {url}")
                opened = open_site(page, url, cfg)
                if not opened:
                    problems.append(f"{short}: {LAST_ERROR['reason'] or '사이트를 열지 못함'} ({url})")
                    polite_wait(cfg)
                    continue
                if opened != url:
                    problems.append(f"{short}: 주소를 {opened} 로 바꿔주세요 (수집은 정상)")
                    url = opened
                if not is_official(page, b):
                    log("  ⚠ 페이지에서 브랜드 이름을 찾지 못해 건너뜀 (주소 확인 필요)")
                    problems.append(f"{short}: 공식몰인지 확인 안 됨 ({url})")
                    polite_wait(cfg)
                    continue

                banners = collect_banners(page, url, cfg, known_hashes,
                                          brand=brand_for_items, source_name=label)
                for it in banners:
                    it["region"], it["category"] = region, category
                items += banners

                # 상세페이지 후보: ① 배너가 연결된 상품 ② 메인에 걸린 상품 ③ config에 적은 주소
                try:
                    links = page.evaluate(JS_LINKS)
                except PWError:
                    links = []
                from_banner = {it["link_url"]: it for it in banners
                               if same_site(it["link_url"], url) and is_product_url(it["link_url"])}
                main_products = [u for u in dict.fromkeys(links) if same_site(u, url) and is_product_url(u)]
                n_auto = b.get("pdp_auto", 5)
                best_products: list[str] = []
                try:
                    best = page.evaluate(JS_BEST_LINK)
                except PWError:
                    best = ""
                if best and same_site(best, url) and safe_goto(page, best, cfg):
                    scroll_through(page, steps=3)
                    try:
                        best_products = [u for u in dict.fromkeys(page.evaluate(JS_LINKS))
                                         if same_site(u, url) and is_product_url(u)]
                    except PWError:
                        pass
                    log(f"  베스트 페이지에서 상품 {len(best_products)}개 발견")
                candidates = [u for u in dict.fromkeys(best_products + main_products)
                              if recent_pdp.get(u, "") < week_ago]       # 이번 주에 안 본 상품부터
                queue = list(from_banner) + candidates[:n_auto] + [u for u in b.get("pdp_pages", []) if u]

                for purl in dict.fromkeys(queue):
                    if pdp_budget <= 0 or time.time() - started > limit:
                        break
                    if recent_pdp.get(purl, "") >= week_ago:
                        continue
                    recent_pdp[purl] = today()
                    polite_wait(cfg)
                    log(f"[상세페이지] {short} {purl}")
                    if not safe_goto(page, purl, cfg):
                        continue
                    pdp_budget -= 1
                    it = collect_pdp(page, purl, cfg, known_hashes, brand=brand_for_items, source_name=label)
                    PDP_STATS["시도"] += 1
                    if not it:
                        continue
                    PDP_STATS["성공"] += 1
                    it["region"], it["category"] = region, category
                    if purl in from_banner:                 # 광고 ↔ 상세페이지 짝짓기
                        it["from_banner"] = from_banner[purl]["id"]
                        from_banner[purl]["linked_pdp"] = it["id"]
                    items.append(it)
                polite_wait(cfg)
        browser.close()
    log("상세페이지 결과: " + ", ".join(f"{k} {v}" for k, v in PDP_STATS.items()))
    return items
