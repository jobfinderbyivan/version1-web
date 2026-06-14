"""Company research cards (spec 6.6) with a 30-day cache shared across users."""
import logging
from datetime import datetime, timedelta

from . import db, llm

log = logging.getLogger("research")

RESEARCH_SCHEMA = llm.obj_schema({
    "size": llm.STR,
    "glassdoor_rating": llm.STR,
    "recent_news": llm.STR,
    "mission": llm.STR,
    "notable_info": llm.STR,
})


def company_research(company_name: str, demo_rating=None, user_id=None) -> dict:
    name = (company_name or "").strip()
    if not name:
        return {}
    cached = db.query_one(
        "SELECT data, created_at FROM company_research_cache WHERE company_name = ?",
        (name.lower(),),
    )
    if cached:
        created = datetime.strptime(cached["created_at"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() - created < timedelta(days=30):
            return db.jloads(cached["data"], {})
        db.execute("DELETE FROM company_research_cache WHERE company_name = ?", (name.lower(),))

    data = None
    if demo_rating is None:  # real company — ask the LLM what it knows
        data = llm.complete_json(
            f"Compile a brief, factual research card about the company \"{name}\". Use only what "
            "you reliably know; when you are not confident about a field, return an empty string "
            "for it rather than guessing. Fields:\n"
            "- size: approximate employee count or scale (e.g. '500-1000 employees', 'Fortune 500')\n"
            "- glassdoor_rating: approximate public employer rating like '3.8/5' if widely known\n"
            "- recent_news: one notable recent news item if widely known\n"
            "- mission: one-line mission statement or company description\n"
            "- notable_info: funding, awards, or noteworthy facts",
            process_type="company_research",
            user_id=user_id,
            schema=RESEARCH_SCHEMA,
            max_tokens=600,
        )
    if data is None:
        data = {
            "size": "",
            "glassdoor_rating": f"{demo_rating}/5" if demo_rating else "",
            "recent_news": "",
            "mission": f"{name} — company research unavailable (no AI key / unknown company).",
            "notable_info": "",
        }
    db.execute(
        "INSERT OR REPLACE INTO company_research_cache (company_name, data, created_at) VALUES (?, ?, ?)",
        (name.lower(), db.jdumps(data), db.now()),
    )
    return data
