"""매주 자동 실행되는 전체 흐름: 수집 → 분석 → 데이터 저장 → 인사이트 갱신.

직접 실행:  python pipeline/run.py
옵션:       --skip-web        공식몰·리테일 수집 건너뛰기
            --skip-instagram  인스타 수집 건너뛰기
            --analyze-only    새로 수집하지 않고 미분석 소재만 분석
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import DATA_FILE, load_config, load_items, save_items, log, today, region_of, mall_label  # noqa: E402
import analyze  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-web", action="store_true")
    ap.add_argument("--skip-instagram", action="store_true")
    ap.add_argument("--analyze-only", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    items = load_items()
    for it in items:                       # 예전에 모은 공식몰 소재에도 국내/해외 표시 붙이기
        if it.get("channel") == "공식몰" and not it.get("region") and it.get("page_url"):
            it["region"] = region_of(it["page_url"])
            it["source_name"] = mall_label(it.get("brand", ""), it["region"])
    known_hashes = [i["hash"] for i in items if i.get("hash")]
    known_ids = {i["id"] for i in items}
    log(f"기존 소재 {len(items)}개")

    new: list[dict] = []
    problems: list[str] = []
    if not args.analyze_only:
        if not args.skip_web:
            import collect_web
            checked_file = DATA_FILE.parent / "pdp_checked.json"
            recent_pdp = json.loads(checked_file.read_text(encoding="utf-8")) if checked_file.exists() else {}
            new += collect_web.run(cfg, known_hashes, recent_pdp, problems)
            checked_file.write_text(json.dumps(recent_pdp, ensure_ascii=False, indent=0), encoding="utf-8")
        if not args.skip_instagram:
            import collect_instagram
            new += collect_instagram.run(cfg, known_hashes, known_ids)
    new = [n for n in new if n["id"] not in known_ids]
    items = new + items
    log(f"새 소재 {len(new)}개 수집")
    save_items(items)                      # 분석 전에 한 번 저장 (중간에 멈춰도 수집분은 남게)

    if analyze.run(items, cfg):
        save_items(items)
        analyze.write_insights(items, cfg)

    meta = {"focus_brand": cfg.get("focus_brand", ""), "last_run": today(),
            "brands": [b["name"] for b in cfg.get("brands", [])]}
    (DATA_FILE.parent / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if problems:
        log("──────── 확인이 필요한 브랜드 ────────")
        for p in problems:
            log(f"  {p}")
        log("위 브랜드는 config.yaml의 주소를 확인해 주세요. 나머지는 정상 수집됐어요.")
    log("끝")


if __name__ == "__main__":
    main()
