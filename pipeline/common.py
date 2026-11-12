"""공통 유틸: 설정, 데이터 파일, 이미지 처리, robots.txt 확인."""
from __future__ import annotations

import hashlib
import io
import json
import random
import time
import urllib.robotparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]          # 저장소 맨 위 폴더
DATA_FILE = ROOT / "data" / "items.json"
INSIGHT_FILE = ROOT / "data" / "insights.json"
IMG_DIR = ROOT / "images"
KST = timezone(timedelta(hours=9))

HOOKS = [
    "할인율 강조", "한정·마감", "성분·효능", "인증·임상", "리뷰·랭킹",
    "사은품", "비포애프터", "모델·셀럽", "신제품", "루틴 제안",
]
CHANNELS = ["공식몰", "인스타그램", "리테일 기획전", "상세페이지"]


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def load_items() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    items = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    # 처음 실제 데이터가 들어오면 샘플 데이터는 지워요.
    return [i for i in items if not i.get("sample")]


def save_items(items: list[dict]) -> None:
    items.sort(key=lambda i: i.get("collected_at", ""), reverse=True)
    DATA_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def polite_wait(cfg: dict) -> None:
    lo, hi = cfg["collect"]["delay_seconds"]
    time.sleep(random.uniform(lo, hi))


# ── robots.txt ────────────────────────────────
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def robots_allowed(url: str, cfg: dict) -> bool:
    if not cfg["collect"].get("respect_robots_txt", True):
        return True
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", timeout=10,
                             headers={"User-Agent": cfg["collect"]["user_agent"]})
            if r.status_code >= 400:
                _robots_cache[base] = None      # robots.txt 없음 → 허용
            else:
                rp.parse(r.text.splitlines())
                _robots_cache[base] = rp
        except requests.RequestException:
            _robots_cache[base] = None
    rp = _robots_cache[base]
    if rp is None:
        return True
    ok = rp.can_fetch(cfg["collect"]["user_agent"], url)
    if not ok:
        log(f"  robots.txt에서 막힌 주소라 건너뜀: {url}")
    return ok


# ── 이미지 ────────────────────────────────────
def open_image(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def dhash(img: Image.Image, size: int = 8) -> str:
    """비슷한 이미지를 같은 것으로 보기 위한 지문(difference hash)."""
    g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def is_duplicate(h: str, known: list[str], threshold: int = 4) -> bool:
    return any(hamming(h, k) <= threshold for k in known)


def make_id(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def save_web_image(img: Image.Image, name: str, width: int, quality: int = 80) -> str:
    """사이트에 올릴 이미지는 줄여서 저장해요(원본 그대로 공개하지 않기 위함)."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    w = min(width, img.width)
    h = round(img.height * w / img.width)
    out = img.resize((w, h), Image.LANCZOS)
    path = IMG_DIR / f"{name}.jpg"
    out.save(path, "JPEG", quality=quality, optimize=True, progressive=True)
    return f"images/{name}.jpg"


def slices_for_analysis(img: Image.Image, width: int = 800, slice_h: int = 1500,
                        max_slices: int = 10) -> list[Image.Image]:
    """세로로 긴 상세페이지를 AI가 읽을 수 있는 크기로 잘라요."""
    w = min(width, img.width)
    h = round(img.height * w / img.width)
    small = img.resize((w, h), Image.LANCZOS)
    parts = [small.crop((0, y, w, min(y + slice_h, h))) for y in range(0, h, slice_h)]
    if len(parts) > max_slices:                    # 너무 길면 고르게 골라서 보냄
        step = len(parts) / max_slices
        parts = [parts[int(i * step)] for i in range(max_slices)]
    return parts


def to_jpeg_bytes(img: Image.Image, max_side: int = 1568) -> bytes:
    if max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ── 국내/해외 구분 ────────────────────────────
COUNTRY_BY_TLD = {
    "kr": "국내", "us": "미국", "jp": "일본", "cn": "중국", "uk": "영국", "tw": "대만",
    "hk": "홍콩", "sg": "싱가포르", "th": "태국", "vn": "베트남", "id": "인도네시아",
    "my": "말레이시아", "ph": "필리핀", "au": "호주", "ca": "캐나다", "de": "독일",
    "fr": "프랑스", "es": "스페인", "it": "이탈리아", "nl": "네덜란드", "eu": "유럽",
}


def region_of(url: str, override: str = "") -> str:
    """주소 끝(.kr, .us 등)으로 나라를 알아내요. config에서 직접 정할 수도 있어요."""
    if override:
        return override
    from urllib.parse import urlparse
    host = urlparse(url).netloc.lower().split(":")[0]
    tld = host.rsplit(".", 1)[-1]
    if tld in COUNTRY_BY_TLD:
        return COUNTRY_BY_TLD[tld]
    first = host.removeprefix("www.").split(".")[0]      # us.brand.com, jp.brand.com 같은 형태
    if first in COUNTRY_BY_TLD:
        return COUNTRY_BY_TLD[first]
    return "해외"


def mall_label(brand: str, region: str) -> str:
    return f"{brand} {'국내' if region == '국내' else region} 공식몰"
