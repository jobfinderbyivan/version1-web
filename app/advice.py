"""On-demand AI features: comprehensive resume advice (9.3), interview prep
(11.2), LinkedIn analysis (11.3), portfolio analysis (13.3), mock interviews
(14)."""
import logging

import httpx

from . import config, db, emailer, llm

log = logging.getLogger("advice")


# ------------------------------------------------ "Give Me Advice Now" ----

def send_full_resume_advice(user) -> dict:
    """Comprehensive resume review email. Returns {sent, reason}."""
    if not user.get("email_pref_resume_advice", 1):
        return {"sent": False,
                "reason": "You've disabled resume advice emails. Enable it in your email preferences to use this feature."}
    resume_text = (user.get("resume_raw_text") or "")[:config.RESUME_TEXT_LIMIT]
    if not resume_text.strip():
        return {"sent": False, "reason": "Upload a resume first so we have something to review."}
    recent = db.query(
        "SELECT job_title, company_name FROM job_history WHERE user_id = ? ORDER BY id DESC LIMIT 10",
        (user["id"],))
    market_summary = "; ".join(f"{r['job_title']} at {r['company_name']}" for r in recent) or \
        "no search cycles have run yet"
    industries = ", ".join(db.jloads(user.get("industry_preferences"), [])) or "not specified"
    text = llm.complete(
        "You are a top-tier professional career coach reviewing a client's resume. This client is "
        "a high-performer who takes their career seriously. Provide a comprehensive, in-depth "
        "resume review covering ALL of the following, formatted as clean HTML (use <h3> headings, "
        "<p>, <ul>/<li>; no <html>/<body> wrapper, no markdown):\n"
        "1. Overall Assessment — first impressions, strengths, biggest opportunity\n"
        f"2. Formatting & Layout\n"
        f"3. Keyword Optimization — based on the job market in {user.get('city') or 'their area'}, "
        f"{user.get('state') or ''}\n"
        "4. Achievement Quantification — identify vague descriptions and rewrite them quantified\n"
        "5. Skills Presentation\n"
        f"6. Industry Alignment — target industries: {industries}\n"
        "7. Work History Narrative\n"
        "8. Action Items — prioritized top 5 changes to make immediately\n\n"
        f"Client's target roles: {user.get('preferred_positions') or 'not specified'}\n"
        f"Recent job market in their area: {market_summary}\n\n"
        f"Client's Resume:\n{resume_text}",
        process_type="resume_advice", user_id=user["id"], max_tokens=8000)
    if text is None:
        text = ("<p>AI review unavailable (no Anthropic API key configured). General guidance: "
                "quantify achievements with numbers, mirror keywords from postings you want, keep "
                "formatting single-column and simple, lead each role with impact, and tailor the "
                "top third of the resume to your target role.</p>")
    name = user.get("full_name") or "Your"
    body = emailer.wrap_html("Your Professional Resume Review", text)
    sent = emailer.send(user, "resume_advice", f"Your Professional Resume Review — {name}", body)
    return {"sent": sent, "reason": None if sent else "Email could not be sent."}


# --------------------------------------------------------- interview prep --

def generate_interview_prep(user, job) -> str:
    """Generate (or return cached) HTML prep materials for a job row."""
    if job.get("interview_prep_data"):
        return job["interview_prep_data"]
    text = llm.complete(
        "You are an expert interview coach. A candidate has been offered an interview for the "
        "following position. Generate comprehensive interview preparation materials as clean HTML "
        "(<h3> headings, <p>, <ul>/<ol>; no <html> wrapper, no markdown):\n"
        "1. Company Deep Dive — key facts, culture, recent news, what they value\n"
        "2. Common Interview Questions for This Role — 10 likely questions with suggested answer frameworks\n"
        "3. Technical/Skills Assessment Prep — practice questions or scenarios if relevant\n"
        "4. Behavioral Questions — 5 STAR-method questions tailored to this role\n"
        "5. Questions to Ask the Interviewer — 5 thoughtful questions\n"
        "6. Tips & Advice — role-specific preparation, presentation, what to wear\n"
        "7. Red Flags to Watch For — cautions based on company research\n\n"
        f"Job Details:\n- Title: {job['job_title']}\n- Company: {job['company_name']}\n"
        f"- Description: {(job.get('description') or '')[:3000]}\n"
        f"- Company Info: {job.get('company_research') or '{}'}\n\n"
        f"Candidate Profile:\n- Resume: {(user.get('resume_raw_text') or '')[:3500]}\n"
        f"- Skills: {user.get('skills') or ''}\n- Experience Level: {user.get('experience_level') or ''}",
        process_type="interview_prep", user_id=user["id"], max_tokens=8000)
    if text is None:
        text = (f"<h3>Interview prep for {job['job_title']} at {job['company_name']}</h3>"
                "<p>AI generation unavailable (no API key). Core checklist: research the company's "
                "products and recent news; prepare 5 STAR stories covering conflict, failure, "
                "leadership, deadline pressure and a big win; prepare 5 questions for the "
                "interviewer; re-read the job description and map each requirement to your "
                "experience; plan logistics the day before.</p>")
    db.execute(
        "UPDATE job_history SET interview_prep_requested = 1, interview_prep_data = ? WHERE id = ?",
        (text, job["id"]))
    if user.get("email_pref_interview_prep", 1):
        body = emailer.wrap_html(f"Interview Prep — {job['job_title']}", text)
        emailer.send(user, "interview_prep",
                     f"Interview Prep: {job['job_title']} at {job['company_name']}", body)
    return text


