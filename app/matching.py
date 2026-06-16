"""Job matching and ranking (spec section 6.7).

Factor 1: hire likelihood via LLM (~50%), Factor 2: salary (~30%),
Factor 3: competition (~20%), plus multi-hire / whitelist / urgency /
benefits-match bonus modifiers. Composite is converted to a 0-100 match score.
"""
import logging
import re

from . import config, db, llm

log = logging.getLogger("matching")

HIRE_SCHEMA = llm.obj_schema({
    "score": llm.INT,
    "reason": llm.STR,
    "skills_gap": llm.STR_ARR,
    "detected_benefits": llm.STR_ARR,
    "preferred_qualifications": llm.STR_ARR,
})


def hire_likelihood(profile_text: str, job: dict, user_id=None) -> dict:
    """Returns {score 1-10, reason, skills_gap, detected_benefits,
    preferred_qualifications}."""
    result = llm.complete_json(
        "You are a career counselor and hiring expert. Given the following candidate profile and "
        "job listing, rate the likelihood (1-10) that this candidate would be hired for this "
        "position. Consider:\n"
        "- Skills match (do the candidate's skills align with the job requirements?)\n"
        "- Experience level match (is the candidate over/under qualified?)\n"
        "- Transferable skills (could their background translate to this role?)\n"
        "- The candidate's stated preferences and bio\n"
        "- Industry alignment (does this match their selected industries?)\n\n"
        "Also identify any skills the job listing requires that the candidate's resume does NOT "
        "mention. List up to 3 missing skills.\n\n"
        "Also extract any benefits mentioned in the job listing from this list: health_insurance, "
        "401k, pto, remote_flexibility, tuition_reimbursement, dental_vision, parental_leave, "
        "professional_development, stock_options.\n\n"
        "Also extract the most commonly requested 'preferred qualifications' or 'nice-to-have' "
        "skills from the listing.\n\n"
        f"Candidate Profile:\n{profile_text[:config.RESUME_TEXT_LIMIT]}\n\n"
        f"Job Listing:\nTitle: {job['title']}\nCompany: {job['company']}\n"
        f"Location: {job.get('location')}\nDescription: {(job.get('description') or '')[:4000]}",
        process_type="job_matching",
        user_id=user_id,
        schema=HIRE_SCHEMA,
        max_tokens=800,
    )
    if result is None:
        result = _heuristic_likelihood(profile_text, job)
    result["score"] = max(1, min(10, int(result.get("score") or 1)))
    result.setdefault("skills_gap", [])
    result.setdefault("detected_benefits", [])
    result.setdefault("preferred_qualifications", [])
    return result


BATCH_HIRE_SCHEMA = llm.obj_schema({
    "evaluations": {"type": "array", "items": llm.obj_schema({
        "index": llm.INT,
        "score": llm.INT,
        "reason": llm.STR,
        "skills_gap": llm.STR_ARR,
        "detected_benefits": llm.STR_ARR,
        "preferred_qualifications": llm.STR_ARR,
    })},
})

HIRE_BATCH_SIZE = 6  # jobs per LLM call — amortizes the resume across the batch


def hire_likelihood_batch(profile_text: str, jobs: list, user_id=None) -> list:
    """Score many jobs against one profile, batching to share the resume tokens
    across each call (far cheaper than one call per job). Returns a list of
    result dicts aligned 1:1 with `jobs`. Falls back to the heuristic per job
    when the AI is unavailable or omits an entry."""
    results = [None] * len(jobs)
    for start in range(0, len(jobs), HIRE_BATCH_SIZE):
        chunk = jobs[start:start + HIRE_BATCH_SIZE]
        parsed = _score_chunk(profile_text, chunk, user_id)
        for i, job in enumerate(chunk):
            r = parsed.get(i) or _heuristic_likelihood(profile_text, job)
            r["score"] = max(1, min(10, int(r.get("score") or 1)))
            r.setdefault("reason", "")
            r.setdefault("skills_gap", [])
            r.setdefault("detected_benefits", [])
            r.setdefault("preferred_qualifications", [])
            results[start + i] = r
    return results


