"""The scheduled automation engine (spec section 6) plus the auxiliary
scheduled jobs: urgency alerts (8), portfolio validation (13.2), monthly
LinkedIn re-analysis (11.3) and token budget alerts (5.2.5)."""
import logging
from datetime import date, datetime, timedelta

from . import (config, db, email_compose, emailer, geo, goals, job_search,
               link_resolver, llm, market, matching, research)
from .link_resolver import normalize_company

log = logging.getLogger("automation")

CADENCE_DAYS = {"daily": 1, "every_3_days": 3, "weekly": 7}
MAX_LLM_EVALUATIONS_PER_USER = 14  # cost control: only the best candidates reach the LLM


# ----------------------------------------------------------------- helpers

def cadence_days(user) -> int:
    return CADENCE_DAYS.get(user.get("email_frequency") or "every_3_days", 3)


def is_due(user) -> bool:
    if not user.get("last_email_sent_at"):
        return True
    last = datetime.strptime(user["last_email_sent_at"], "%Y-%m-%d %H:%M:%S")
    return datetime.now() - last >= timedelta(days=cadence_days(user), hours=-2)


def build_profile_text(user, resumes) -> str:
    parts = []
    for r in resumes[:3]:
        parts.append(f"[Resume: {r['label']}]\n{(r['raw_text'] or '')[:2500]}")
    if not resumes and user.get("resume_raw_text"):
        parts.append(user["resume_raw_text"][:4000])
    skills = db.jloads(user.get("skills"), [])
    extras = [
        ("Skills", ", ".join(map(str, skills))),
        ("Experience level", user.get("experience_level")),
        ("Bio", user.get("bio")),
        ("Preferred positions", user.get("preferred_positions")),
        ("Additional skills", user.get("additional_skills")),
        ("Feedback notes from the candidate", user.get("feedback_notes")),
        ("Industry preferences", ", ".join(db.jloads(user.get("industry_preferences"), []))),
    ]
    for label, value in extras:
        if value:
            parts.append(f"{label}: {value}")
    return "\n\n".join(parts)


DISABILITY_SCHEMA = llm.obj_schema({
    "detected": {"type": "boolean"},
    "note_terms": llm.STR,
    "extra_queries": llm.STR_ARR,
})


def detect_disability(user) -> dict:
    """Conservative bio scan (spec 6.8). Additive only — never filters jobs."""
    bio = (user.get("bio") or "").strip()
    if len(bio) < 15:
        return {"detected": False}
    result = llm.complete_json(
        "Analyze this short bio for CLEAR, EXPLICIT mentions of a physical disability, mobility "
        "limitation, dexterity limitation or chronic pain. Be conservative — when in doubt or the "
        "phrasing is vague, return detected=false. If detected, suggest 2-3 supplementary job "
        "search queries targeting sedentary/desk-based/remote roles suited to the condition, and "
        "a short respectful phrase describing their needs WITHOUT using the word 'wheelchair' "
        "(e.g. 'your mobility needs').\n\nBio:\n" + bio[:1500],
        process_type="job_matching", user_id=user["id"],
        schema=DISABILITY_SCHEMA, max_tokens=400,
    )
    if not result or not result.get("detected"):
        return {"detected": False}
    return {"detected": True,
            "note_terms": (result.get("note_terms") or "your accessibility needs").replace("wheelchair", "mobility"),
            "extra_queries": (result.get("extra_queries") or [])[:3]}


QUERY_SCHEMA = llm.obj_schema({"queries": llm.STR_ARR})