# ------------------------------------------------------- LinkedIn analysis -

LINKEDIN_SCHEMA = llm.obj_schema({
    "score": llm.INT,
    "suggestions": {"type": "array", "items": llm.obj_schema({
        "category": llm.STR, "suggestion": llm.STR,
        "priority": {"type": "string", "enum": ["high", "medium", "low"]}})},
})


def _try_scrape(url: str) -> str:
    """LinkedIn blocks anonymous scraping almost always; degrade gracefully."""
    try:
        resp = httpx.get(url, timeout=12, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200 and "authwall" not in str(resp.url):
            import re
            text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", resp.text, flags=re.S)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            if len(text) > 600:
                return text[:5000]
    except Exception:
        pass
    return ""


def linkedin_analyze(user, send_email: bool = False) -> dict:
    url = (user.get("linkedin_url") or "").strip()
    if not url:
        return {"error": "Add your LinkedIn URL to get optimization tips that help recruiters find you."}
    scraped = _try_scrape(url if url.startswith("http") else "https://" + url)
    profile_section = (f"LinkedIn Profile Data (scraped):\n{scraped}" if scraped else
                       "LinkedIn profile could not be scraped (private profile or access wall) — base the "
                       "analysis on the resume, bio and the live job market data, and frame suggestions as "
                       "what their LinkedIn SHOULD contain.")
    demand = db.query(
        "SELECT skill_name, percentage FROM skills_demand WHERE user_id = ? ORDER BY percentage DESC LIMIT 8",
        (user["id"],))
    market_terms = ", ".join(f"{d['skill_name']} ({d['percentage']}%)" for d in demand) or "no data yet"
    result = llm.complete_json(
        "You are a LinkedIn optimization expert and recruiter. Analyze this candidate's LinkedIn "
        "presence compared to their resume and the current job market in their area. Provide "
        "specific, actionable suggestions to improve recruiter visibility in these categories: "
        "Headline Optimization, Summary/About Section, Skills Section, Experience Descriptions. "
        "Good suggestions look like: \"Consider adding 'Python' to your headline — 65% of matching "
        "jobs mention it\". Rate the overall profile 0-100.\n\n"
        f"{profile_section}\n\nResume:\n{(user.get('resume_raw_text') or '')[:3500]}\n\n"
        f"Bio/Preferences: {(user.get('bio') or '')[:600]} | Target roles: {user.get('preferred_positions') or '-'}\n"
        f"In-demand skills in their matched jobs: {market_terms}",
        process_type="linkedin_analysis", user_id=user["id"],
        schema=LINKEDIN_SCHEMA, max_tokens=2500)
    if result is None:
        return {"error": "We couldn't analyze your LinkedIn profile right now. Please make sure "
                         "your profile is public and try again (an Anthropic API key is required "
                         "for this feature)."}
    result["scraped"] = bool(scraped)
    db.execute("INSERT INTO linkedin_analyses (user_id, analysis_data, score) VALUES (?, ?, ?)",
               (user["id"], db.jdumps(result), result.get("score")))
    if send_email and user.get("email_pref_linkedin_tips", 1):
        items = "".join(f"<li><strong>[{s.get('priority', 'medium').upper()}] {s.get('category')}:</strong> "
                        f"{s.get('suggestion')}</li>" for s in result.get("suggestions", []))
        body = emailer.wrap_html(
            "LinkedIn Optimization Tips",
            f"<p>Your LinkedIn profile scored <strong>{result.get('score')}/100</strong>.</p>"
            f"<ul>{items}</ul><p><a href='{config.APP_BASE_URL}/profile'>Open your profile</a> "
            "to update your LinkedIn URL or re-run the analysis.</p>")
        emailer.send(user, "linkedin_tips", "Your LinkedIn optimization tips", body)
    return result


# ------------------------------------------------------ portfolio analysis -

PORTFOLIO_SCHEMA = llm.obj_schema({
    "score": llm.INT,
    "breakdown": llm.obj_schema({
        "design": llm.INT, "content": llm.INT, "navigation": llm.INT,
        "mobile": llm.INT, "professionalism": llm.INT}),
    "strengths": llm.STR_ARR,
    "improvements": llm.STR_ARR,
    "summary": llm.STR,
})


def portfolio_analyze(user) -> dict:
    from .automation import check_portfolio_url
    url = (user.get("portfolio_url") or "").strip()
    if not url:
        return {"error": "Add your portfolio URL first."}
    if not url.startswith("http"):
        url = "https://" + url
    basic = check_portfolio_url(url)
    db.execute(
        "INSERT INTO portfolio_checks (user_id, url, status_code, response_time_ms, ssl_valid, "
        "is_accessible, issues) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], url, basic["status_code"], basic["response_time_ms"],
         1 if basic["ssl_valid"] else 0, 1 if basic["is_accessible"] else 0, db.jdumps(basic["issues"])))
    if not basic["is_accessible"]:
        return {"error": f"Your portfolio could not be reached ({'; '.join(basic['issues'])}). "
                         "Fix accessibility first, then re-run the analysis.", "basic": basic}
    content = ""
    try:
        import re
        resp = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", resp.text, flags=re.S)
        content = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:6000]
    except Exception:
        pass
    result = llm.complete_json(
        "You are a professional portfolio reviewer and UX expert. Analyze this portfolio website. "
        "Rate each criterion 0-20 (design & visual appeal, content quality, navigation & "
        "usability, mobile responsiveness, professionalism), give an overall 0-100 score, "
        "strengths, and specific actionable improvements.\n\n"
        f"Portfolio URL: {url}\nPortfolio Content (text extract):\n{content or '(could not extract page text)'}\n"
        f"User's Target Roles: {user.get('preferred_positions') or '-'}\n"
        f"User's Skills: {user.get('skills') or '-'}",
        process_type="portfolio_analysis", user_id=user["id"],
        schema=PORTFOLIO_SCHEMA, max_tokens=2000)
    if result is None:
        return {"error": "Portfolio is live, but the AI analysis is unavailable (no API key). "
                         "Basic accessibility check passed.", "basic": basic}
    db.execute("INSERT INTO portfolio_analyses (user_id, analysis_data, score) VALUES (?, ?, ?)",
               (user["id"], db.jdumps(result), result.get("score")))
    result["basic"] = basic
    return result