def _score_chunk(profile_text: str, chunk: list, user_id) -> dict:
    """One LLM call scoring up to HIRE_BATCH_SIZE jobs. Returns {index: result}."""
    listings = []
    for i, job in enumerate(chunk):
        listings.append(
            f"[Job {i}]\nTitle: {job['title']}\nCompany: {job['company']}\n"
            f"Location: {job.get('location')}\n"
            f"Description: {(job.get('description') or '')[:1500]}")
    result = llm.complete_json(
        "You are a career counselor and hiring expert. For EACH job listing below, rate the "
        "likelihood (1-10) that this candidate would be hired for that position. Consider:\n"
        "- Skills match (do the candidate's skills align with the job requirements?)\n"
        "- Experience level match (is the candidate over/under qualified?)\n"
        "- Transferable skills (could their background translate to this role?)\n"
        "- The candidate's stated preferences and bio\n"
        "- Industry alignment\n\n"
        "For each job also list up to 3 skills the listing requires that the candidate's resume "
        "does NOT mention, any benefits it mentions (from: health_insurance, 401k, pto, "
        "remote_flexibility, tuition_reimbursement, dental_vision, parental_leave, "
        "professional_development, stock_options), and its 'preferred/nice-to-have' qualifications.\n\n"
        "Return ONE evaluation object per job in the `evaluations` array, each carrying the matching "
        "`index` from its [Job N] header.\n\n"
        f"Candidate Profile:\n{profile_text[:config.RESUME_TEXT_LIMIT]}\n\n"
        "Job Listings:\n" + "\n\n".join(listings),
        process_type="job_matching", user_id=user_id,
        schema=BATCH_HIRE_SCHEMA, max_tokens=300 * len(chunk) + 200)
    out = {}
    if result and isinstance(result.get("evaluations"), list):
        for ev in result["evaluations"]:
            try:
                idx = int(ev.get("index"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(chunk) and idx not in out:
                out[idx] = ev
    return out


def _heuristic_likelihood(profile_text: str, job: dict) -> dict:
    """Keyword/skills-overlap fallback when no AI key is configured.
    Weighted toward the candidate's parsed skills and target positions so
    genuinely relevant listings clear the >=5 qualification bar."""
    lower_profile = profile_text.lower()
    job_text = f"{job['title']} {job.get('description', '')}".lower()

    skills = []
    m = re.search(r"^skills:\s*(.+)$", profile_text, flags=re.M | re.I)
    if m:
        skills = [s.strip().lower() for s in m.group(1).split(",") if len(s.strip()) >= 2]
    skill_hits = [s for s in skills if s in job_text]

    position_hit = False
    m = re.search(r"^preferred positions:\s*(.+)$", profile_text, flags=re.M | re.I)
    if m:
        for pos in m.group(1).split(","):
            words = [w for w in re.findall(r"[a-z]{4,}", pos.lower()) if w != "manager"]
            if pos.strip() and (pos.strip().lower() in job_text or
                                any(w in job["title"].lower() for w in words)):
                position_hit = True
                break

    profile_words = set(re.findall(r"[a-z]{4,}", lower_profile))
    job_words = set(re.findall(r"[a-z]{4,}", job_text))
    stop = {"with", "your", "from", "more", "their", "have", "will", "that", "this",
            "work", "team", "include", "looking", "join", "years", "experience"}
    overlap = (profile_words & job_words) - stop

    score = 3.0 + min(4.5, len(skill_hits) * 1.5) + (1.5 if position_hit else 0) \
        + min(1.5, len(overlap) / 20.0)
    score = int(max(1, min(10, round(score))))

    quals = re.search(r"preferred qualifications?:?\s*([^.]+)", job_text)
    preferred = [q.strip().title() for q in quals.group(1).split(",")][:5] if quals else []
    gaps = [q for q in preferred if q.lower() not in lower_profile][:3]
    reason_bits = []
    if skill_hits:
        reason_bits.append(f"matches your skills in {', '.join(s.title() for s in skill_hits[:3])}")
    if position_hit:
        reason_bits.append("aligns with your preferred positions")
    reason = ("This role " + " and ".join(reason_bits) + " (keyword-based estimate)."
              if reason_bits else
              f"Your background overlaps with this role on {len(overlap)} relevant terms "
              "(keyword-based estimate).")
    return {
        "score": score,
        "reason": reason,
        "skills_gap": gaps,
        "detected_benefits": [],
        "preferred_qualifications": preferred,
    }


def parse_salary_number(salary: str):
    """Extract a representative annual number from a salary string.
    Hourly rates are converted assuming 2080 hours/year."""
    if not salary:
        return None
    nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]+(?:\.\d+)?", salary)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return None
    value = sum(nums[:2]) / min(len(nums), 2)
    if "hour" in salary.lower() or "/hr" in salary.lower() or value < 200:
        value *= 2080
    elif value < 2000:  # weekly-ish numbers; ignore
        return None
    return value


def parse_applicants(applicant_count: str):
    if not applicant_count:
        return None
    m = re.search(r"\d+", applicant_count)
    return int(m.group(0)) if m else None


def composite_score(job: dict, user: dict) -> float:
    """Combine factors into 0-100 (job dict must carry hire_score/salary/etc.)."""
    hire = job["hire_score"] / 10.0  # 0-1

    preferred = parse_salary_number(user.get("preferred_salary") or "")
    value = parse_salary_number(job.get("salary") or "")
    if value is None or preferred is None:
        salary_factor = 0.5  # neutral
    else:
        ratio = value / preferred
        salary_factor = max(0.0, min(1.0, (ratio - 0.6) / 0.6))  # 60% of pref -> 0, 120% -> 1
        if ratio < 0.8:  # spec: don't rank jobs >20% below preferred highly
            salary_factor = min(salary_factor, 0.25)

    applicants = parse_applicants(job.get("applicant_count"))
    competition_factor = 0.5 if applicants is None else max(0.0, min(1.0, 1.0 - applicants / 200.0))

    score = hire * 0.5 + salary_factor * 0.3 + competition_factor * 0.2

    bonus = 1.0
    if job.get("is_multi_hire"):
        bonus += 0.12
    if job.get("is_whitelisted"):
        bonus += 0.25
    if job.get("is_urgent"):
        bonus += 0.10
    matched_benefits = job.get("matched_benefits") or []
    bonus += min(0.15, 0.05 * len(matched_benefits))
    if job.get("not_interested_penalty"):
        bonus -= 0.25  # feedback-loop deprioritisation (not a hard exclude)

    return round(max(1, min(100, score * 100 * bonus)), 1)
