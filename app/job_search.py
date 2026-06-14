"""Job listing providers.

The Abacus spec performs `web_search` scraping; locally we use real job APIs
when keys are configured (Adzuna, JSearch/RapidAPI) and fall back to a demo
provider that fabricates realistic listings so the entire pipeline (filtering,
matching, ranking, emails, dedup) can be exercised without any keys.

Every provider returns normalised dicts:
  title, company, location, latitude, longitude, salary (str|None),
  salary_source, description, apply_link, aggregate_link, posted_days_ago,
  applicant_count, source
Signal detection (urgency, deadline, benefits, multi-hire, contract,
drug-testing, criminal-record) is shared and text based.
"""
import logging
import random
import re
import time
from datetime import date, datetime, timedelta

import httpx

from . import config

log = logging.getLogger("jobsearch")

SEARCH_DELAY_SECONDS = 2  # spec edge case 6: rate limit between search calls

BENEFIT_PATTERNS = {
    "health_insurance": r"health (?:insurance|benefits)|medical insurance|medical coverage",
    "401k": r"401\s*\(?k\)?|retirement (?:plan|match)",
    "pto": r"\bpto\b|paid time off|vacation days|paid vacation",
    "remote_flexibility": r"remote flexib|work from home|wfh|hybrid schedule",
    "tuition_reimbursement": r"tuition (?:reimbursement|assistance)",
    "dental_vision": r"dental|vision insurance|vision coverage",
    "parental_leave": r"parental leave|maternity|paternity",
    "professional_development": r"professional development|training budget|learning stipend",
    "stock_options": r"stock options|equity|rsus?\b|employee stock",
}

URGENCY_PATTERN = re.compile(
    r"urgent|immediate start|must fill|asap|hiring immediately|start (?:date)?:? ?asap|"
    r"need someone (?:now|yesterday)|temporary opening|backfill|quick(?:ly)? fill", re.I)

MULTI_HIRE_PATTERN = re.compile(
    r"hiring (?:\d+|multiple|several)|multiple (?:openings|positions)|several positions|"
    r"\d+\s*(?:openings|positions available)", re.I)

CONTRACT_PATTERN = re.compile(
    r"\b(?:contract(?:or|-to-hire)?|temp(?:orary)?|1099|freelance|contingent)\b", re.I)
CONTRACT_FALSE_POSITIVE = re.compile(
    r"government contract|defense contract|contract management|contracts? with clients", re.I)

DRUG_TEST_PATTERN = re.compile(
    r"drug (?:test|screen)|substance (?:test|screening)|pre-?employment screening", re.I)
CRIMINAL_PATTERN = re.compile(
    r"clean (?:criminal )?record required|no felon|criminal background check required|"
    r"must pass (?:a )?background check", re.I)

DEADLINE_PATTERN = re.compile(
    r"(?:applications? close|deadline|apply by|closing date)[:\s]*"
    r"((?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}(?:,?\s*\d{4})?|\d{1,2}/\d{1,2}/\d{2,4})", re.I)


def detect_signals(title: str, description: str) -> dict:
    text = f"{title}\n{description or ''}"
    benefits = [name for name, pat in BENEFIT_PATTERNS.items() if re.search(pat, text, re.I)]
    is_contract = bool(CONTRACT_PATTERN.search(text)) and not CONTRACT_FALSE_POSITIVE.search(text)
    deadline = None
    m = DEADLINE_PATTERN.search(text)
    if m:
        deadline = _parse_deadline(m.group(1))
    urgency_hits = len(URGENCY_PATTERN.findall(text))
    return {
        "detected_benefits": benefits,
        "is_urgent": urgency_hits > 0,
        "urgency_hits": urgency_hits,
        "is_multi_hire": bool(MULTI_HIRE_PATTERN.search(text)),
        "is_contract": is_contract,
        "mentions_drug_testing": bool(DRUG_TEST_PATTERN.search(text)),
        "mentions_criminal_filter": bool(CRIMINAL_PATTERN.search(text)),
        "deadline_date": deadline,
    }