# -------------------------------------------------------- mock interviews --

QUESTIONS_SCHEMA = llm.obj_schema({
    "questions": {"type": "array", "items": llm.obj_schema({
        "question": llm.STR,
        "type": {"type": "string", "enum": ["behavioral", "technical", "situational"]},
        "what_to_assess": llm.STR})},
})


def mock_interview_questions(user, job) -> list:
    result = llm.complete_json(
        "Generate 7 realistic interview questions for this position. Mix behavioral, situational, "
        "and technical questions as appropriate for the role.\n\n"
        f"Job: {job['job_title']} at {job['company_name']}\n"
        f"Description: {(job.get('description') or '')[:2500]}\n"
        f"Candidate Background: {(user.get('resume_raw_text') or '')[:2500]}",
        process_type="mock_interview", user_id=user["id"],
        schema=QUESTIONS_SCHEMA, max_tokens=2000)
    if result and result.get("questions"):
        return result["questions"][:10]
    role = job["job_title"]
    return [
        {"question": f"Tell me about yourself and why you're interested in this {role} position.",
         "type": "behavioral", "what_to_assess": "communication, motivation"},
        {"question": "Describe a time you had to handle a difficult situation at work. What did you do?",
         "type": "behavioral", "what_to_assess": "conflict resolution"},
        {"question": f"What skills make you a strong fit for a {role} role?",
         "type": "technical", "what_to_assess": "self-awareness, skills match"},
        {"question": "Tell me about a time you had to learn something new quickly.",
         "type": "behavioral", "what_to_assess": "adaptability"},
        {"question": "How do you prioritize when everything feels urgent?",
         "type": "situational", "what_to_assess": "organization"},
        {"question": f"Where do you see yourself in three years if you join {job['company_name']}?",
         "type": "behavioral", "what_to_assess": "ambition, retention"},
        {"question": "Do you have any questions for us?",
         "type": "situational", "what_to_assess": "preparation, curiosity"},
    ]