def generate_queries(user, resumes, *, max_queries=5) -> list:
    """Skills-based queries — what the user COULD be hired for (spec 6.2)."""
    profiles = resumes[:3] if resumes else [None]
    queries: list = []
    for resume in profiles:
        skills = db.jloads(resume["skills"] if resume else user.get("skills"), [])
        n = max(2, max_queries // len(profiles))
        result = llm.complete_json(
            "You are a job search strategist. Based on this candidate's transferable skills and "
            f"preferences, produce {n} distinct job search queries (just role names, e.g. "
            "'Operations Manager'). Do NOT simply repeat their past job titles — focus on roles "
            "they could realistically be hired for. Incorporate their keywords and industry "
            "preferences where sensible.\n\n"
            f"Skills: {', '.join(map(str, skills))[:800]}\n"
            f"Experience level: {(resume or {}).get('experience_level') or user.get('experience_level')}\n"
            f"Preferred positions: {user.get('preferred_positions') or '-'}\n"
            f"Bio: {(user.get('bio') or '-')[:600]}\n"
            f"Feedback notes: {(user.get('feedback_notes') or '-')[:400]}\n"
            f"Search keywords: {', '.join(db.jloads(user.get('search_keywords'), []))}\n"
            f"Industries: {', '.join(db.jloads(user.get('industry_preferences'), []))}",
            process_type="job_matching", user_id=user["id"],
            schema=QUERY_SCHEMA, max_tokens=400,
        )
        if result and result.get("queries"):
            queries += [q.strip() for q in result["queries"] if q and q.strip()][:n]
    if not queries:  # heuristic fallback
        positions = [p.strip() for p in (user.get("preferred_positions") or "").split(",") if p.strip()]
        queries += positions[:3]
        skills = db.jloads(user.get("skills"), [])
        for s in skills[:3]:
            queries.append(f"{s} specialist")
        if not queries:
            queries = ["customer service", "operations"]
    # user keywords guarantee (spec: at least one query uses them)
    for kw in db.jloads(user.get("search_keywords"), [])[:2]:
        if not any(kw.lower() in q.lower() for q in queries):
            queries.append(str(kw))
    # de-dup, cap
    seen, out = set(), []
    for q in queries:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out[:max_queries + 2]


def not_interested_patterns(user_id) -> dict:
    """Companies with 3+ 'not interested' marks get deprioritised (spec 6.5.6)."""
    rows = db.query(
        "SELECT LOWER(company_name) AS c, COUNT(*) AS n FROM job_history "
        "WHERE user_id = ? AND not_interested = 1 GROUP BY LOWER(company_name) HAVING n >= 3",
        (user_id,))
    return {r["c"] for r in rows}


def previously_sent(user_id) -> dict:
    """Map of normalized (company, title, city) -> most recent date_emailed."""
    rows = db.query(
        "SELECT company_name, job_title, location, MAX(date_emailed) AS d "
        "FROM job_history WHERE user_id = ? GROUP BY company_name, job_title, location",
        (user_id,))
    out = {}
    for r in rows:
        key = dedup_key(r["company_name"], r["job_title"], r["location"])
        if key not in out or r["d"] > out[key]:
            out[key] = r["d"]
    return out


def _fuzzy_company_match(company: str, names: list) -> bool:
    c = (company or "").lower().strip()
    return any(n and (n in c or c in n) for n in (str(x).lower().strip() for x in names))


def normalize_location(location: str) -> str:
    """City-level key: 'Salt Lake City, UT' == 'Salt Lake City, Salt Lake County'."""
    return (location or "").split(",")[0].strip().lower()


def dedup_key(company: str, title: str, location: str) -> tuple:
    return (normalize_company(company), (title or "").strip().lower(), normalize_location(location))


def _merge_duplicate(kept: dict, dup: dict) -> None:
    """Same job found via another source: keep the best of both — most
    importantly, upgrade to a direct employer link when one source has it
    (spec 6.3), keeping the board link as the secondary reference."""
    kept_direct = link_resolver.is_direct_link(kept.get("apply_link", ""))
    dup_direct = link_resolver.is_direct_link(dup.get("apply_link", ""))
    if dup_direct and not kept_direct:
        kept["aggregate_link"] = kept.get("aggregate_link") or kept.get("apply_link")
        kept["apply_link"] = dup["apply_link"]
    elif kept_direct and not dup_direct and not kept.get("aggregate_link"):
        kept["aggregate_link"] = dup.get("apply_link")
    if not kept.get("salary") and dup.get("salary"):
        kept["salary"] = dup["salary"]
        kept["salary_source"] = dup.get("salary_source")
    if kept.get("latitude") is None and dup.get("latitude") is not None:
        kept["latitude"], kept["longitude"] = dup["latitude"], dup["longitude"]
    if not kept.get("applicant_count") and dup.get("applicant_count"):
        kept["applicant_count"] = dup["applicant_count"]
    if not kept.get("company_domain") and dup.get("company_domain"):
        kept["company_domain"] = dup["company_domain"]


# -------------------------------------------------------------- main cycle

def run_main_cycle(force_user_id=None) -> dict:
    """Process every active user whose cadence is due. Returns a summary."""
    if not db.acquire_lock("main_cycle"):
        db.log("main", None, "Skipped: previous run still in progress")
        return {"skipped": True, "reason": "previous run in progress"}
    summary = {"processed": 0, "emailed": 0, "skipped": 0, "errors": 0}
    try:
        users = db.query("SELECT * FROM users WHERE is_paused = 0")
        for user in users:
            if force_user_id and user["id"] != force_user_id:
                continue
            if not force_user_id and not is_due(user):
                continue
            if not (user.get("resume_raw_text") or "").strip():
                db.log("main", user["id"], "Skipped: no resume on file")
                summary["skipped"] += 1
                continue
            try:
                sent = process_user(user)
                summary["processed"] += 1
                summary["emailed"] += 1 if sent else 0
            except Exception as exc:
                log.exception("Automation failed for user %s", user["id"])
                db.log("main", user["id"], f"ERROR: {exc}")
                summary["errors"] += 1
    finally:
        db.release_lock("main_cycle")
    db.log("main", None, f"Cycle finished: {summary}")
    return summary


def process_user(user) -> bool:
    """Full pipeline for one user. Returns True if an email was sent."""
    uid = user["id"]
    days = cadence_days(user)
    resumes = db.query("SELECT * FROM user_resumes WHERE user_id = ? ORDER BY is_primary DESC, id DESC", (uid,))
    profile_text = build_profile_text(user, resumes)

    disability = detect_disability(user)
    queries = generate_queries(user, resumes)
    db.log("main", uid, f"Queries: {queries}" + (" (+accessibility)" if disability["detected"] else ""))

    remote_ok = bool(user.get("job_type_remote"))
    local_ok = any(user.get(k) for k in ("job_type_onsite", "job_type_hybrid", "job_type_fulltime",
                                         "job_type_parttime", "job_type_contract")) or not remote_ok

    raw_jobs = []
    if local_ok and (user.get("city") or user.get("state")):
        for q in queries:
            raw_jobs += job_search.search_jobs(q, user.get("city") or "", user.get("state") or "",
                                               remote=False, radius_miles=user.get("search_radius_miles") or 15,
                                               max_days_old=max(days, config.SEARCH_RECENCY_DAYS))
    if remote_ok:
        for q in queries[:3]:  # spec: 2-3 queries for country-wide remote search
            raw_jobs += job_search.search_jobs(q, user.get("city") or "", user.get("state") or "",
                                               remote=True, max_days_old=max(days, config.SEARCH_RECENCY_DAYS))
    if disability["detected"]:
        for q in disability.get("extra_queries", [])[:2]:  # additive only
            raw_jobs += job_search.search_jobs(q, user.get("city") or "", user.get("state") or "",
                                               remote=remote_ok, max_days_old=max(days, config.SEARCH_RECENCY_DAYS))
    db.log("main", uid, f"Collected {len(raw_jobs)} raw listings")

    candidates = _filter_candidates(user, raw_jobs, days)
    db.log("main", uid, f"{len(candidates)} candidates after filters/dedup")
    if not candidates:
        db.log("main", uid, "No qualifying jobs this cycle — no email sent")
        market.compute_market_health(user, 0, days)
        return False

    analyzed = _evaluate_candidates(user, profile_text, candidates)
    qualifying = [j for j in analyzed if j["hire_score"] >= 5 and not j.get("below_minimum")]
    db.log("main", uid, f"{len(qualifying)} passed hire-likelihood >= 5 of {len(analyzed)} evaluated")

    market.record_cycle_data(user, analyzed)
    health = market.compute_market_health(user, len(qualifying), days)

    if not qualifying:
        db.log("main", uid, "Zero qualifying jobs — skipping email (no padding with weak matches)")
        return False

    for job in qualifying:
        job["final_score"] = matching.composite_score(job, user)
    qualifying.sort(key=lambda j: -j["final_score"])
    top = qualifying[:5]

    upgraded = link_resolver.upgrade_links(top, user_id=uid)  # spec 6.3
    if upgraded:
        db.log("main", uid, f"Upgraded {upgraded} job(s) to direct employer links")

    for job in top:  # research + geocode only for jobs we actually send
        job["company_research"] = research.company_research(
            job["company"], demo_rating=job.get("_demo_rating"), user_id=uid)
        if job.get("latitude") is None and job.get("location") and job["location"].lower() != "remote":
            coords = geo.geocode(job["location"])
            if coords:
                job["latitude"], job["longitude"] = coords

    cert_recs = update_cert_recommendations(user)
    advice = resume_advice_paragraph(user, top, analyzed)
    disability_note = None
    if disability["detected"]:
        suited = [str(i + 1) for i, j in enumerate(top) if j.get("_accessibility_friendly")]
        if suited:
            disability_note = (
                f"Based on what you shared in your profile about {disability['note_terms']}, we think "
                f"job{'s' if len(suited) > 1 else ''} #{', #'.join(suited)} "
                f"{'are' if len(suited) > 1 else 'is'} particularly well-suited for you. All "
                f"{len(top)} jobs above are strong matches for your skills — these just stood out.")

    sent_jobs = _insert_history(user, top)
    subject, body = email_compose.compose_job_email(
        user, sent_jobs, health=health, disability_note=disability_note,
        cert_recommendations=cert_recs, resume_advice=advice)
    delivered = emailer.send(user, "job_listing", subject, body)

    updates = ["last_email_sent_at = ?", "first_email_sent = 1"]
    params = [db.now()]
    if advice is not None:
        updates.append("resume_advice_pending = 0")
    db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", (*params, uid))
    db.log("main", uid, f"Sent {len(sent_jobs)} jobs (email delivered: {delivered})")
    return True


def _filter_candidates(user, raw_jobs, days) -> list:
    uid = user["id"]
    blacklist = db.jloads(user.get("blacklisted_companies"), [])
    whitelist = db.jloads(user.get("whitelisted_companies"), [])
    negatives = [str(k).lower() for k in db.jloads(user.get("negative_keywords"), [])]
    avoid_companies = not_interested_patterns(uid)
    history = previously_sent(uid)
    contract_ok = bool(user.get("job_type_contract"))
    user_benefits = set(db.jloads(user.get("preferred_benefits"), []))
    minimum = matching.parse_salary_number(user.get("minimum_salary") or "")

    home_coords = None
    if user.get("city"):
        home_coords = geo.geocode(", ".join(p for p in (user.get("city"), user.get("state")) if p))

    seen_batch: dict = {}
    out = []
    for job in raw_jobs:
        title, company = job.get("title", ""), job.get("company", "")
        if not title or not job.get("apply_link"):
            continue
        # free learning: any direct ATS link reveals this company's board
        if link_resolver.is_direct_link(job["apply_link"]):
            link_resolver.learn_from_url(company, job["apply_link"])

        key = dedup_key(company, title, job.get("location"))
        if key in seen_batch:
            _merge_duplicate(seen_batch[key], job)
            continue
        seen_batch[key] = job
        if job.get("posted_days_ago", 0) > max(days, config.SEARCH_RECENCY_DAYS):
            continue

        signals = job_search.detect_signals(title, job.get("description"))
        job.update(signals)

        if signals["is_contract"] and not contract_ok:
            continue
        if _fuzzy_company_match(company, blacklist):
            continue
        text = f"{title} {job.get('description', '')}".lower()
        if any(neg and neg in text for neg in negatives):
            continue
        if user.get("exclude_drug_testing") and signals["mentions_drug_testing"]:
            continue
        if user.get("exclude_criminal_record_filter") and signals["mentions_criminal_filter"]:
            continue

        # dedup vs. history with reopened-position allowance (spec 6.4)
        prev = history.get(key)
        job["is_reopened"] = False
        if prev:
            prev_date = date.fromisoformat(prev)
            if (date.today() - prev_date).days <= 14:
                continue
            job["is_reopened"] = True

        # location / commute filter (remote always passes)
        is_remote = (job.get("location") or "").lower() == "remote" or "remote" in text[:300]
        if not is_remote:
            passed = _location_ok(user, job, home_coords)
            if not passed:
                continue

        job["is_whitelisted"] = _fuzzy_company_match(company, whitelist)
        job["matched_benefits"] = [b for b in signals["detected_benefits"] if b in user_benefits]
        job["not_interested_penalty"] = company.lower() in avoid_companies
        job["below_minimum"] = False
        value = matching.parse_salary_number(job.get("salary"))
        if minimum and value and value < minimum:
            job["below_minimum"] = True  # hard cutoff only when salary data exists
            continue
        desk_terms = ("remote", "desk", "office", "analyst", "support", "coordinator", "clerk", "data")
        job["_accessibility_friendly"] = is_remote or any(t in text[:500] for t in desk_terms)
        out.append(job)
    return out


def _location_ok(user, job, home_coords) -> bool:
    mode = user.get("commute_mode") or "radius"
    if mode != "radius" and user.get("home_address"):
        minutes = geo.commute_minutes(user["home_address"], job.get("location") or "", mode)
        if minutes is not None:
            job["commute_minutes"] = minutes
            job["commute_mode_used"] = mode
            return minutes <= (user.get("commute_max_minutes") or 30)
        # fall through to radius (spec edge case 22)
    radius = min(user.get("search_radius_miles") or 15, 50)
    if home_coords and (job.get("latitude") is None or job.get("longitude") is None) and job.get("location"):
        coords = geo.geocode(job["location"])  # cached; fills map data too
        if coords:
            job["latitude"], job["longitude"] = coords
    if home_coords and job.get("latitude") is not None and job.get("longitude") is not None:
        dist = geo.haversine_miles(home_coords[0], home_coords[1], job["latitude"], job["longitude"])
        return dist <= radius
    # no coordinates — trust the provider's location targeting (all active
    # providers receive the user's city/state in the search request)
    city = (user.get("city") or "").lower()
    return not city or city in (job.get("location") or "").lower() or \
        job.get("source") in ("adzuna", "jsearch", "theirstack", "usajobs", "demo")


def _evaluate_candidates(user, profile_text, candidates) -> list:
    """LLM hire-likelihood for the most promising candidates (cost-capped)."""
    def pre_rank(j):
        return (j.get("is_whitelisted", False), j.get("is_urgent", False),
                bool(j.get("salary")), -j.get("posted_days_ago", 0))
    candidates.sort(key=pre_rank, reverse=True)
    evaluated = []
    for job in candidates[:MAX_LLM_EVALUATIONS_PER_USER]:
        result = matching.hire_likelihood(profile_text, job, user_id=user["id"])
        job["hire_score"] = result["score"]
        job["match_reason"] = result["reason"]
        job["skills_gap"] = result["skills_gap"]
        job["preferred_qualifications"] = result["preferred_qualifications"]
        for b in result["detected_benefits"]:
            if b not in job["detected_benefits"]:
                job["detected_benefits"].append(b)
        user_benefits = set(db.jloads(user.get("preferred_benefits"), []))
        job["matched_benefits"] = [b for b in job["detected_benefits"] if b in user_benefits]
        evaluated.append(job)
    return evaluated


def _insert_history(user, top_jobs) -> list:
    """Insert sent jobs into job_history; returns rows shaped for the email."""
    today = date.today().isoformat()
    rows = []
    for job in top_jobs:
        try:
            row_id = db.execute(
                "INSERT INTO job_history (user_id, job_title, company_name, location, latitude, "
                "longitude, salary, salary_source, applicant_count, apply_link, aggregate_link, "
                "description, match_reason, match_score, skills_gap, detected_benefits, "
                "preferred_qualifications, company_research, deadline_date, is_urgent, "
                "commute_minutes, commute_mode, date_emailed, is_multi_hire, is_reopened) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user["id"], job["title"], job["company"], job.get("location"),
                 job.get("latitude"), job.get("longitude"), job.get("salary"),
                 job.get("salary_source"), job.get("applicant_count"), job["apply_link"],
                 job.get("aggregate_link"), (job.get("description") or "")[:4000],
                 job.get("match_reason"), int(job["final_score"]),
                 db.jdumps(job.get("skills_gap") or []),
                 db.jdumps(job.get("detected_benefits") or []),
                 db.jdumps(job.get("preferred_qualifications") or []),
                 db.jdumps(job.get("company_research") or {}),
                 job.get("deadline_date"), 1 if job.get("is_urgent") else 0,
                 job.get("commute_minutes"), job.get("commute_mode_used"),
                 today, 1 if job.get("is_multi_hire") else 0,
                 1 if job.get("is_reopened") else 0),
            )
        except Exception as exc:  # unique dedup index collision
            db.log("main", user["id"], f"Dedup index rejected {job['company']} / {job['title']}: {exc}")
            continue
        rows.append({
            "id": row_id, "job_title": job["title"], "company_name": job["company"],
            "location": job.get("location"), "salary": job.get("salary"),
            "salary_source": job.get("salary_source"), "applicant_count": job.get("applicant_count"),
            "apply_link": job["apply_link"], "aggregate_link": job.get("aggregate_link"),
            "match_reason": job.get("match_reason"), "match_score": job["final_score"],
            "skills_gap": job.get("skills_gap") or [], "detected_benefits": job.get("detected_benefits") or [],
            "company_research": job.get("company_research") or {}, "deadline_date": job.get("deadline_date"),
            "is_urgent": job.get("is_urgent"), "is_multi_hire": job.get("is_multi_hire"),
            "is_reopened": job.get("is_reopened"), "commute_minutes": job.get("commute_minutes"),
            "commute_mode": job.get("commute_mode_used"),
        })
    return rows


