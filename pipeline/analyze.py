"""수집한 소재를 Claude로 분석해요: 후킹 유형, 카피, 레이아웃, 톤, 상세페이지 구조."""
from __future__ import annotations

import base64
import json
import os
from collections import Counter
from datetime import datetime, timedelta

import anthropic
from PIL import Image

from common import (
    ROOT, HOOKS, INSIGHT_FILE, KST, log, slices_for_analysis, to_jpeg_bytes,
)

PROOFS = ["임상 시험", "성분 설명", "비포애프터", "리뷰 캡처", "수상·랭킹", "전문가 추천", "인증 마크", "모델·셀럽"]

SYSTEM = f"""당신은 K-뷰티 브랜드의 퍼포먼스 마케터이자 상세페이지 기획자입니다.
광고 이미지를 보고 '무엇으로 시선을 잡는지'와 '어떻게 디자인했는지'를 분석합니다.
반드시 JSON 객체 하나만 출력하고, 앞뒤에 설명이나 ``` 표시를 붙이지 마세요.
후킹 유형은 다음 중에서만 고릅니다: {", ".join(HOOKS)}"""

BASE_FIELDS = """  "is_brand_ad": true,              // 특정 화장품 브랜드의 광고·홍보 소재면 true. 판매처(올리브영 등)의 공통 행사 배너, 공지, 로고뿐인 이미지는 false
  "brand_detected": "",             // 이미지에서 보이는 브랜드명 (한국어 표기)
  "product": "",                    // 제품명 또는 제품군
  "product_type": "",               // 화장품 / 디바이스 중 하나 (뷰티 기기면 디바이스)
  "hooks": {"primary": "", "secondary": []},
  "copy": {"main": "", "sub": "", "cta": ""},   // 이미지 속 글자 그대로
  "visual_focus": "",               // 제품 / 모델 / 텍스트 / 혼합 중 하나
  "layout": "",                     // 배치 특징 한 문장 (예: 제품 중앙, 할인 숫자를 최대 크기로)
  "tone": [],                       // 톤앤무드 키워드 2~3개
  "target": "",                     // 추정 타깃 한 문장
""" + '  "why_it_hooks": ""'

BASE_SCHEMA = "{\n" + BASE_FIELDS + "               // 왜 눈길을 끄는지 1~2문장\n}"

PDP_SCHEMA = "{\n" + BASE_FIELDS + f""",            // 왜 눈길을 끄는지 1~2문장
  "pdp": {{
    "first_screen_hook": "",        // 첫 화면이 어떻게 시선을 잡는지 한 문장
    "sections": [                   // 위에서 아래 순서대로, 4~12개
      {{"name": "", "role": "", "copy": "", "share": 0}}   // role 예: 공감 후킹, 해결책 제시, 근거, 사회적 증거, 행동 유도, 구매 결정. share는 전체 길이 중 비중(%)
    ],
    "proofs": {{{", ".join(f'"{p}": false' for p in PROOFS)}}}
  }}
}}"""


def b64(img: Image.Image) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(to_jpeg_bytes(img)).decode()}}


def parse_json(text: str) -> dict:
    t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(t[t.find("{"): t.rfind("}") + 1])


def palette(img: Image.Image, k: int = 5) -> list[dict]:
    """이미지에서 많이 쓰인 색 k개와 비율을 직접 계산해요."""
    small = img.copy()
    small.thumbnail((200, 200))
    q = small.quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[: k * 3]
    counts = sorted(q.getcolors(), reverse=True)
    total = sum(c for c, _ in counts)
    return [{"hex": "#{:02X}{:02X}{:02X}".format(*pal[i * 3: i * 3 + 3]),
             "pct": round(c * 100 / total)} for c, i in counts]


SAVE_TOOL = {
    "name": "save_result",
    "description": "분석 결과를 저장합니다. 요청받은 JSON 형식 그대로 입력하세요.",
    "input_schema": {"type": "object", "additionalProperties": True},
}


def ask_json(client: anthropic.Anthropic, model: str, content, max_tokens: int = 3000) -> dict:
    """결과를 '도구 입력' 형태로 받아서 JSON이 깨지지 않게 해요. 그래도 실패하면 최대 3번 시도."""
    last = None
    for _ in range(3):
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=SYSTEM,
            tools=[SAVE_TOOL], tool_choice={"type": "tool", "name": "save_result"},
            messages=[{"role": "user", "content": content}])
        for b in resp.content:
            if b.type == "tool_use" and isinstance(b.input, dict) and b.input:
                return b.input
        text = "".join(getattr(b, "text", "") for b in resp.content)
        try:
            return parse_json(text)
        except (json.JSONDecodeError, ValueError) as e:
            last = e
    raise ValueError(f"분석 결과를 읽지 못함: {last}")


