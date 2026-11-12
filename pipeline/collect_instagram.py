"""인스타그램 공식 API(Business Discovery)로 경쟁 브랜드 게시물을 가져와요.

필요한 것 (GitHub Secrets):
  IG_USER_ID       내 인스타 비즈니스/크리에이터 계정의 ID
  IG_ACCESS_TOKEN  Meta 개발자 앱에서 발급한 장기 액세스 토큰 (약 60일마다 갱신 필요)
"""
from __future__ import annotations

import os

import requests

from common import log, today, open_image, dhash, is_duplicate, make_id, save_web_image

FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count"


def fetch(username: str, cfg: dict) -> list[dict]:
    ver = cfg["instagram"]["graph_api_version"]
    n = cfg["instagram"]["posts_per_brand"]
    url = f"https://graph.facebook.com/{ver}/{os.environ['IG_USER_ID']}"
    params = {
        "fields": f"business_discovery.username({username}){{media.limit({n}){{{FIELDS}}}}}",
        "access_token": os.environ["IG_ACCESS_TOKEN"],
    }
    r = requests.get(url, params=params, timeout=30)
    if r.status_code != 200:
        msg = r.json().get("error", {}).get("message", r.text[:200])
        log(f"  인스타 API 오류 (@{username}): {msg}")
        return []
    return r.json().get("business_discovery", {}).get("media", {}).get("data", [])


def run(cfg: dict, known_hashes: list[str], known_ids: set[str]) -> list[dict]:
    if not (os.environ.get("IG_USER_ID") and os.environ.get("IG_ACCESS_TOKEN")):
        log("[인스타그램] 토큰이 없어 건너뜀 (README의 인스타 설정 참고)")
        return []
    items = []
    for b in cfg.get("brands", []):
        user = (b.get("instagram") or "").lstrip("@")
        if not user:
            continue
        log(f"[인스타그램] {b['name']} @{user}")
        for m in fetch(user, cfg):
            iid = make_id("ig", m["id"])
            if iid in known_ids:
                continue
            src = m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url")
            if not src:
                continue
            try:
                img = open_image(requests.get(src, timeout=30).content)
            except requests.RequestException:
                img = None
            if img is None:
                continue
            h = dhash(img)
            if is_duplicate(h, known_hashes):
                continue
            known_hashes.append(h)
            items.append({
                "id": iid,
                "brand": b["name"],
                "channel": "인스타그램",
                "source_name": f"@{user}",
                "page_url": m.get("permalink", ""),
                "collected_at": today(),
                "posted_at": (m.get("timestamp") or "")[:10],
                "media_type": m.get("media_type"),
                "caption": (m.get("caption") or "")[:600],
                "likes": m.get("like_count"),
                "comments": m.get("comments_count"),
                "hash": h,
                "image": save_web_image(img, iid, 900),
                "analysis": None,
            })
    log(f"  인스타 게시물 {len(items)}개")
    return items