EVAL_SCHEMA = llm.obj_schema({
    "per_question": {"type": "array", "items": llm.obj_schema({
        "question": llm.STR, "content_quality": llm.INT, "relevance": llm.INT,
        "specificity": llm.INT, "suggestions": llm.STR})},
    "filler_word_count": llm.INT,
    "pacing_assessment": llm.STR,
    "confidence_assessment": llm.STR,
    "overall_score": llm.INT,
    "top_tips": llm.STR_ARR,
})


def evaluate_mock_interview(user, job, questions, responses) -> dict:
    qa = "\n\n".join(f"Q{i + 1}: {q.get('question') if isinstance(q, dict) else q}\n"
                     f"Answer: {(responses[i] if i < len(responses) else '') or '(no answer)'}"
                     for i, q in enumerate(questions))
    result = llm.complete_json(
        "You are an expert interview coach. Evaluate these interview responses.\n"
        "For each response, score content quality (1-10), relevance (1-10), specificity / STAR "
        "method use (1-10), and give an improvement suggestion. Also count filler words (um, uh, "
        "like, you know) across the transcripts, assess pacing (too short / rambling / good) and "
        "confidence signals, give an overall 0-100 score and the top 3 actionable tips.\n\n"
        f"Job: {job['job_title']} at {job['company_name']}\n\n{qa[:7000]}",
        process_type="mock_interview", user_id=user["id"],
        schema=EVAL_SCHEMA, max_tokens=4000)
    if result is None:
        import re
        filler = sum(len(re.findall(r"\b(?:um|uh|like|you know)\b", (r or "").lower())) for r in responses)
        answered = [r for r in responses if (r or "").strip()]
        avg_words = (sum(len(r.split()) for r in answered) / len(answered)) if answered else 0
        score = max(20, min(85, int(40 + avg_words / 3 - filler * 2)))
        result = {
            "per_question": [{"question": (q.get("question") if isinstance(q, dict) else str(q)),
                              "content_quality": 5, "relevance": 5, "specificity": 5,
                              "suggestions": "Use the STAR method: Situation, Task, Action, Result."}
                             for q in questions],
            "filler_word_count": filler,
            "pacing_assessment": ("Answers were quite short — aim for 60-120 seconds each."
                                  if avg_words < 40 else "Reasonable answer length."),
            "confidence_assessment": "Heuristic review only (no AI key configured).",
            "overall_score": score,
            "top_tips": ["Use specific examples with measurable results",
                         "Reduce filler words by pausing instead",
                         "Prepare 5 reusable STAR stories before the real interview"],
        }
    grade = ("A" if result["overall_score"] >= 90 else "B" if result["overall_score"] >= 80 else
             "C" if result["overall_score"] >= 65 else "D" if result["overall_score"] >= 50 else "F")
    result["grade"] = grade
    session_id = db.execute(
        "INSERT INTO mock_interview_sessions (user_id, job_history_id, questions_asked, "
        "user_responses, feedback, overall_score, summary_report) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], job["id"], db.jdumps(questions), db.jdumps(responses),
         db.jdumps(result), result["overall_score"], ""))
    result["session_id"] = session_id
    if user.get("email_pref_interview_prep", 1):
        tips = "".join(f"<li>{t}</li>" for t in result.get("top_tips", []))
        body = emailer.wrap_html(
            "Mock Interview Report",
            f"<p>Your mock interview for <strong>{job['job_title']}</strong> at "
            f"<strong>{job['company_name']}</strong> scored "
            f"<strong>{result['overall_score']}/100 (grade {grade})</strong>.</p>"
            f"<p>Filler words: {result.get('filler_word_count', 0)} · "
            f"{result.get('pacing_assessment', '')}</p><strong>Top tips:</strong><ul>{tips}</ul>")
        emailer.send(user, "interview_prep",
                     f"Mock interview report — {job['job_title']}", body)
    return result