def _parse_deadline(raw: str):
    raw = raw.strip().rstrip(".,")
    for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    try:  # month + day without year -> assume this year (or next if passed)
        parsed = datetime.strptime(raw + f", {date.today().year}", "%B %d, %Y").date()
        if parsed < date.today():
            parsed = parsed.replace(year=parsed.year + 1)
        return parsed.isoformat()
    except ValueError:
        return None


# ------------------------------------------------------------- providers ---

def search_jobs(query: str, city: str, state: str, *, remote: bool = False,
                radius_miles: int = 15, max_days_old: int = 3) -> list:
    """Run one query against all configured providers (rate limited)."""
    results = []
    for name in config.search_provider_names():
        try:
            if name == "adzuna":
                results += _adzuna(query, city, state, remote, radius_miles, max_days_old)
            elif name == "jsearch":
                results += _jsearch(query, city, state, remote, max_days_old)
            elif name == "theirstack":
                results += _theirstack(query, city, state, remote, max_days_old)
            elif name == "usajobs":
                results += _usajobs(query, city, state, remote, radius_miles, max_days_old)
            elif name == "demo":
                results += _demo(query, city, state, remote)
        except Exception as exc:
            log.warning("Provider %s failed for %r: %s", name, query, exc)
        time.sleep(SEARCH_DELAY_SECONDS if name != "demo" else 0)
    return results