# -------------------------------------------------- resume advice & certs

def resume_advice_paragraph(user, top_jobs, analyzed):
    """One-paragraph advice for the email, per spec 9.2/9.4 rules. None = skip."""
    first_email = not user.get("first_email_sent")
    pending = bool(user.get("resume_advice_pending"))
    always = bool(user.get("resume_advice_enabled"))
    if not (first_email or pending or always):
        return None
    strong = sum(1 for j in top_jobs if j.get("hire_score", 0) >= 7)
    performing_well = strong >= min(4, len(top_jobs)) and len(top_jobs) >= 2
    if performing_well and not always:
        return None  # resume matched well — skip advice
    gaps = {}
    for j in analyzed:
        for g in j.get("skills_gap") or []:
            gaps[g] = gaps.get(g, 0) + 1
    top_gaps = [g for g, _ in sorted(gaps.items(), key=lambda kv: -kv[1])[:3]]
    text = llm.complete(
        "Write ONE short paragraph (3-4 sentences) of practical resume advice for this job seeker, "
        "based on how their resume performed against this cycle's actual job listings. Mention the "
        f"frequently requested skills they may be missing: {', '.join(top_gaps) or 'n/a'}. "
        "Friendly, specific, no fluff, no headings.\n\n"
        f"Resume:\n{(user.get('resume_raw_text') or '')[:3000]}",
        process_type="resume_advice", user_id=user["id"], max_tokens=400)
    if text:
        return text
    if top_gaps:
        return (f"We noticed your resume didn't match strongly with many local openings. Employers in "
                f"your area frequently ask for {', '.join(top_gaps)} — consider adding these (or making "
                f"them more prominent) if you have the experience. Quantifying achievements with numbers "
                f"also improves visibility. You can update your resume anytime from your dashboard.")
    return ("We noticed your resume didn't match strongly with many local openings. Consider adding "
            "more of the specific skills and tools you've used, and quantify achievements with numbers. "
            "You can update your resume anytime from your dashboard.")


