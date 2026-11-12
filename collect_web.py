"""공식몰 배너, 리테일 기획전 배너, 상세페이지를 자동으로 수집해요."""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright, Page, Error as PWError

from common import (
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
    for im in list_images(page, c["min_banner_width"]):
        ratio = im["w"] / im["h"]
        if not 0.4 <= ratio <= 5:            # 너무 가늘거나 긴 건 배너가 아님
            continue
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

    if not found:                             # 배경 이미지로 된 배너는 첫 화면을 캡처
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
def safe_goto(page: Page, url: str, cfg: dict) -> bool:
    if not url or not robots_allowed(url, cfg):
        return False
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWError as e:
        log(f"  페이지를 열지 못함 ({url}): {str(e).splitlines()[0]}")
        return url in page.url
    log(f"  열림: {page.title()[:60]!r}")
    return True


def run(cfg: dict, known_hashes: list[str]) -> list[dict]:
    items: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ko-KR",
                                  user_agent=cfg["collect"]["user_agent"])
        page = ctx.new_page()

        for b in cfg.get("brands", []):
            for url in [u for u in b.get("official_pages", []) if u]:
                log(f"[공식몰] {b['name']} {url}")
                if safe_goto(page, url, cfg):
                    items += collect_banners(page, url, cfg, known_hashes, brand=b["name"],
                                             channel="공식몰", source_name=f"{b['name']} 공식몰")
                polite_wait(cfg)
            for url in [u for u in b.get("pdp_pages", []) if u]:
                log(f"[상세페이지] {b['name']} {url}")
                if safe_goto(page, url, cfg):
                    it = collect_pdp(page, url, cfg, known_hashes, brand=b["name"],
                                     source_name=f"{b['name']} 공식몰")
                    items += [it] if it else []
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
