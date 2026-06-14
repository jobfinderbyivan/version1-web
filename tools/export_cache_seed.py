"""Export the employer ATS-board cache to seed/company_ats_cache.json.

This file is committed to the repo so a fresh deployment boots with all the
direct-link discovery work already in place (1,300+ employer boards) instead
of starting empty. It contains only public company->ATS-platform mappings — no
user data, resumes, jobs, or personal information.

Run this whenever you've meaningfully grown the cache locally and want the
hosted app to inherit it:  python tools/export_cache_seed.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db  # noqa: E402


def main():
    db.init_db()
    rows = db.query(
        "SELECT company_key, ats, slug, board_found, domain, created_at "
        "FROM company_ats_cache ORDER BY id")
    seed_dir = config.BASE_DIR / "seed"
    seed_dir.mkdir(exist_ok=True)
    out = seed_dir / "company_ats_cache.json"
    out.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    boards = sum(1 for r in rows if r["board_found"])
    print(f"Wrote {len(rows)} rows ({boards} verified boards) -> {out} "
          f"({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