STATIC_COURSES = lambda skill: [
    {"name": f"{skill} tutorials (free)", "provider": "freeCodeCamp / YouTube", "price": "Free",
     "url": f"https://www.youtube.com/results?search_query={skill.replace(' ', '+')}+course"},
    {"name": f"{skill} courses", "provider": "Coursera", "price": "Free audit / paid certificate",
     "url": f"https://www.coursera.org/search?query={skill.replace(' ', '%20')}"},
    {"name": f"{skill} certification prep", "provider": "Udemy", "price": "Paid (often $13-20)",
     "url": f"https://www.udemy.com/courses/search/?q={skill.replace(' ', '%20')}"},
]

COURSES_SCHEMA = llm.obj_schema({
    "courses": {"type": "array", "items": llm.obj_schema(
        {"name": llm.STR, "provider": llm.STR, "price": llm.STR, "url": llm.STR})},
})


def update_cert_recommendations(user) -> list:
    """Persistent skill gaps: a skill appearing in skills_gap of 3+ jobs within
    30 days triggers a course recommendation (spec 6.11 / 11.4)."""
    uid = user["id"]
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    rows = db.query(
        "SELECT skills_gap FROM job_history WHERE user_id = ? AND date_emailed >= ? AND skills_gap IS NOT NULL",
        (uid, cutoff))
    counts = {}
    for r in rows:
        for g in db.jloads(r["skills_gap"], []):
            g = str(g).strip()
            if g:
                counts[g] = counts.get(g, 0) + 1
    persistent = {g: n for g, n in counts.items() if n >= 3}
    for skill, n in persistent.items():
        existing = db.query_one(
            "SELECT id, dismissed FROM cert_recommendations WHERE user_id = ? AND skill_name = ?",
            (uid, skill))
        if existing:
            db.execute("UPDATE cert_recommendations SET gap_count = ?, updated_at = ? WHERE id = ?",
                       (n, db.now(), existing["id"]))
            continue
        result = llm.complete_json(
            f"Suggest 3 real online courses/certifications for learning '{skill}' — one free option "
            "and two paid. Use only well-known providers (Coursera, Udemy, LinkedIn Learning, "
            "freeCodeCamp, official vendor training) and give realistic search/landing URLs.",
            process_type="job_matching", user_id=uid, schema=COURSES_SCHEMA, max_tokens=500)
        courses = (result or {}).get("courses") or STATIC_COURSES(skill)
        db.execute(
            "INSERT INTO cert_recommendations (user_id, skill_name, gap_count, courses) VALUES (?, ?, ?, ?)",
            (uid, skill, n, db.jdumps(courses)))
    return db.query(
        "SELECT * FROM cert_recommendations WHERE user_id = ? AND dismissed = 0 ORDER BY gap_count DESC",
        (uid,))


