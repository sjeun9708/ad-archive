"""공식몰 배너, 리테일 기획전 배너, 상세페이지를 자동으로 수집해요."""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright, Page, Error as PWError

from common import (
    region_of, mall_label,
    log, today, polite_wait, robots_allowed, open_image, dhash, is_duplicate,
    make_id, save_web_image,
)

JS_IMAGES = """(minW) => Array.from(document.images)
  .filter(i => i.naturalWidth >= minW && i.naturalHeight >= 120)
  .map(i => ({src: i.currentSrc || i.src, w: i.naturalWidth, h: i.naturalHeight,
              alt: i.alt || '', top: i.getBoundingClientRect().top + window.scrollY}))"""

MAX_PDP_HEIGHT = 40000
DEBUG_DIR = Path(__file__).resolve().parents[1] / "data" / "debug"


def debug_shot(page: Page, name: str) -> None:
    """사이트가 어떤 화면을 보여줬는지 확인용으로 첫 화면을 저장해요 (data/debug 폴더)."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        img = open_image(page.screenshot())
        if img is not None:
            img.thumbnail((900, 900))
            img.save(DEBUG_DIR / f"{name}.jpg", "JPEG", quality=70)
        log(f"  화면 확인용 캡처 저장: data/debug/{name}.jpg (페이지 제목: {page.title()[:60]!r})")
    except PWError:
        pass


# 상품 링크가 href 대신 data-속성(goodsNo 등)에 들어 있는 사이트도 있어서 둘 다 찾아요.
DEFAULT_TEMPLATES = {
    "올리브영": "https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={id}",
}
JS_PRODUCT_LINKS = """({pat, tpl}) => {
  const out = [];
  const add = u => { if (u && !out.includes(u)) out.push(u); };
  document.querySelectorAll('a[href]').forEach(a => { if (a.href.includes(pat)) add(a.href); });
  if (tpl) {
    document.querySelectorAll('*').forEach(el => {
      for (const at of el.attributes) {
        if (/goods.?no/i.test(at.name) && /^[A-Z0-9]{6,}$/i.test(at.value)) add(tpl.replace('{id}', at.value));
      }
    });
  }
  return out;
}"""


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


def list_images(page: Page, min_w: int) -> list[dict]:
    seen, out = set(), []
    for im in page.evaluate(JS_IMAGES, min_w):
        if im["src"] in seen or im["src"].startswith("data:"):
            continue
        seen.add(im["src"])
        out.append(im)
    return sorted(out, key=lambda i: i["top"])


def download(page: Page, src: str) -> Image.Image | None:
    try:
        r = page.request.get(src, timeout=20000)
        return open_image(r.body()) if r.ok else None
    except PWError:
        return None


def new_item(img: Image.Image, *, brand: str, channel: str, source_name: str,
             page_url: str, src: str, width: int, quality: int = 80,
             extra: dict | None = None) -> dict:
    h = dhash(img)
    iid = make_id(channel, src, today())
    return {
        "id": iid,
        "brand": brand,
        "channel": channel,
        "source_name": source_name,
        "page_url": page_url,
        "image_src": src,
        "collected_at": today(),
        "hash": h,
        "image": save_web_image(img, iid, width, quality),
        "analysis": None,
        **(extra or {}),
    }


# ── 배너 ──────────────────────────────────────
def collect_banners(page: Page, url: str, cfg: dict, known: list[str], *,
                    brand: str, channel: str, source_name: str) -> list[dict]:
    c = cfg["collect"]
    scroll_through(page, steps=4)
    found = []
    candidates = 0
    for im in list_images(page, c["min_banner_width"]):
        ratio = im["w"] / im["h"]
        if not 0.4 <= ratio <= 5:            # 너무 가늘거나 긴 건 배너가 아님
            continue
        candidates += 1
        img = download(page, im["src"])
        if img is None:
            continue
        h = dhash(img)
        if is_duplicate(h, known):
            continue
        known.append(h)
        found.append(new_item(img, brand=brand, channel=channel, source_name=source_name,
                              page_url=url, src=im["src"], width=900,
                              extra={"alt": im["alt"][:200]}))
        if len(found) >= c["max_banners_per_page"]:
            break

    if not candidates:                        # 배너 이미지가 아예 없으면(배경 이미지형) 첫 화면을 캡처
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        img = open_image(page.screenshot(clip={"x": 0, "y": 0, "width": 1440, "height": 900}))
        if img is not None and not is_duplicate(dhash(img), known):
            known.append(dhash(img))
            found.append(new_item(img, brand=brand, channel=channel, source_name=source_name,
                                  page_url=url, src=url + "#first-screen", width=900))
    log(f"  배너 {len(found)}개")
    return found


# ── 상세페이지 ────────────────────────────────
def stitch(images: list[Image.Image], width: int = 860) -> Image.Image:
    resized = [im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
               for im in images]
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
    for label in ("상품정보 더보기", "상세정보 펼쳐보기", "상품설명 더보기"):
        try:
            page.get_by_role("button", name=label).first.click(timeout=1500)
            page.wait_for_timeout(800)
            break
        except PWError:
            continue
    scroll_through(page, steps=40, pause=500)

    # 국내 상세페이지는 대부분 '세로로 이어 붙인 이미지'라서 큰 이미지를 순서대로 이어 붙여요.
    tall = [im for im in list_images(page, 600) if im["h"] >= 700 or im["h"] / im["w"] > 1]
    parts = [p for p in (download(page, im["src"]) for im in tall) if p is not None]
    if parts:
        long_img = stitch(parts)
    else:                                     # 이미지가 아니라 HTML로 된 경우 화면 전체 캡처
        long_img = open_image(page.screenshot(full_page=True))
        if long_img is None:
            return None
        long_img = long_img.crop((0, 0, long_img.width, min(long_img.height, MAX_PDP_HEIGHT)))

    h = dhash(long_img)
    if is_duplicate(h, known, threshold=2):
        log("  지난번과 같은 상세페이지라 건너뜀")
        return None
    known.append(h)
    title = page.title()[:120]
    log(f"  상세페이지 캡처 (세로 {long_img.height}px)")
    return new_item(long_img, brand=brand, channel="상세페이지", source_name=source_name,
                    page_url=url, src=url, width=600, quality=72,
                    extra={"page_title": title, "height_px": long_img.height})


# ── 실행 ──────────────────────────────────────
LAST_ERROR = {"reason": ""}


def url_variants(url: str) -> list[str]:
    """주소가 조금 다를 때를 대비해 www 유무, .co.kr/.com을 바꿔서도 시도해요."""
    from urllib.parse import urlparse
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


def open_site(page: Page, url: str, cfg: dict) -> str:
    """열리는 주소를 찾아서 돌려줘요. 다 실패하면 빈 문자열."""
    for i, v in enumerate(url_variants(url)):
        if i:
            log(f"  다른 주소로 다시 시도: {v}")
        if safe_goto(page, v, cfg):
            return v
    return ""


def safe_goto(page: Page, url: str, cfg: dict) -> bool:
    LAST_ERROR["reason"] = ""
    if not url:
        return False
    if not robots_allowed(url, cfg):
        LAST_ERROR["reason"] = "robots.txt에서 수집 금지"
        return False
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if resp is not None and resp.status >= 400:
            log(f"  페이지가 없음 (응답 {resp.status}): {url}")
            LAST_ERROR["reason"] = "접속 차단" if resp.status in (401, 403, 429) else f"페이지 없음({resp.status})"
            return False
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWError as e:
        msg = str(e).splitlines()[0]
        log(f"  페이지를 열지 못함 ({url}): {msg}")
        if url in page.url:
            return True
        LAST_ERROR["reason"] = ("주소가 존재하지 않음" if "NAME_NOT_RESOLVED" in msg
                                else "응답 없음(시간 초과)" if "Timeout" in msg else "접속 오류")
        return False
    log(f"  열림: {page.title()[:60]!r}")
    return True


JS_PAGE_ID = """() => {
  const og = document.querySelector('meta[property="og:site_name"]');
  return [document.title || '', og ? og.content : '', location.host,
          (document.body ? document.body.innerText : '').slice(0, 1500)].join(' ');
}"""

JS_SHOP_LINKS = """(pats) => {
  const out = [];
  const here = location.host.replace(/^www\\./, '');
  for (const a of document.querySelectorAll('a[href]')) {
    let u; try { u = new URL(a.href); } catch { continue; }
    if (u.host.replace(/^www\\./, '') !== here) continue;
    const s = u.pathname + u.search;
    if (pats.some(p => s.toLowerCase().includes(p)) && !out.includes(u.href)) out.push(u.href);
  }
  return out;
}"""
PRODUCT_PATTERNS = ["/product/", "/products/", "/goods/", "goodsno=", "product_no=", "/item/", "/prd/", "/shop/detail"]


def is_official(page: Page, b: dict) -> bool:
    """열린 사이트가 정말 그 브랜드 공식몰인지 이름으로 확인해요 (엉뚱한 사이트 수집 방지)."""
    names = [b["name"], *b.get("aliases", [])]
    try:
        text = page.evaluate(JS_PAGE_ID).lower().replace(" ", "")
    except PWError:
        return False
    return any(n.lower().replace(" ", "") in text for n in names if n)


def run(cfg: dict, known_hashes: list[str], recent_pdp: dict[str, str] | None = None,
        problems: list[str] | None = None) -> list[dict]:
    """recent_pdp: 최근 7일 안에 이미 캡처한 상세페이지 주소 → 다시 안 찍어요."""
    from datetime import datetime, timedelta
    from common import KST
    if recent_pdp is None:
        recent_pdp = {}
    if problems is None:
        problems = []
    week_ago = (datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d")
    pdp_budget = cfg["collect"].get("max_pdp_per_run", 15)
    items: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ko-KR",
                                  user_agent=cfg["collect"]["user_agent"])
        page = ctx.new_page()

        import time
        started = time.time()
        limit = cfg["collect"].get("max_minutes", 60) * 60
        brands = cfg.get("brands", [])
        if brands:                                   # 매일 시작 브랜드를 바꿔서, 시간이 모자라도 골고루 수집
            k = datetime.now(KST).timetuple().tm_yday % len(brands)
            brands = brands[k:] + brands[:k]
        for bi, b in enumerate(brands):
            if time.time() - started > limit:
                rest = ", ".join(x["name"].split(" (")[0] for x in brands[bi:])
                log(f"⏱ 수집 시간 {limit // 60}분이 지나 여기서 멈추고 저장해요. 남은 브랜드는 다음 실행 때: {rest}")
                break
            brand_for_items = "" if b.get("detect_brand") else b["name"]   # 여러 브랜드가 모인 몰은 AI가 브랜드를 알아내요
            category = b.get("category", "화장품")
            for entry in b.get("official_pages", []):
                url = entry.get("url", "") if isinstance(entry, dict) else entry
                if not url:
                    continue
                region = region_of(url, entry.get("region", "") if isinstance(entry, dict) else "")
                label = mall_label(b["name"].split(" (")[0], region)
                log(f"[공식몰] {label} {url}")
                opened = open_site(page, url, cfg)
                if not opened:
                    problems.append(f"{b['name']}: {LAST_ERROR['reason'] or '사이트를 열지 못함'} ({url})")
                    polite_wait(cfg)
                    continue
                if opened != url:
                    log(f"  ✓ 이 주소로 열림 → config.yaml을 {opened} 로 바꿔두면 더 빨라요")
                    problems.append(f"{b['name']}: 주소를 {opened} 로 바꿔주세요 (수집은 정상)")
                    url = opened
                if not is_official(page, b):
                    log("  ⚠ 페이지에서 브랜드 이름을 찾지 못해 건너뜀 (주소 확인 필요)")
                    problems.append(f"{b['name']}: 공식몰인지 확인 안 됨 ({url})")
                    debug_shot(page, f"check_{b['name'].split(' (')[0]}")
                    polite_wait(cfg)
                    continue
                found = collect_banners(page, url, cfg, known_hashes, brand=brand_for_items,
                                        channel="공식몰", source_name=label)
                for it in found:
                    it["region"], it["category"] = region, category

                # 메인에 걸린 대표 제품 상세페이지도 자동으로 (일주일에 한 번)
                n_auto = b.get("pdp_auto", 2)
                pdp_urls = [u for u in b.get("pdp_pages", []) if u]
                if n_auto and pdp_budget > 0:
                    try:
                        pdp_urls += page.evaluate(JS_SHOP_LINKS, PRODUCT_PATTERNS)[:n_auto]
                    except PWError:
                        pass
                items += found
                for purl in dict.fromkeys(pdp_urls):
                    if pdp_budget <= 0:
                        break
                    if recent_pdp.get(purl, "") >= week_ago:
                        continue
                    recent_pdp[purl] = today()
                    polite_wait(cfg)
                    log(f"[상세페이지] {b['name']} {purl}")
                    if safe_goto(page, purl, cfg):
                        pdp_budget -= 1
                        it = collect_pdp(page, purl, cfg, known_hashes, brand=brand_for_items,
                                         source_name=label)
                        if it:
                            it["region"], it["category"] = region, category
                            items.append(it)
                polite_wait(cfg)

        for r in cfg.get("retail", []):
            log(f"[리테일 기획전] {r['name']}")
            if safe_goto(page, r["main_url"], cfg):
                promo = r["promo_menu_text"]
                clicked = False
                for loc in (page.get_by_role("link", name=promo, exact=True),
                            page.locator("a", has_text=promo),
                            page.get_by_text(promo, exact=True)):
                    try:
                        loc.first.click(timeout=5000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        clicked = True
                        break
                    except PWError:
                        continue
                if clicked and robots_allowed(page.url, cfg):
                    log(f"  기획전 페이지: {page.url}")
                    items += collect_banners(page, page.url, cfg, known_hashes, brand="",
                                             channel="리테일 기획전", source_name=f"{r['name']} 기획전")
                elif not clicked:
                    log(f"  '{promo}' 메뉴를 찾지 못함")
                    debug_shot(page, f"{r['name']}_main")
            polite_wait(cfg)

            if r.get("ranking_url") and safe_goto(page, r["ranking_url"], cfg):
                scroll_through(page, steps=3)
                links = page.evaluate(JS_PRODUCT_LINKS, {
                    "pat": r["product_link_pattern"],
                    "tpl": r.get("product_url_template") or DEFAULT_TEMPLATES.get(r["name"], ""),
                })[: r.get("ranking_top_n", 10)]
                if not links:
                    debug_shot(page, f"{r['name']}_ranking")
                log(f"[랭킹] {r['name']} 상위 {len(links)}개 상세페이지")
                for rank, url in enumerate(links, 1):
                    polite_wait(cfg)
                    if safe_goto(page, url, cfg):
                        it = collect_pdp(page, url, cfg, known_hashes, brand="",
                                         source_name=f"{r['name']} 랭킹 {rank}위")
                        if it:
                            it["rank"] = rank
                            items.append(it)
        browser.close()
    return items