def _adzuna(query, city, state, remote, radius_miles, max_days_old) -> list:
    where = "remote" if remote else ", ".join(p for p in (city, state) if p)
    params = {
        "app_id": config.ADZUNA_APP_ID,
        "app_key": config.ADZUNA_APP_KEY,
        "what": query,
        "results_per_page": 20,
        "max_days_old": max_days_old,
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
    if not remote:
        params["distance"] = int(radius_miles * 1.609)  # km
    resp = httpx.get(
        f"https://api.adzuna.com/v1/api/jobs/{config.ADZUNA_COUNTRY}/search/1",
        params=params, timeout=30)
    resp.raise_for_status()
    out = []
    for item in resp.json().get("results", []):
        salary = None
        if item.get("salary_min") or item.get("salary_max"):
            lo, hi = item.get("salary_min"), item.get("salary_max")
            if lo and hi and abs(hi - lo) > 1:
                salary = f"${lo:,.0f} - ${hi:,.0f}/year"
            else:
                salary = f"${(hi or lo):,.0f}/year"
        created = item.get("created", "")
        days = 0
        try:
            days = (datetime.now() - datetime.strptime(created[:10], "%Y-%m-%d")).days
        except ValueError:
            pass
        out.append({
            "title": item.get("title", "").replace("<strong>", "").replace("</strong>", ""),
            "company": (item.get("company") or {}).get("display_name", "Unknown"),
            "location": (item.get("location") or {}).get("display_name", where),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "salary": salary,
            "salary_source": "Job listing" if salary else None,
            "description": item.get("description", ""),
            "apply_link": item.get("redirect_url", ""),
            "aggregate_link": item.get("redirect_url", ""),
            "posted_days_ago": days,
            "applicant_count": None,
            "source": "adzuna",
        })
    return out


def _jsearch(query, city, state, remote, max_days_old) -> list:
    # "near <city>" covers the metro area around small towns (an exact
    # "in <small town>" often returns nothing); the radius filter downstream
    # enforces the user's actual distance preference.
    q = f"{query} near {city}, {state}" if (city and not remote) else query
    if remote:
        q += " remote"
    resp = httpx.get(
        "https://jsearch.p.rapidapi.com/search",
        params={"query": q, "num_pages": 1,
                "date_posted": "3days" if max_days_old <= 3 else "week"},
        headers={"X-RapidAPI-Key": config.JSEARCH_API_KEY,
                 "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
        timeout=30)
    resp.raise_for_status()
    out = []
    for item in resp.json().get("data", []):
        salary = None
        if item.get("job_min_salary") or item.get("job_max_salary"):
            lo, hi = item.get("job_min_salary"), item.get("job_max_salary")
            period = (item.get("job_salary_period") or "YEAR").lower()
            unit = "/hour" if "hour" in period else "/year"
            if lo and hi:
                salary = f"${lo:,.0f} - ${hi:,.0f}{unit}"
            else:
                salary = f"${(hi or lo):,.0f}{unit}"
        days = 0
        posted = item.get("job_posted_at_datetime_utc") or ""
        try:
            days = (datetime.utcnow() - datetime.strptime(posted[:10], "%Y-%m-%d")).days
        except ValueError:
            pass
        loc = item.get("job_location") or \
            ", ".join(p for p in (item.get("job_city"), item.get("job_state")) if p) or \
            ("Remote" if item.get("job_is_remote") else "")
        # JSearch sometimes exposes the employer's own posting among apply_options:
        # prefer options flagged is_direct, then links on the employer's own domain.
        apply_link = item.get("job_apply_link", "")
        aggregate_link = apply_link
        employer_site = item.get("employer_website") or ""
        employer_domain = employer_site.split("//")[-1].split("/")[0].removeprefix("www.")
        for option in item.get("apply_options") or []:
            link = option.get("apply_link", "")
            if link and (option.get("is_direct") or (employer_domain and employer_domain in link)):
                apply_link = link
                break
        out.append({
            "title": item.get("job_title", ""),
            "company": item.get("employer_name", "Unknown"),
            "company_domain": (item.get("employer_website") or "").split("//")[-1].split("/")[0] or None,
            "location": loc,
            "latitude": item.get("job_latitude"),
            "longitude": item.get("job_longitude"),
            "salary": salary,
            "salary_source": "Job listing" if salary else None,
            "description": (item.get("job_description") or "")[:6000],
            "apply_link": apply_link,
            "aggregate_link": aggregate_link if aggregate_link != apply_link else None,
            "posted_days_ago": days,
            "applicant_count": None,
            "source": "jsearch",
        })
    return out


US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def _theirstack(query, city, state, remote, max_days_old) -> list:
    """TheirStack jobs API (https://theirstack.com). Bonus: `final_url` is
    frequently the company's own ATS posting — a direct application link,
    which the ranking pipeline prefers over board links (spec 6.3).
    Note: TheirStack bills API credits per job returned, so keep `limit` low."""
    body = {
        "page": 0,
        "limit": 10,
        "posted_at_max_age_days": max(int(max_days_old), 1),
        "job_title_or": [query],
        "job_country_code_or": ["US"],
        "blur_company_data": False,
    }
    if remote:
        body["remote"] = True
    elif city or state:
        # Patterns are substring regexes: use the city plus the FULL state name —
        # a bare 2-letter code like "UT" would match unrelated words ("Duluth").
        state_name = US_STATE_NAMES.get((state or "").strip().upper(), (state or "").strip())
        patterns = [p for p in (city, state_name) if p]
        body["job_location_pattern_or"] = patterns
    resp = httpx.post(
        "https://api.theirstack.com/v1/jobs/search",
        headers={"Authorization": f"Bearer {config.THEIRSTACK_API_KEY}",
                 "Content-Type": "application/json"},
        json=body, timeout=60)
    resp.raise_for_status()
    out = []
    for item in resp.json().get("data", []):
        company = item.get("company") or (item.get("company_object") or {}).get("name") or "Unknown"
        salary = item.get("salary_string")
        if not salary:
            lo = item.get("min_annual_salary_usd") or item.get("min_annual_salary")
            hi = item.get("max_annual_salary_usd") or item.get("max_annual_salary")
            if lo and hi:
                salary = f"${lo:,.0f} - ${hi:,.0f}/year"
            elif lo or hi:
                salary = f"${(lo or hi):,.0f}/year"
        days = 0
        posted = item.get("date_posted") or ""
        try:
            days = (datetime.now() - datetime.strptime(posted[:10], "%Y-%m-%d")).days
        except ValueError:
            pass
        description = (item.get("description") or "")[:6000]
        statuses = [s.lower() for s in (item.get("employment_statuses") or [])]
        if any("contract" in s or "temporary" in s for s in statuses):
            description = "Contract/temporary position. " + description
        apply_link = item.get("final_url") or item.get("url") or item.get("source_url") or ""
        source_url = item.get("source_url") or item.get("url")
        out.append({
            "title": item.get("job_title", ""),
            "company": company,
            "company_domain": item.get("company_domain"),
            "location": item.get("short_location") or item.get("location") or
                        ("Remote" if item.get("remote") else ""),
            "latitude": item.get("latitude"),
            "longitude": item.get("longitude"),
            "salary": salary,
            "salary_source": "Job listing" if salary else None,
            "description": description,
            "apply_link": apply_link,
            "aggregate_link": source_url if (source_url and source_url != apply_link) else None,
            "posted_days_ago": days,
            "applicant_count": None,
            "source": "theirstack",
        })
    return out


def _usajobs(query, city, state, remote, radius_miles, max_days_old) -> list:
    """USAJOBS.gov — official US federal government jobs API (free, generous
    limits). Listings always include salary ranges and application deadlines;
    PositionURI is the official application page (a direct link)."""
    params = {
        "Keyword": query,
        "ResultsPerPage": 15,
        "DatePosted": min(max(int(max_days_old), 1), 60),
    }
    if remote:
        params["RemoteIndicator"] = "True"
    elif city or state:
        state_name = US_STATE_NAMES.get((state or "").strip().upper(), (state or "").strip())
        params["LocationName"] = ", ".join(p for p in (city, state_name) if p)
        params["Radius"] = min(int(radius_miles or 25), 100)
    resp = httpx.get(
        "https://data.usajobs.gov/api/search",
        params=params,
        headers={"Host": "data.usajobs.gov",
                 "User-Agent": config.USAJOBS_USER_AGENT,
                 "Authorization-Key": config.USAJOBS_API_KEY},
        timeout=60)
    resp.raise_for_status()
    out = []
    for item in (resp.json().get("SearchResult") or {}).get("SearchResultItems") or []:
        d = item.get("MatchedObjectDescriptor") or {}
        pay = (d.get("PositionRemuneration") or [{}])[0]
        salary = None
        try:
            lo, hi = float(pay.get("MinimumRange") or 0), float(pay.get("MaximumRange") or 0)
            unit = "/hour" if (pay.get("RateIntervalCode") or "PA").upper() == "PH" else "/year"
            if lo and hi and hi > lo:
                salary = f"${lo:,.0f} - ${hi:,.0f}{unit}"
            elif lo or hi:
                salary = f"${(hi or lo):,.0f}{unit}"
        except (TypeError, ValueError):
            pass
        days = 0
        posted = d.get("PublicationStartDate") or ""
        try:
            days = (datetime.now() - datetime.strptime(posted[:10], "%Y-%m-%d")).days
        except ValueError:
            pass
        description = (d.get("QualificationSummary") or "")[:5500]
        closes = d.get("ApplicationCloseDate") or ""
        try:  # surface the deadline so the pipeline's deadline detection picks it up
            close_date = datetime.strptime(closes[:10], "%Y-%m-%d")
            description += f" Applications close {close_date.strftime('%B %d, %Y')}."
        except ValueError:
            pass
        loc = (d.get("PositionLocation") or [{}])[0]
        out.append({
            "title": d.get("PositionTitle", "").title() if d.get("PositionTitle", "").isupper()
                     else d.get("PositionTitle", ""),
            "company": d.get("OrganizationName") or d.get("DepartmentName") or "US Federal Government",
            "location": d.get("PositionLocationDisplay") or "",
            "latitude": loc.get("Latitude"),
            "longitude": loc.get("Longitude"),
            "salary": salary,
            "salary_source": "Job listing" if salary else None,
            "description": description,
            "apply_link": (d.get("PositionURI") or "").replace(":443", ""),
            "aggregate_link": None,
            "posted_days_ago": days,
            "applicant_count": None,
            "source": "usajobs",
        })
    return out


# ------------------------------------------------------------ demo data ---

_DEMO_COMPANIES = [
    ("Brightline Health", "healthcare", 4.1), ("Summit Logistics Group", "logistics", 3.6),
    ("NovaTech Solutions", "tech", 4.3), ("Cedar & Main Retail Co.", "retail", 3.4),
    ("Ironwood Manufacturing", "manufacturing", 3.8), ("BlueRiver Financial", "finance", 3.9),
    ("Harborview Hospitality", "hospitality", 3.5), ("GreenGrid Energy", "energy", 4.0),
    ("Atlas Freight Systems", "transportation", 3.7), ("Pinnacle Staffing Partners", "staffing", 3.2),
    ("Lakeside Medical Center", "healthcare", 4.2), ("Quantum Data Labs", "tech", 4.5),
    ("Metro Build Construction", "construction", 3.6), ("Fairway Foods", "food service", 3.3),
    ("Sterling Legal Group", "legal", 3.9), ("Beacon Education Services", "education", 4.0),
]

_DEMO_BENEFITS_SNIPPETS = [
    "We offer health insurance, dental and vision coverage, and a 401k with company match.",
    "Benefits include paid time off, parental leave, and a professional development budget.",
    "Enjoy remote flexibility, stock options, and tuition reimbursement.",
    "Full benefits package: medical insurance, PTO, and 401k retirement plan.",
    "",
]

_DEMO_EXTRAS = [
    "", "Hiring multiple positions for this team.",
    "Urgent hire — immediate start preferred.",
    "Applications close {deadline}.",
    "Candidates must pass a drug screening prior to employment.",
    "",
    "",
]


def _demo(query: str, city: str, state: str, remote: bool) -> list:
    """Deterministic-per-day fabricated listings used when no API keys exist."""
    seed = f"{query}|{city}|{state}|{remote}|{date.today().isoformat()}"
    rng = random.Random(seed)
    role = re.sub(r"\b(?:jobs?|near|in)\b", "", query, flags=re.I).strip().title() or "Specialist"
    out = []
    for i in range(rng.randint(4, 7)):
        company, industry, rating = rng.choice(_DEMO_COMPANIES)
        level = rng.choice(["", "Senior ", "Lead ", "Junior ", ""])
        title = f"{level}{role}"
        base = rng.randint(38, 130) * 1000
        salary = None
        salary_source = None
        if rng.random() < 0.65:
            salary = f"${base:,.0f} - ${int(base * 1.25):,.0f}/year"
            salary_source = "Job listing"
        deadline = (date.today() + timedelta(days=rng.randint(2, 21))).strftime("%B %d, %Y")
        extra = rng.choice(_DEMO_EXTRAS).format(deadline=deadline)
        contract_bit = "This is a contract (1099) position. " if rng.random() < 0.18 else ""
        description = (
            f"{company} is looking for a {title} to join our {industry} team"
            f"{' (fully remote)' if remote else f' in {city}, {state}'}. "
            f"Responsibilities include day-to-day {role.lower()} work, collaborating with "
            f"cross-functional teams, and reporting on outcomes. "
            f"Required: {rng.randint(1, 5)}+ years of relevant experience. "
            f"Preferred qualifications: {', '.join(rng.sample(['Excel', 'SQL', 'Salesforce', 'Project Management', 'Python', 'Customer Service', 'Data Analysis', 'Leadership', 'AWS', 'Communication'], 3))}. "
            f"{contract_bit}{rng.choice(_DEMO_BENEFITS_SNIPPETS)} {extra}"
        ).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", f"{company} {title}".lower()).strip("-")
        direct = rng.random() < 0.6
        company_domain = re.sub(r"[^a-z0-9]+", "", company.lower())[:18]
        out.append({
            "title": title,
            "company": company,
            "location": "Remote" if remote else f"{city}, {state}",
            "latitude": None,
            "longitude": None,
            "salary": salary,
            "salary_source": salary_source,
            "description": description,
            "apply_link": (f"https://careers.{company_domain}.example.com/jobs/{slug}"
                           if direct else f"https://www.indeed.example.com/viewjob?jk={slug}"),
            "aggregate_link": (f"https://www.indeed.example.com/viewjob?jk={slug}" if direct else None),
            "posted_days_ago": rng.randint(0, 2),
            "applicant_count": f"{rng.randint(3, 180)} applicants" if rng.random() < 0.5 else None,
            "source": "demo",
            "_demo_rating": rating,
        })
    return out