# ----------------------------------------------------------- urgency check

def run_urgency_check() -> dict:
    """Lightweight between-cycle check (spec section 8)."""
    if not db.acquire_lock("urgency_check", max_age_minutes=50):
        return {"skipped": True}
    summary = {"checked": 0, "alerted": 0}
    try:
        users = db.query(
            "SELECT * FROM users WHERE is_paused = 0 AND urgency_alerts_enabled = 1 "
            "AND email_pref_urgency_alerts = 1")
        for user in users:
            if is_due(user):  # regular email due today — let the main cycle handle it
                continue
            if user.get("last_urgency_alert_at"):
                last = datetime.strptime(user["last_urgency_alert_at"], "%Y-%m-%d %H:%M:%S")
                if datetime.now() - last < timedelta(hours=24):
                    continue  # rate limit: max 1 per 24h
            if not (user.get("resume_raw_text") or "").strip():
                continue
            summary["checked"] += 1
            try:
                if _urgency_for_user(user):
                    summary["alerted"] += 1
            except Exception as exc:
                log.exception("Urgency check failed for user %s", user["id"])
                db.log("urgency", user["id"], f"ERROR: {exc}")
    finally:
        db.release_lock("urgency_check")
    db.log("urgency", None, f"Urgency sweep finished: {summary}")
    return summary


