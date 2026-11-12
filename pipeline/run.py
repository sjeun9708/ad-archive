"""매주 자동 실행되는 전체 흐름: 수집 → 분석 → 데이터 저장 → 인사이트 갱신.

직접 실행:  python ad-archive/pipeline/run.py
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

from common import DATA_FILE, load_config, load_items, save_items, log, today  # noqa: E402
import analyze  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-web", action="store_true")
    ap.add_argument("--skip-instagram", action="store_true")
    ap.add_argument("--analyze-only", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    items = load_items()
    known_hashes = [i["hash"] for i in items if i.get("hash")]
    known_ids = {i["id"] for i in items}
    log(f"기존 소재 {len(items)}개")

    new: list[dict] = []
    if not args.analyze_only:
        if not args.skip_web:
            import collect_web
            new += collect_web.run(cfg, known_hashes)
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
    log("끝")


if __name__ == "__main__":
    main()
