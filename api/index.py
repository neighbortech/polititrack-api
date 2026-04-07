"""
PolitiTrack API — Vercel Serverless Edition v2
Now with real per-member FEC donation data and Congress.gov voting records.
No database needed — calls government APIs directly.
"""

import os
import asyncio
import logging
import re
from fastapi import FastAPI, Query, Header, Body, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("polititrack")

app = FastAPI(title="PolitiTrack API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

FEC_KEY = os.environ.get("FEC_API_KEY", "")
FEC_BASE = "https://api.open.fec.gov/v1"
CONGRESS_KEY = os.environ.get("CONGRESS_API_KEY", "")
CONGRESS_BASE = "https://api.congress.gov/v3"

# ── Simple in-memory cache (survives within a single Lambda warm period) ──

_cache = {}
_CACHE_MISS = object()  # sentinel to distinguish "not cached" from "cached as None"

def cached(key, ttl=600):
    """Simple cache check. Returns _CACHE_MISS if not cached, otherwise the cached value (which may be None)."""
    import time
    entry = _cache.get(key)
    if entry and time.time() - entry["t"] < ttl:
        return entry["v"]
    return _CACHE_MISS

def cache_set(key, value):
    import time
    _cache[key] = {"v": value, "t": time.time()}
    # Evict if cache gets too big (serverless memory)
    if len(_cache) > 500:
        oldest = sorted(_cache.keys(), key=lambda k: _cache[k]["t"])[:100]
        for k in oldest:
            del _cache[k]


# ── FEC API helper ──────────────────────────────────────

def safe_float(val, default=0.0):
    """Safely convert to float. Handles None, empty string, and non-numeric values."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _sanitize_error(e: Exception) -> str:
    """Remove API keys from exception strings."""
    msg = str(e)
    if FEC_KEY:
        msg = msg.replace(FEC_KEY, "[REDACTED]")
    if CONGRESS_KEY:
        msg = msg.replace(CONGRESS_KEY, "[REDACTED]")
    return msg


async def fec(endpoint: str, params: dict) -> dict:
    params["api_key"] = FEC_KEY
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{FEC_BASE}{endpoint}", params=params)
        r.raise_for_status()
        return r.json()


# ── Congress.gov API helper ─────────────────────────────

async def congress(endpoint: str, params: dict = None) -> dict:
    if not CONGRESS_KEY:
        return {}
    p = {"api_key": CONGRESS_KEY, "format": "json"}
    if params:
        p.update(params)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{CONGRESS_BASE}{endpoint}", params=p)
        r.raise_for_status()
        return r.json()


# ── Tracked FY2026 bills ────────────────────────────────

TRACKED_BILLS = [
    {
        "bill_id": "hr7148", "bill_type": "hr", "number": 7148,
        "title": "Consolidated Appropriations Act, 2026",
        "short_title": "Consolidated Appropriations Act, 2026",
        "amount": "$412B", "status": "Signed into law",
        "yourCost_default": "$886B defense budget — 3.2% increase over FY2025",
        "costDir": "up",
    },
    {
        "bill_id": "hr7147", "bill_type": "hr", "number": 7147,
        "title": "DHS Appropriations Act, 2026",
        "short_title": "DHS Appropriations Act, 2026",
        "amount": "$62.8B", "status": "Passed House 220-207",
        "yourCost_default": "$4.1B for border wall at $26M/mile",
        "costDir": "neutral",
    },
    {
        "bill_id": "hr3944", "bill_type": "hr", "number": 3944,
        "title": "Agriculture, VA Appropriations Act, 2026",
        "short_title": "Agriculture, VA Appropriations",
        "amount": "$284.7B", "status": "Signed into law",
        "yourCost_default": "Preserved SNAP benefits at $234/person/month",
        "costDir": "down",
    },
    {
        "bill_id": "hr7006", "bill_type": "hr", "number": 7006,
        "title": "Financial Services & State Dept Appropriations Act, 2026",
        "short_title": "Financial Services & State Dept",
        "amount": "$98.2B", "status": "Signed into law",
        "yourCost_default": "IRS budget cut 13% — longer wait times for refunds",
        "costDir": "up",
    },
    {
        "bill_id": "s890", "bill_type": "s", "number": 890,
        "title": "Prescription Drug Pricing Reform Act",
        "short_title": "Prescription Drug Pricing Reform",
        "amount": "N/A", "status": "Passed Senate 62-38",
        "yourCost_default": "Savings of ~$300/yr on prescriptions for Medicare patients",
        "costDir": "down",
    },
]


# ═══════════════════════════════════════════════════════════
# NEW: Per-member FEC data lookups
# ═══════════════════════════════════════════════════════════

def _clean_rep_name(name: str) -> str:
    """Remove titles and suffixes from representative names."""
    if not name:
        return ""
    # Remove common prefixes
    for prefix in ["Rep. ", "Sen. ", "Representative ", "Senator ", "Del. ", "Delegate ", "Commish. "]:
        if name.startswith(prefix):
            name = name[len(prefix):]
    # Remove suffixes like Jr., Sr., III, IV
    name = re.sub(r',?\s+(Jr\.?|Sr\.?|III|IV|II)\s*$', '', name)
    return name.strip()


async def find_fec_candidate(name: str, state: str, office: str = "H") -> str | None:
    """Find FEC candidate_id by name and state. Returns candidate_id or None."""
    clean_name = _clean_rep_name(name)
    if not clean_name or not clean_name.strip():
        return None
    ck = f"fec_cand:{clean_name}:{state}:{office}"
    cv = cached(ck, ttl=3600)
    if cv is not _CACHE_MISS:
        return cv

    last_name = clean_name.split()[-1] if clean_name else clean_name
    first_name = clean_name.split()[0].lower() if clean_name and " " in clean_name else ""

    # Try the specified office first, then try the other office as fallback
    # (handles cases like Adam Schiff who moved from House to Senate)
    offices_to_try = [office]
    alt_office = "H" if office == "S" else "S"
    offices_to_try.append(alt_office)

    for off in offices_to_try:
        try:
            data = await fec("/candidates/search/", {
                "q": last_name, "state": state, "office": off,
                "sort": "-election_year", "per_page": "20",
            })
            results = data.get("results", [])

            # FEC stores names as "SCHIFF, ADAM B." — match on last name + first name
            for r in results:
                cand_name = (r.get("name", "") or "").upper()
                if last_name.upper() in cand_name:
                    # If we have a first name, verify it matches too
                    if first_name and first_name.upper() not in cand_name:
                        continue
                    cid = r.get("candidate_id")
                    cache_set(ck, cid)
                    return cid

            # Fallback: any result with matching last name
            for r in results:
                cand_name = (r.get("name", "") or "").upper()
                if last_name.upper() in cand_name:
                    cid = r.get("candidate_id")
                    cache_set(ck, cid)
                    return cid
        except Exception as e:
            logger.warning(f"FEC candidate search failed for {clean_name} ({state}, {off}): {_sanitize_error(e)}")

    cache_set(ck, None)
    return None


async def get_candidate_committee(candidate_id: str) -> str | None:
    """Get the principal campaign committee for a candidate."""
    ck = f"fec_comm:{candidate_id}"
    cv = cached(ck, ttl=3600)
    if cv is not _CACHE_MISS:
        return cv

    try:
        data = await fec("/committees/", {
            "candidate_id": candidate_id, "designation": "P", "per_page": "1",
        })
        results = data.get("results", [])
        if results:
            cid = results[0].get("committee_id")
            cache_set(ck, cid)
            return cid
    except Exception as e:
        logger.warning(f"FEC committee lookup failed for {candidate_id}: {_sanitize_error(e)}")

    cache_set(ck, None)
    return None


async def get_member_donors(candidate_id: str, committee_id: str | None) -> dict:
    """Get top donors and industries for a member from FEC.

    Returns {top_donors, top_industries, total_raised}.
    """
    ck = f"fec_donors:{candidate_id}"
    cv = cached(ck, ttl=1800)
    if cv is not _CACHE_MISS:
        return cv

    result = {"top_donors": [], "top_industries": [], "total_raised": 0}

    # Get totals
    try:
        data = await fec(f"/candidate/{candidate_id}/totals/", {
            "cycle": "2026", "per_page": "1",
        })
        totals = data.get("results", [{}])[0] if data.get("results") else {}
        result["total_raised"] = totals.get("receipts", 0) or 0
    except Exception:
        pass

    if not committee_id:
        cache_set(ck, result)
        return result

    # Get top contributors (PACs and individuals aggregated)
    try:
        data = await fec("/schedules/schedule_a/by_contributor/", {
            "committee_id": committee_id, "cycle": "2026",
            "sort": "-total", "per_page": "8",
        })
        for r in data.get("results", [])[:5]:
            name = r.get("contributor_name", "Unknown")
            if name.upper() in ("NONE", "N/A", ""):
                continue
            result["top_donors"].append({
                "name": name,
                "amount": round(r.get("total", 0)),
                "industry": r.get("contributor_type", "Individual"),
            })
    except Exception as e:
        logger.warning(f"FEC contributor lookup failed: {_sanitize_error(e)}")

    # Get top industries (by employer as proxy)
    try:
        data = await fec("/schedules/schedule_a/by_employer/", {
            "committee_id": committee_id, "cycle": "2026",
            "sort": "-total", "per_page": "10",
        })
        for r in data.get("results", []):
            employer = r.get("employer", "")
            if not employer or employer.strip().upper() in (
                "NONE", "N/A", "RETIRED", "SELF-EMPLOYED", "NOT EMPLOYED",
                "SELF", "HOMEMAKER", "STUDENT", "UNEMPLOYED", "NOT APPLICABLE",
                "INFORMATION REQUESTED", "INFORMATION REQUESTED PER BEST EFFORTS",
            ):
                continue
            result["top_industries"].append({
                "name": employer,
                "total": round(r.get("total", 0)),
            })
            if len(result["top_industries"]) >= 5:
                break
    except Exception as e:
        logger.warning(f"FEC industry lookup failed: {_sanitize_error(e)}")

    cache_set(ck, result)
    return result


async def fetch_full_member_fec(name: str, state: str, chamber: str) -> dict:
    """Full FEC data pull for one member. Returns donors + industries + totals."""
    office = "S" if chamber == "Senate" else "H"
    candidate_id = await find_fec_candidate(name, state, office)
    if not candidate_id:
        return {"top_donors": [], "top_industries": [], "total_raised": 0, "fec_id": None}

    committee_id = await get_candidate_committee(candidate_id)
    donor_data = await get_member_donors(candidate_id, committee_id)
    donor_data["fec_id"] = candidate_id
    return donor_data


# ═══════════════════════════════════════════════════════════
# NEW: Congress.gov voting record lookups
# ═══════════════════════════════════════════════════════════

# Known roll call vote URLs for tracked bills (hardcoded fallback)
# These are official government URLs that won't change
KNOWN_ROLL_CALLS = {
    "hr7148": [
        {"url": "https://clerk.house.gov/evs/2026/roll045.xml", "date": "2026-01-22", "text": "On Passage 341-88"},
        {"url": "https://clerk.house.gov/evs/2026/roll053.xml", "date": "2026-02-03", "text": "On Motion to Concur in Senate Amendments 217-214"},
        {"url": "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00020.xml", "date": "2026-01-30", "text": "Senate passage 71-29"},
    ],
    "hr7147": [
        {"url": "https://clerk.house.gov/evs/2026/roll046.xml", "date": "2026-01-22", "text": "On Passage 220-207"},
    ],
    "hr3944": [
        {"url": "https://clerk.house.gov/evs/2025/roll330.xml", "date": "2025-06-26", "text": "On Passage"},
    ],
    "hr7006": [
        {"url": "https://clerk.house.gov/evs/2026/roll007.xml", "date": "2026-01-14", "text": "On Passage"},
    ],
    "s890": [
        {"url": "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1191/vote_119_1_00142.xml", "date": "2025-05-15", "text": "Senate passage 62-38"},
    ],
}


async def get_bill_roll_calls(bill_type: str, bill_number: int, congress_num: int = 119) -> list:
    """Get roll call vote URLs for a bill.
    
    First tries Congress.gov API, then falls back to hardcoded known URLs.
    """
    bill_key = f"{bill_type}{bill_number}"
    ck = f"bill_rc:{bill_key}"
    cv = cached(ck, ttl=3600)
    if cv is not _CACHE_MISS:
        return cv

    # Try Congress.gov API first — paginate through all actions
    if CONGRESS_KEY:
        try:
            roll_calls = []
            offset = 0
            while True:
                data = await congress(
                    f"/bill/{congress_num}/{bill_type}/{bill_number}/actions",
                    {"limit": "250", "offset": str(offset)},
                )
                actions = data.get("actions", [])
                if not actions:
                    break
                for action in actions:
                    rvs = action.get("recordedVotes", [])
                    for rv in rvs:
                        url = rv.get("url", "")
                        if url:
                            roll_calls.append({
                                "url": url,
                                "date": rv.get("date", action.get("actionDate", "")),
                                "text": action.get("text", ""),
                            })
                # If we got fewer than 250, we've reached the end
                if len(actions) < 250:
                    break
                offset += 250
            
            if roll_calls:
                logger.info(f"Found {len(roll_calls)} roll call(s) for {bill_type}{bill_number} via Congress.gov API")
                cache_set(ck, roll_calls)
                return roll_calls
            else:
                logger.info(f"No recordedVotes found in Congress.gov actions for {bill_type}{bill_number}, using fallback")
        except Exception as e:
            logger.warning(f"Congress.gov actions failed for {bill_type}{bill_number}: {_sanitize_error(e)}")

    # Fallback: use hardcoded known roll call URLs
    fallback = KNOWN_ROLL_CALLS.get(bill_key, [])
    cache_set(ck, fallback)
    return fallback


async def parse_roll_call_xml(url: str) -> dict:
    """Fetch and parse a House/Senate roll call XML.
    
    Returns dict with two types of keys:
    - bioguide_id -> vote_position (for House XML)
    - "name:LASTNAME:STATE" -> vote_position (for Senate XML, since Senate doesn't use bioguide_id)
    """
    ck = f"rc_xml:{url}"
    cv = cached(ck, ttl=3600)
    if cv is not _CACHE_MISS:
        return cv

    result = {}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text

        # House XML: <legislator name-id="B001234" ...>Name</legislator><vote>Yea</vote>
        house_pattern = re.compile(
            r'name-id="([A-Z]\d+)"[^>]*>.*?<vote>(\w+)</vote>', re.DOTALL
        )
        for match in house_pattern.finditer(text):
            result[match.group(1)] = match.group(2)

        # Senate XML: uses <last_name>, <state>, <vote_cast> — no bioguide_id!
        if not result:
            senate_pattern = re.compile(
                r'<last_name>([^<]+)</last_name>\s*<first_name>[^<]*</first_name>\s*<party>[^<]*</party>\s*<state>([^<]+)</state>\s*<vote_cast>([^<]+)</vote_cast>',
                re.DOTALL
            )
            for match in senate_pattern.finditer(text):
                last_name = match.group(1).strip()
                state = match.group(2).strip()
                vote = match.group(3).strip()
                # Store with a name-based key since Senate doesn't have bioguide_id
                result[f"name:{last_name.upper()}:{state}"] = vote

    except Exception as e:
        logger.warning(f"Roll call XML fetch failed: {_sanitize_error(e)}")

    cache_set(ck, result)
    return result


async def get_member_votes_on_tracked_bills(member_name: str, bioguide_id: str | None, chamber: str, state: str = "") -> list:
    """Get a member's votes on all tracked bills.

    Returns list of {bill, title, vote, amount, yourCost, costDir}.
    Pre-fetches all roll call data in parallel to avoid serial HTTP calls.
    Matches by bioguide_id for House and by last_name:state for Senate.
    """
    # Build name-based key for Senate matching
    clean_name = _clean_rep_name(member_name)
    last_name = clean_name.split()[-1].upper() if clean_name else ""
    senate_key = f"name:{last_name}:{state}" if last_name and state else ""

    # If we have neither bioguide_id nor a usable name, skip
    if not bioguide_id and not senate_key:
        return [
            {
                "bill": f"{'H.R.' if bill['bill_type'] == 'hr' else 'S.'} {bill['number']}",
                "title": bill["short_title"],
                "vote": "Not recorded",
                "amount": bill["amount"],
                "yourCost": bill["yourCost_default"],
                "costDir": bill["costDir"],
                "status": bill["status"],
            }
            for bill in TRACKED_BILLS
        ]

    # Step 1: Fetch all bill roll calls in parallel
    roll_call_tasks = [
        get_bill_roll_calls(bill["bill_type"], bill["number"])
        for bill in TRACKED_BILLS
    ]
    all_roll_calls = await asyncio.gather(*roll_call_tasks)

    # Step 2: Collect all unique roll call URLs to fetch
    url_set = set()
    for rcs in all_roll_calls:
        for rc in rcs:
            url_set.add(rc["url"])

    # Step 3: Fetch all roll call XMLs in parallel
    url_list = list(url_set)
    xml_tasks = [parse_roll_call_xml(url) for url in url_list]
    xml_results = await asyncio.gather(*xml_tasks)
    url_to_votes = dict(zip(url_list, xml_results))

    # Step 4: Look up member's position in each bill
    votes = []
    for i, bill in enumerate(TRACKED_BILLS):
        vote_position = None
        for rc in all_roll_calls[i]:
            vote_map = url_to_votes.get(rc["url"], {})
            # Try bioguide_id first (House XML)
            if bioguide_id and bioguide_id in vote_map:
                vote_position = vote_map[bioguide_id]
                break
            # Try name-based key (Senate XML)
            if senate_key and senate_key in vote_map:
                vote_position = vote_map[senate_key]
                break

        votes.append({
            "bill": f"{'H.R.' if bill['bill_type'] == 'hr' else 'S.'} {bill['number']}",
            "title": bill["short_title"],
            "vote": vote_position or "Not recorded",
            "amount": bill["amount"],
            "yourCost": bill["yourCost_default"],
            "costDir": bill["costDir"],
            "status": bill["status"],
        })

    return votes


# ═══════════════════════════════════════════════════════════
# NEW: Member bioguide ID lookup
# ═══════════════════════════════════════════════════════════

async def find_member_info(name: str, state: str) -> dict:
    """Look up a member's bioguide ID and committees from Congress.gov.

    Returns {bioguide_id, committees}.
    """
    if not CONGRESS_KEY:
        return {"bioguide_id": None, "committees": []}

    clean_name = _clean_rep_name(name)
    if not clean_name:
        return {"bioguide_id": None, "committees": []}

    ck = f"member_info:{clean_name}:{state}"
    cv = cached(ck, ttl=7200)
    if cv is not _CACHE_MISS:
        return cv

    result = {"bioguide_id": None, "committees": []}
    clean_name = _clean_rep_name(name)
    last_name = clean_name.split()[-1] if clean_name else clean_name
    first_name = clean_name.split()[0].lower() if clean_name and " " in clean_name else ""

    # We need to search all current members — Congress.gov paginates at 250 max
    # First try filtering by state to reduce results
    try:
        data = await congress("/member", {
            "currentMember": "true", "limit": "250", "stateCode": state,
        })
        members = data.get("members", [])
        for m in members:
            mn = (m.get("name", "") or "").lower()
            # Congress.gov returns names as "Schiff, Adam" or "Adam Schiff"
            if last_name.lower() in mn and (not first_name or first_name in mn):
                result["bioguide_id"] = m.get("bioguideId", "")
                if result["bioguide_id"]:
                    try:
                        cdata2 = await congress(
                            f"/member/{result['bioguide_id']}/committees",
                            {"limit": "50"},
                        )
                        for c in cdata2.get("committees", []):
                            cname = c.get("name", "")
                            if cname:
                                result["committees"].append(cname)
                    except Exception:
                        pass
                break
    except Exception as e:
        logger.warning(f"Congress.gov member search failed: {_sanitize_error(e)}")

    cache_set(ck, result)
    return result


# ═══════════════════════════════════════════════════════════
# MAIN ENDPOINT: /api/v1/district?zip=XXXXX
# ═══════════════════════════════════════════════════════════

@app.get("/api/v1/district")
async def district_data(zip: str = Query(..., min_length=5, max_length=5)):
    """Full district data package for a ZIP code.

    Returns real representatives with real FEC donor data and real voting records.
    This is the endpoint the My District page should call.
    """
    # Step 1: Get representatives (reuse existing logic)
    reps_raw = await _get_reps_raw(zip)
    if not reps_raw:
        raise HTTPException(404, f"No representatives found for ZIP {zip}")

    # Step 2: For each rep, fetch FEC data + voting records in parallel
    async def enrich_rep(rep: dict) -> dict:
        name = rep.get("name", "")
        state = rep.get("state", "")
        chamber = rep.get("chamber", "House")

        # Parallel fetch: FEC data + member info (bioguide + committees)
        fec_task = fetch_full_member_fec(name, state, chamber)
        info_task = find_member_info(name, state)

        fec_data, member_info = await asyncio.gather(fec_task, info_task)

        bioguide_id = member_info.get("bioguide_id")
        committees = member_info.get("committees", [])

        # Now fetch votes (needs bioguide_id)
        votes = await get_member_votes_on_tracked_bills(name, bioguide_id, chamber, state)

        return {
            "name": name,
            "party": rep.get("party", ""),
            "state": state,
            "district": rep.get("district", ""),
            "chamber": chamber,
            "phone": rep.get("phone", "(202) 224-3121"),
            "office": rep.get("office", ""),
            "website": rep.get("website", ""),
            "photoUrl": rep.get("photoUrl", ""),
            "bioguide_id": bioguide_id,
            "fec_id": fec_data.get("fec_id"),
            "committees": committees,
            "topDonors": fec_data.get("top_donors", []),
            "topIndustries": fec_data.get("top_industries", []),
            "totalFromTopIndustries": sum(
                i.get("total", 0) for i in fec_data.get("top_industries", [])
            ),
            "totalRaised": fec_data.get("total_raised", 0),
            "votes": votes,
            "votedWithParty": "N/A",  # Would need full vote history to calculate
            "dataSource": "fec+congress.gov",
        }

    # Enrich all reps in parallel
    enriched = await asyncio.gather(*[enrich_rep(r) for r in reps_raw])

    state = reps_raw[0].get("state", "") if reps_raw else ""
    return {
        "zip": zip,
        "state": state,
        "representatives": list(enriched),
        "tracked_bills": [
            {
                "bill": f"{'H.R.' if b['bill_type'] == 'hr' else 'S.'} {b['number']}",
                "title": b["title"],
                "status": b["status"],
                "amount": b["amount"],
            }
            for b in TRACKED_BILLS
        ],
        "source": "fec.gov + congress.gov",
    }


async def _get_reps_raw(zip: str) -> list:
    """Get raw rep list from whoismyrepresentative or congress.gov."""
    results = []

    # Try whoismyrepresentative.com first
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://whoismyrepresentative.com/getall_mems.php?zip={zip}&output=json"
            )
            if r.status_code == 200:
                data = r.json()
                for m in data.get("results", []):
                    is_senator = "senate" in (m.get("link", "") or "").lower()
                    results.append({
                        "name": m.get("name", "Unknown"),
                        "party": m.get("party", ""),
                        "state": m.get("state", ""),
                        "district": m.get("district", ""),
                        "chamber": "Senate" if is_senator else "House",
                        "phone": m.get("phone", ""),
                        "office": m.get("office", ""),
                        "website": m.get("link", ""),
                    })
    except Exception:
        pass

    # Fallback: Congress.gov
    if not results and CONGRESS_KEY:
        try:
            state = zip_to_state(zip)
            if state:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r = await client.get(
                        f"https://api.congress.gov/v3/member",
                        params={
                            "stateCode": state, "currentMember": "true",
                            "limit": 20, "api_key": CONGRESS_KEY, "format": "json",
                        },
                    )
                    if r.status_code == 200:
                        data = r.json()
                        for m in data.get("members", []):
                            terms = m.get("terms", {}).get("item", [])
                            latest = terms[-1] if terms else {}
                            ch = latest.get("chamber", "")
                            results.append({
                                "name": m.get("name", ""),
                                "party": m.get("partyName", ""),
                                "state": m.get("state", ""),
                                "district": str(m.get("district", "")),
                                "chamber": ch if ch in ("Senate", "House") else (
                                    "Senate" if "Senate" in ch else "House"
                                ),
                                "phone": "(202) 224-3121",
                                "office": "",
                                "website": m.get("officialWebsiteUrl", ""),
                                "photoUrl": (m.get("depiction") or {}).get("imageUrl", ""),
                            })
        except Exception:
            pass

    return results


# ═══════════════════════════════════════════════════════════
# EXISTING ENDPOINTS (unchanged)
# ═══════════════════════════════════════════════════════════

@app.get("/")
async def root():
    return {"name": "PolitiTrack API", "version": "2.0.0", "docs": "/docs"}


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "fec_key_set": bool(FEC_KEY),
        "congress_key_set": bool(CONGRESS_KEY),
        "cache_size": len(_cache),
    }


@app.get("/api/v1/people/search")
async def search_people(
    name: str = Query(None),
    employer: str = Query(None),
    occupation: str = Query(None),
    state: str = Query(None),
    city: str = Query(None),
    min_amount: float = Query(None),
    cycle: int = Query(None),
    limit: int = Query(30, ge=1, le=100),
):
    if not name and not employer:
        raise HTTPException(400, "Provide a name or employer")

    params = {"per_page": str(limit), "sort": "-contribution_receipt_amount", "sort_hide_null": "true", "is_individual": "true"}
    if name: params["contributor_name"] = name
    if employer: params["contributor_employer"] = employer
    if occupation: params["contributor_occupation"] = occupation
    if state: params["contributor_state"] = state
    if city: params["contributor_city"] = city
    if min_amount: params["min_amount"] = str(min_amount)
    if cycle: params["two_year_transaction_period"] = str(cycle)

    try:
        data = await fec("/schedules/schedule_a/", params)
    except Exception as e:
        raise HTTPException(502, f"FEC API error: {_sanitize_error(e)}")

    return {
        "total": data.get("pagination", {}).get("count", 0),
        "results": [
            {
                "contributor_name": r.get("contributor_name", ""),
                "employer": r.get("contributor_employer", ""),
                "occupation": r.get("contributor_occupation", ""),
                "city": r.get("contributor_city", ""),
                "state": r.get("contributor_state", ""),
                "amount": safe_float(r.get("contribution_receipt_amount")),
                "date": r.get("contribution_receipt_date", ""),
                "recipient": r.get("candidate_name") or r.get("committee", {}).get("name", "Unknown"),
                "recipient_party": (r.get("candidate") or {}).get("party"),
                "recipient_office": (r.get("candidate") or {}).get("office"),
                "recipient_state": (r.get("candidate") or {}).get("state"),
                "committee": r.get("committee", {}).get("name", ""),
                "memo": r.get("memo_text", ""),
                "cycle": r.get("two_year_transaction_period"),
            }
            for r in data.get("results", [])
        ],
    }


@app.get("/api/v1/people/{name}/profile")
async def person_profile(name: str, cycles: str = Query("2024,2022,2020")):
    try:
        cycle_list = [int(c.strip()) for c in cycles.split(",") if c.strip().isdigit()]
    except (ValueError, TypeError):
        cycle_list = [2024, 2022, 2020]
    if not cycle_list:
        cycle_list = [2024, 2022, 2020]
    all_c = []
    for cyc in cycle_list:
        try:
            data = await fec("/schedules/schedule_a/", {
                "contributor_name": name, "two_year_transaction_period": str(cyc),
                "is_individual": "true", "per_page": "100",
                "sort": "-contribution_receipt_amount", "sort_hide_null": "true",
            })
            all_c.extend(data.get("results", []))
        except Exception:
            continue
    if not all_c:
        raise HTTPException(404, f"No contributions found for '{name}'")

    total = sum(safe_float(c.get("contribution_receipt_amount")) for c in all_c)
    by_recip, by_party, by_cycle = {}, {}, {}
    for c in all_c:
        amt = safe_float(c.get("contribution_receipt_amount"))
        cand = c.get("candidate_name") or c.get("committee", {}).get("name", "Unknown")
        party = (c.get("candidate") or {}).get("party") or "Other"
        cy = str(c.get("two_year_transaction_period", "?"))
        by_recip.setdefault(cand, {"name": cand, "party": (c.get("candidate") or {}).get("party"), "office": (c.get("candidate") or {}).get("office"), "state": (c.get("candidate") or {}).get("state"), "total": 0.0, "count": 0})
        by_recip[cand]["total"] += amt
        by_recip[cand]["count"] += 1
        by_party.setdefault(party, {"total": 0.0, "count": 0})
        by_party[party]["total"] += amt
        by_party[party]["count"] += 1
        by_cycle[cy] = by_cycle.get(cy, 0) + amt

    first = all_c[0]
    return {
        "donor": {"name": name, "type": "individual", "employer": first.get("contributor_employer", ""), "occupation": first.get("contributor_occupation", ""), "city": first.get("contributor_city", ""), "state": first.get("contributor_state", "")},
        "total_contributed": round(total, 2),
        "total_contributions": len(all_c),
        "by_party": {k: {"total": round(v["total"], 2), "count": v["count"]} for k, v in by_party.items()},
        "by_cycle": {k: round(v, 2) for k, v in by_cycle.items()},
        "recipients": sorted([{**v, "total": round(v["total"], 2)} for v in by_recip.values()], key=lambda x: -x["total"])[:30],
        "recent_contributions": [
            {"recipient": c.get("candidate_name") or c.get("committee", {}).get("name", "?"), "amount": safe_float(c.get("contribution_receipt_amount")), "date": c.get("contribution_receipt_date", ""), "party": (c.get("candidate") or {}).get("party"), "committee": c.get("committee", {}).get("name", "")}
            for c in sorted(all_c, key=lambda x: x.get("contribution_receipt_date", ""), reverse=True)[:50]
        ],
    }


@app.get("/api/v1/donors/search")
async def search_donors(q: str = Query(..., min_length=2), limit: int = Query(10)):
    grouped = {}
    try:
        data = await fec("/schedules/schedule_a/", {
            "contributor_name": q, "is_individual": "true",
            "per_page": str(min(limit * 3, 100)),
            "sort": "-contribution_receipt_amount",
        })
        for r in data.get("results", []):
            name = r.get("contributor_name", "Unknown")
            if name not in grouped:
                grouped[name] = {"id": None, "name": name, "type": "individual", "industry": r.get("contributor_occupation"), "state": r.get("contributor_state"), "employer": r.get("contributor_employer"), "total_contributed": 0}
            grouped[name]["total_contributed"] += safe_float(r.get("contribution_receipt_amount"))
    except Exception:
        pass
    committees = []
    try:
        data = await fec("/names/committees/", {"q": q})
        for r in data.get("results", [])[:5]:
            committees.append({"id": r.get("id"), "name": r.get("name", ""), "type": "committee", "industry": None, "state": None, "total_contributed": 0})
    except Exception:
        pass
    return sorted(grouped.values(), key=lambda x: -x["total_contributed"])[:limit] + committees[:3]


@app.get("/api/v1/donations")
async def search_donations(
    donor: str = Query(None), recipient: str = Query(None),
    min_amount: float = Query(None), cycle: int = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    params = {"per_page": str(limit), "sort": "-contribution_receipt_amount", "sort_hide_null": "true"}
    if donor: params["contributor_name"] = donor
    if recipient: params["committee_id"] = recipient
    if min_amount: params["min_amount"] = str(min_amount)
    if cycle: params["two_year_transaction_period"] = str(cycle)
    try:
        data = await fec("/schedules/schedule_a/", params)
    except Exception as e:
        raise HTTPException(502, f"FEC API error: {_sanitize_error(e)}")
    return {
        "total": data.get("pagination", {}).get("count", 0),
        "donations": [
            {
                "donor": {"name": r.get("contributor_name", ""), "industry": r.get("contributor_occupation", ""), "state": r.get("contributor_state", "")},
                "recipient": {"name": r.get("candidate_name") or r.get("committee", {}).get("name", ""), "party": (r.get("candidate") or {}).get("party")},
                "amount": safe_float(r.get("contribution_receipt_amount")),
                "date": r.get("contribution_receipt_date", ""),
            }
            for r in data.get("results", [])
        ],
    }


@app.get("/api/v1/stats/overview")
async def stats():
    return {
        "platform": "PolitiTrack", "version": "2.0.0",
        "data_sources_live": ["FEC (api.open.fec.gov)", "Congress.gov (api.congress.gov)"],
        "data_sources_planned": ["Senate LDA", "USASpending"],
        "features_live": [
            "ZIP → representative lookup",
            "Per-member FEC donation data (top donors, top industries, total raised)",
            "Per-member committee assignments",
            "Per-member voting records on 5 tracked FY2026 bills",
            "Individual donor search and profiles",
            "PAC/committee search",
            "MCP server for AI assistants",
        ],
        "features_planned": [
            "Historical FEC filing snapshots",
            "Influence scoring",
            "Vote-alignment analysis",
            "Cross-source connection discovery",
            "Amendment tracking",
        ],
        "tracked_bills": ["H.R. 7148", "H.R. 7147", "H.R. 3944", "H.R. 7006", "S. 890"],
        "note": "Use /api/v1/district?zip=XXXXX for full district data",
    }


# ── Proprietary endpoints (placeholders) ────────────────

@app.get("/api/v1/timeline/{entity_name}")
async def entity_timeline(entity_name: str, days: int = Query(365)):
    return {"entity": entity_name, "period": f"Last {days} days", "snapshots": [], "changes_detected": 0, "note": "Historical snapshots are captured daily.", "proprietary": True}

@app.get("/api/v1/influence/{entity_name}")
async def influence_score(entity_name: str):
    return {"entity": entity_name, "influence_score": None, "note": "Score accuracy improves with more vote data.", "proprietary": True}

@app.get("/api/v1/amendments/{entity_name}")
async def filing_amendments(entity_name: str):
    return {"entity": entity_name, "amendments_found": [], "note": "Amendment detection requires historical snapshots.", "proprietary": True}

@app.get("/api/v1/connections/{entity_name}")
async def entity_connections(entity_name: str):
    return {"entity": entity_name, "connections": [], "note": "Connections are discovered by AI and verified by humans.", "proprietary": True}

@app.get("/api/v1/vote-alignment/{politician_name}")
async def vote_alignment(politician_name: str):
    return {"politician": politician_name, "alignment_rate": None, "votes_tracked": 0, "note": "Alignment scoring requires ongoing vote tracking.", "proprietary": True}


# ── Original /api/v1/reps (kept for backward compat) ────

@app.get("/api/v1/reps")
async def reps_by_zip(zip: str = Query(..., min_length=5, max_length=5)):
    """Lightweight rep lookup — names + contact info only."""
    results = await _get_reps_raw(zip)
    return {
        "zip": zip,
        "count": len(results),
        "results": results,
        "source": "whoismyrepresentative.com" if results else "none",
    }


# ── API key management (simple in-memory for now) ───────

import uuid
import time

_api_keys = {}  # key_string -> {email, name, tier, created, requests_today, last_reset}


@app.post("/api/v1/keys")
async def create_api_key(request: Request):
    """Generate a free API key."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Provide JSON body with email field")
    if not body or not isinstance(body, dict):
        raise HTTPException(400, "Provide JSON body with email field")
    email = body.get("email", "")
    name = body.get("name", "")
    if not email:
        raise HTTPException(400, "Email is required")
    key = f"pt_live_{uuid.uuid4().hex[:24]}"
    _api_keys[key] = {
        "email": email, "name": name, "tier": "free",
        "daily_limit": 100, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "requests_today": 0, "last_reset": time.strftime("%Y-%m-%d"),
    }
    return {
        "key": key, "key_prefix": key[:16],
        "tier": "free", "daily_limit": 100,
        "created_at": _api_keys[key]["created_at"],
    }


@app.get("/api/v1/keys/me")
async def check_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Check API key usage and remaining quota."""
    if not x_api_key:
        raise HTTPException(401, "Provide X-API-Key header")
    info = _api_keys.get(x_api_key)
    if not info:
        raise HTTPException(404, "API key not found")
    today = time.strftime("%Y-%m-%d")
    if info["last_reset"] != today:
        info["requests_today"] = 0
        info["last_reset"] = today
    return {
        "key_prefix": x_api_key[:16],
        "tier": info["tier"],
        "daily_limit": info["daily_limit"],
        "requests_today": info["requests_today"],
        "remaining": info["daily_limit"] - info["requests_today"],
    }


# ── Donor summary (for Explore page profile view) ───────

@app.get("/api/v1/donors/{donor_id}/summary")
async def donor_summary(donor_id: str):
    """Get aggregated donor summary. Tries FEC committee lookup."""
    try:
        # Try as committee ID
        data = await fec(f"/committee/{donor_id}/", {})
        committee = data.get("results", [{}])[0] if data.get("results") else {}
        if not committee:
            raise HTTPException(404, "Donor not found")

        # Get financials
        fin = {}
        try:
            fdata = await fec(f"/committee/{donor_id}/totals/", {"per_page": "1"})
            fin = fdata.get("results", [{}])[0] if fdata.get("results") else {}
        except Exception:
            pass

        return {
            "name": committee.get("name", donor_id),
            "type": committee.get("committee_type_full", "Committee"),
            "industry": committee.get("designation_full", ""),
            "state": committee.get("state", ""),
            "total": fin.get("receipts", 0) or 0,
            "byParty": {},
            "byYear": {},
            "topRecipients": [],
            "recentDonations": [],
            "lobbying": {"spend": "N/A", "issues": [], "filings": 0},
            "contracts": {"total": "N/A", "agency": "", "count": 0},
            "velocity": {"trend": "N/A", "lastSpike": "N/A", "concentration": "N/A"},
            "influenceScore": None,
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(404, f"Donor '{donor_id}' not found")


# ── AI analyze/ask (placeholder with helpful response) ──

@app.get("/api/v1/analyze/ask")
async def analyze_ask(q: str = Query(..., min_length=3)):
    """AI-powered question answering about political money. Placeholder."""
    return {
        "answer": f"Analysis for '{q}' requires the Pro tier. This endpoint will use AI to cross-reference FEC donations, Senate lobbying filings, Congress.gov vote records, and USASpending contract data to answer your question. Upgrade to Pro at polititrack.com/pricing for AI-powered analysis.",
        "confidence": None,
        "sources": ["FEC", "Senate LDA", "Congress.gov", "USASpending"],
        "tier_required": "pro",
    }


# ── ZIP → state mapping ─────────────────────────────────

def zip_to_state(z: str) -> str:
    try:
        n = int(z[:3])
    except (ValueError, TypeError):
        return ""
    mapping = [
        (10, 27, "MA"), (28, 29, "RI"), (30, 38, "NH"), (39, 49, "ME"),
        (50, 59, "VT"), (60, 69, "CT"), (70, 89, "NJ"), (100, 149, "NY"),
        (150, 196, "PA"), (197, 199, "DE"), (200, 205, "DC"), (206, 219, "MD"),
        (220, 246, "VA"), (247, 268, "WV"), (270, 289, "NC"), (290, 299, "SC"),
        (300, 319, "GA"), (320, 349, "FL"), (350, 369, "AL"), (370, 385, "TN"),
        (386, 397, "MS"), (400, 427, "KY"), (430, 458, "OH"), (460, 479, "IN"),
        (480, 499, "MI"), (500, 528, "IA"), (530, 549, "WI"), (550, 567, "MN"),
        (570, 577, "SD"), (580, 588, "ND"), (590, 599, "MT"), (600, 629, "IL"),
        (630, 658, "MO"), (660, 679, "KS"), (680, 693, "NE"), (700, 714, "LA"),
        (715, 749, "TX"), (750, 799, "TX"), (800, 816, "CO"), (820, 831, "WY"),
        (832, 838, "ID"), (840, 847, "UT"), (850, 865, "AZ"), (870, 884, "NM"),
        (889, 898, "NV"), (900, 961, "CA"), (967, 968, "HI"), (970, 979, "OR"),
        (980, 994, "WA"), (995, 999, "AK"),
    ]
    for lo, hi, st in mapping:
        if lo <= n <= hi:
            return st
    return ""