def _urgency_for_user(user) -> bool:
    skills = db.jloads(user.get("skills"), [])
    queries = [", ".join(map(str, skills[:2])) or "specialist"]
    positions = [p.strip() for p in (user.get("preferred_positions") or "").split(",") if p.strip()]
    if positions:
        queries = positions[:1] + queries[:1]
    raw = []
    for q in queries[:2]:  # 1-2 lightweight queries only
        raw += job_search.search_jobs(q, user.get("city") or "", user.get("state") or "",
                                      remote=bool(user.get("job_type_remote")), max_days_old=2)
    candidates = _filter_candidates(user, raw, days=2)
    urgent = [j for j in candidates if j.get("urgency_hits", 0) >= 1 and
              (j.get("urgency_hits", 0) >= 2 or _deadline_within(j, 3) or j.get("is_urgent"))]
    if not urgent:
        return False
    profile_text = build_profile_text(user, db.query(
        "SELECT * FROM user_resumes WHERE user_id = ? ORDER BY is_primary DESC", (user["id"],)))
    for job in urgent[:3]:  # quick scoring, no salary research (efficiency)
        result = matching.hire_likelihood(profile_text, job, user_id=user["id"])
        if result["score"] >= 8:
            job["hire_score"] = result["score"]
            job["match_reason"] = result["reason"]
            job["skills_gap"] = result["skills_gap"]
            job["final_score"] = matching.composite_score(job, user)
            link_resolver.upgrade_links([job], user_id=user["id"], jsearch_budget=1)
            job["company_research"] = research.company_research(
                job["company"], demo_rating=job.get("_demo_rating"), user_id=user["id"])
            rows = _insert_history(user, [job])
            if not rows:
                return False
            subject, body = email_compose.compose_urgency_email(user, rows[0])
            sent = emailer.send(user, "urgency_alert", subject, body)
            db.execute("UPDATE users SET last_urgency_alert_at = ? WHERE id = ?", (db.now(), user["id"]))
            db.log("urgency", user["id"], f"Urgency alert sent: {job['title']} at {job['company']}")
            return sent
    return False