def analyze_item(client: anthropic.Anthropic, model: str, item: dict) -> dict:
    img = Image.open(ROOT / item["image"]).convert("RGB")
    is_pdp = item["channel"] == "상세페이지"
    context = [f"채널: {item['channel']}", f"출처: {item['source_name']}"]
    if item.get("brand"):
        context.append(f"수집 대상 브랜드: {item['brand']}")
    if item.get("page_title"):
        context.append(f"페이지 제목: {item['page_title']}")
    if item.get("caption"):
        context.append(f"인스타 캡션: {item['caption'][:400]}")

    if is_pdp:
        images = slices_for_analysis(img, width=720)
        ask = ("다음은 하나의 상품 상세페이지를 위에서부터 순서대로 자른 이미지들입니다. "
               "전체를 하나의 긴 광고로 보고, 섹션 구조와 설득 흐름까지 분석하세요.")
        schema = PDP_SCHEMA
    else:
        images = [img]
        ask = "다음 광고 이미지를 분석하세요."
        schema = BASE_SCHEMA

    content = [b64(i) for i in images] + [{
        "type": "text",
        "text": f"{ask}\n\n" + "\n".join(context) + f"\n\nsave_result 도구에 아래 형식 그대로 담아주세요 (// 주석은 빼고):\n{schema}",
    }]
    result = ask_json(client, model, content, max_tokens=3000)
    result["palette"] = palette(img if not is_pdp else images[0])
    return result


def run(items: list[dict], cfg: dict) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("[분석] ANTHROPIC_API_KEY가 없어 분석을 건너뜀")
        return 0
    client = anthropic.Anthropic()
    model = cfg["analysis"]["model"]
    todo = [i for i in items if i.get("analysis") is None][: cfg["analysis"]["max_items_per_run"]]
    log(f"[분석] {len(todo)}개 분석 시작 ({model})")
    done = 0
    for it in todo:
        try:
            a = analyze_item(client, model, it)
        except Exception as e:                        # 하나가 실패해도 나머지는 계속
            log(f"  분석 실패 {it['id']}: {e}")
            continue
        it["analysis"] = a
        if not it.get("brand") and a.get("brand_detected"):
            it["brand"] = a["brand_detected"]
        done += 1
    log(f"  분석 완료 {done}개")
    return done


# ── 이번 달 인사이트 ──────────────────────────
def write_insights(items: list[dict], cfg: dict) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return
    since = (datetime.now(KST) - timedelta(days=28)).strftime("%Y-%m-%d")
    recent = [i for i in items if i.get("analysis") and i["analysis"].get("is_brand_ad", True)
              and i["collected_at"] >= since]
    if len(recent) < 5:
        return
    focus = cfg.get("focus_brand", "")
    hook = Counter(i["analysis"]["hooks"]["primary"] for i in recent)
    by_channel = {ch: dict(Counter(i["analysis"]["hooks"]["primary"] for i in recent if i["channel"] == ch))
                  for ch in {i["channel"] for i in recent}}
    by_brand = {b: dict(Counter(i["analysis"]["hooks"]["primary"] for i in recent if i["brand"] == b))
                for b in {i["brand"] for i in recent}}
    pdp_first = [i["analysis"].get("pdp", {}).get("first_screen_hook", "") for i in recent
                 if i["channel"] == "상세페이지"][:15]
    stats = {"기간": f"{since} 이후", "소재 수": len(recent), "후킹 비율": dict(hook),
             "채널별 후킹": by_channel, "브랜드별 후킹": by_brand,
             "상세페이지 첫 화면 후킹": pdp_first}

    client = anthropic.Anthropic()
    prompt = (f"최근 4주간 수집한 뷰티 광고 소재 통계입니다. 기준 브랜드는 '{focus}'입니다.\n"
              f"{json.dumps(stats, ensure_ascii=False)}\n\n"
              "이 데이터에서만 확인되는 사실로 인사이트 3개를 뽑으세요. 하나는 반드시 기준 브랜드와 경쟁사의 차이를 다루세요. "
              "숫자를 지어내지 말고 통계에 있는 숫자만 쓰세요.\n"
              '형식: {"insights": [{"tag": "변화|채널 차이|경쟁 비교|상세페이지", "title": "한 문장", "body": "1~2문장"}]}')
    try:
        data = ask_json(client, cfg["analysis"]["model"], prompt, max_tokens=1500)
    except Exception:
        return
    data["updated_at"] = datetime.now(KST).strftime("%Y-%m-%d")
    INSIGHT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log("[인사이트] 갱신 완료")