def _deadline_within(job, days) -> bool:
    if not job.get("deadline_date"):
        return False
    try:
        return (date.fromisoformat(job["deadline_date"]) - date.today()).days <= days
    except ValueError:
        return False


# ------------------------------------------------------ portfolio validator

def run_portfolio_checks() -> dict:
    """Weekly portfolio link validation (spec 13.2)."""
    import time as _time
    import httpx
    summary = {"checked": 0, "broken": 0}
    users = db.query("SELECT * FROM users WHERE is_paused = 0 AND portfolio_url IS NOT NULL AND portfolio_url != ''")
    for user in users:
        url = user["portfolio_url"]
        if not url.startswith("http"):
            url = "https://" + url
        result = check_portfolio_url(url)
        db.execute(
            "INSERT INTO portfolio_checks (user_id, url, status_code, response_time_ms, ssl_valid, "
            "is_accessible, issues) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], url, result["status_code"], result["response_time_ms"],
             1 if result["ssl_valid"] else 0, 1 if result["is_accessible"] else 0,
             db.jdumps(result["issues"])))
        summary["checked"] += 1
        if not result["is_accessible"]:
            summary["broken"] += 1
            body = emailer.wrap_html(
                "⚠️ Portfolio link check",
                f"<p>We noticed your portfolio at <a href='{url}'>{url}</a> is returning "
                f"<strong>{result['issues'][0] if result['issues'] else 'an error'}</strong>. "
                f"This could hurt your job applications — please check it.</p>")
            emailer.send(user, "system", "⚠️ Your Portfolio Link May Be Broken", body)
    db.log("portfolio", None, f"Portfolio sweep finished: {summary}")
    return summary


def check_portfolio_url(url: str) -> dict:
    import time as _time
    import httpx
    issues, ssl_valid, status_code, elapsed_ms = [], True, None, None
    try:
        start = _time.time()
        resp = httpx.get(url, timeout=12, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant portfolio check)"})
        elapsed_ms = int((_time.time() - start) * 1000)
        status_code = resp.status_code
        if status_code >= 400:
            issues.append(f"HTTP {status_code}")
        if elapsed_ms > 5000:
            issues.append(f"Slow response ({elapsed_ms} ms)")
        if len(resp.text.strip()) < 200:
            issues.append("Page appears to be empty or a parking page")
    except httpx.ConnectError as exc:
        if "SSL" in str(exc) or "certificate" in str(exc).lower():
            ssl_valid = False
            issues.append("SSL certificate invalid or expired")
        else:
            issues.append("Site unreachable (connection refused)")
    except httpx.TimeoutException:
        issues.append("Site timed out (>12s)")
    except Exception as exc:
        issues.append(f"Error: {exc}")
    accessible = status_code is not None and status_code < 400 and ssl_valid
    return {"status_code": status_code, "response_time_ms": elapsed_ms,
            "ssl_valid": ssl_valid, "is_accessible": accessible, "issues": issues}


# ------------------------------------------------- monthly LinkedIn re-run

def run_linkedin_monthly() -> dict:
    from . import advice
    summary = {"analyzed": 0}
    users = db.query(
        "SELECT * FROM users WHERE is_paused = 0 AND linkedin_url IS NOT NULL AND linkedin_url != ''")
    for user in users:
        try:
            advice.linkedin_analyze(user, send_email=True)
            summary["analyzed"] += 1
        except Exception:
            log.exception("Monthly LinkedIn analysis failed for user %s", user["id"])
    db.log("linkedin", None, f"Monthly LinkedIn sweep: {summary}")
    return summary


# ------------------------------------------------------- token budget alert

def run_token_budget_check() -> dict:
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()
    daily = db.query_one(
        "SELECT COALESCE(SUM(tokens_used),0) AS t, COALESCE(SUM(estimated_cost),0) AS c "
        "FROM token_usage WHERE created_at >= ?", (today,)) or {"t": 0, "c": 0}
    monthly = db.query_one(
        "SELECT COALESCE(SUM(tokens_used),0) AS t, COALESCE(SUM(estimated_cost),0) AS c "
        "FROM token_usage WHERE created_at >= ?", (month_start,)) or {"t": 0, "c": 0}
    daily_budget = int(db.get_setting("token_daily_budget", "2000000"))
    monthly_budget = int(db.get_setting("token_monthly_budget", "40000000"))
    alerts = []
    if daily_budget and daily["t"] >= daily_budget * 0.8:
        alerts.append(f"Daily token usage is at {daily['t']:,} ({100 * daily['t'] / daily_budget:.0f}% "
                      f"of the {daily_budget:,} budget). Estimated cost today: ${daily['c']:.2f}.")
    if monthly_budget and monthly["t"] >= monthly_budget * 0.8:
        alerts.append(f"Monthly token usage is at {monthly['t']:,} ({100 * monthly['t'] / monthly_budget:.0f}% "
                      f"of the {monthly_budget:,} budget). Estimated cost this month: ${monthly['c']:.2f}.")
    # runaway user detection: 3x average per-user usage today
    rows = db.query(
        "SELECT user_id, SUM(tokens_used) AS t FROM token_usage WHERE created_at >= ? "
        "AND user_id IS NOT NULL GROUP BY user_id", (today,))
    if len(rows) >= 2:
        avg = sum(r["t"] for r in rows) / len(rows)
        for r in rows:
            if r["t"] > 3 * avg and r["t"] > 50000:
                u = db.query_one("SELECT email FROM users WHERE id = ?", (r["user_id"],))
                alerts.append(f"User {(u or {}).get('email', r['user_id'])} consumed {r['t']:,} tokens today "
                              f"(more than 3x the {avg:,.0f} average) — possible runaway process.")
    if alerts:
        admin = {"id": None, "email": config.ALERT_EMAIL, "email_pref_system_notifications": 1}
        body = emailer.wrap_html(
            "Token budget alert",
            "<p>" + "</p><p>".join(alerts) + "</p>"
            f"<p>Review the <a href='{config.APP_BASE_URL}/admin'>admin dashboard</a> for details.</p>")
        emailer.send(admin, "system", "⚠️ Token budget alert", body, enforce_pref=False)
        db.log("budget", None, f"Token alerts sent: {len(alerts)}")
    return {"alerts": len(alerts), "daily_tokens": daily["t"], "monthly_tokens": monthly["t"]}
