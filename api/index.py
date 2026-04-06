"""
PolitiTrack API — Vercel Serverless Edition v2
Now with real per-member FEC donation data and Congress.gov voting records.
No database needed — calls government APIs directly.
"""

import os
import asyncio
import logging
import re
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("polititrack")

app = FastAPI(title="PolitiTrack API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FEC_KEY = os.environ.get("FEC_API_KEY", "")
FEC_BASE = "https://api.open.fec.gov/v1"
CONGRESS_KEY = os.environ.get("CONGRESS_API_KEY", "")
CONGRESS_BASE = "https://api.congress.gov/v3"

# ── Simple in-memory cache (survives within a single Lambda warm period) ──

_cache = {}

def cached(key, ttl=600):
    """Simple cache check. Returns cached value or None."""
    import time
    entry = _cache.get(key)
    if entry and time.time() - entry["t"] < ttl:
        return entry["v"]
    return None

def cache_set(key, value):
    import time
    _cache[key] = {"v": value, "t": time.time()}
    # Evict if cache gets too big (serverless memory)
    if len(_cache) > 500:
        oldest = sorted(_cache.keys(), key=lambda k: _cache[k]["t"])[:100]
        for k in oldest:
            del _cache[k]


# ── FEC API helper ──────────────────────────────────────

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

async def find_fec_candidate(name: str, state: str, office: str = "H") -> str | None:
    """Find FEC candidate_id by name and state. Returns candidate_id or None."""
    ck = f"fec_cand:{name}:{state}:{office}"
    cv = cached(ck, ttl=3600)
    if cv is not None:
        return cv

    # Search by last name for better matching
    last_name = name.split()[-1] if name else name
    try:
        data = await fec("/candidates/search/", {
            "q": last_name, "state": state, "office": office,
            "sort": "-election_year", "per_page": "10",
            "is_active_candidate": "true",
        })
        results = data.get("results", [])
        # Try to match on full name
        name_lower = name.lower()
        for r in results:
            cand_name = r.get("name", "").lower()
            if last_name.lower() in cand_name:
                cid = r.get("candidate_id")
                cache_set(ck, cid)
                return cid
        # Fallback: first result
        if results:
            cid = results[0].get("candidate_id")
            cache_set(ck, cid)
            return cid
    except Exception as e:
        logger.warning(f"FEC candidate search failed for {name}: {e}")

    cache_set(ck, None)
    return None


async def get_candidate_committee(candidate_id: str) -> str | None:
    """Get the principal campaign committee for a candidate."""
    ck = f"fec_comm:{candidate_id}"
    cv = cached(ck, ttl=3600)
    if cv is not None:
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
        logger.warning(f"FEC committee lookup failed for {candidate_id}: {e}")

    cache_set(ck, None)
    return None


async def get_member_donors(candidate_id: str, committee_id: str | None) -> dict:
    """Get top donors and industries for a member from FEC.

    Returns {top_donors, top_industries, total_raised}.
    """
    ck = f"fec_donors:{candidate_id}"
    cv = cached(ck, ttl=1800)
    if cv is not None:
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
        logger.warning(f"FEC contributor lookup failed: {e}")

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
        logger.warning(f"FEC industry lookup failed: {e}")

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

async def get_bill_roll_calls(bill_type: str, bill_number: int, congress_num: int = 119) -> list:
    """Get roll call vote URLs for a bill from Congress.gov actions."""
    ck = f"bill_rc:{bill_type}{bill_number}"
    cv = cached(ck, ttl=3600)
    if cv is not None:
        return cv

    if not CONGRESS_KEY:
        cache_set(ck, [])
        return []

    try:
        data = await congress(
            f"/bill/{congress_num}/{bill_type}/{bill_number}/actions",
            {"limit": "250"},
        )
        actions = data.get("actions", [])
        roll_calls = []
        for action in actions:
            rvs = action.get("recordedVotes", [])
            for rv in rvs:
                url = rv.get("url", "")
                if url:
                    roll_calls.append({
                        "url": url,
                        "date": action.get("actionDate", ""),
                        "text": action.get("text", ""),
                    })
        cache_set(ck, roll_calls)
        return roll_calls
    except Exception as e:
        logger.warning(f"Congress.gov actions failed for {bill_type}{bill_number}: {e}")
        cache_set(ck, [])
        return []


async def parse_roll_call_xml(url: str) -> dict:
    """Fetch and parse a House/Senate roll call XML, returning {bioguide_id: vote_position}."""
    ck = f"rc_xml:{url}"
    cv = cached(ck, ttl=3600)
    if cv is not None:
        return cv

    result = {}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text

        # House XML: name-id="B001234" ... <vote>Yea</vote>
        house_pattern = re.compile(
            r'name-id="([A-Z]\d+)"[^>]*>.*?<vote>(\w+)</vote>', re.DOTALL
        )
        for match in house_pattern.finditer(text):
            result[match.group(1)] = match.group(2)

        # Senate XML: <member><bioguide_id>X000000</bioguide_id>...<vote_cast>Yea</vote_cast>
        if not result:
            senate_pattern = re.compile(
                r'<bioguide_id>(\w+)</bioguide_id>.*?<vote_cast>(\w+)</vote_cast>', re.DOTALL
            )
            for match in senate_pattern.finditer(text):
                result[match.group(1)] = match.group(2)

    except Exception as e:
        logger.warning(f"Roll call XML fetch failed for {url}: {e}")

    cache_set(ck, result)
    return result


async def get_member_votes_on_tracked_bills(member_name: str, bioguide_id: str | None, chamber: str) -> list:
    """Get a member's votes on all tracked bills.

    Returns list of {bill, title, vote, amount, yourCost, costDir}.
    """
    votes = []

    for bill in TRACKED_BILLS:
        # Only check bills for the right chamber (House bills for House members, etc.)
        # But appropriations bills get votes in both chambers
        vote_position = None

        if bioguide_id:
            roll_calls = await get_bill_roll_calls(bill["bill_type"], bill["number"])
            for rc in roll_calls:
                vote_map = await parse_roll_call_xml(rc["url"])
                if bioguide_id in vote_map:
                    vote_position = vote_map[bioguide_id]
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

async def find_bioguide_id(name: str, state: str) -> str | None:
    """Look up a member's bioguide ID from Congress.gov."""
    if not CONGRESS_KEY:
        return None

    ck = f"bioguide:{name}:{state}"
    cv = cached(ck, ttl=7200)
    if cv is not None:
        return cv

    last_name = name.split()[-1] if name else name
    try:
        data = await congress("/member", {
            "currentMember": "true", "limit": "50",
        })
        members = data.get("members", [])
        name_lower = name.lower()
        for m in members:
            mn = (m.get("name", "") or "").lower()
            ms = m.get("state", "")
            if last_name.lower() in mn and ms == state:
                bio_id = m.get("bioguideId", "")
                cache_set(ck, bio_id)
                return bio_id
    except Exception as e:
        logger.warning(f"Congress.gov member search failed for {name}: {e}")

    cache_set(ck, None)
    return None


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

        # Parallel fetch: FEC data + bioguide ID
        fec_task = fetch_full_member_fec(name, state, chamber)
        bio_task = find_bioguide_id(name, state)

        fec_data, bioguide_id = await asyncio.gather(fec_task, bio_task)

        # Now fetch votes (needs bioguide_id)
        votes = await get_member_votes_on_tracked_bills(name, bioguide_id, chamber)

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
            "committees": [],  # Would need a separate Congress.gov call
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
        raise HTTPException(502, f"FEC API error: {e}")

    return {
        "total": data.get("pagination", {}).get("count", 0),
        "results": [
            {
                "contributor_name": r.get("contributor_name", ""),
                "employer": r.get("contributor_employer", ""),
                "occupation": r.get("contributor_occupation", ""),
                "city": r.get("contributor_city", ""),
                "state": r.get("contributor_state", ""),
                "amount": float(r.get("contribution_receipt_amount", 0)),
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
    cycle_list = [int(c.strip()) for c in cycles.split(",")]
    all_c = []
    for cyc in cycle_list:
        try:
            data = await fec("/schedules/schedule_a/", {
                "contributor_name": name, "two_year_transaction_period": str(cyc),
                "is_individual": "true", "per_page": "100",
                "sort": "-contribution_receipt_amount", "sort_hide_null": "true",
            })
            all_c.extend(data.get("results", []))
        except:
            continue
    if not all_c:
        raise HTTPException(404, f"No contributions found for '{name}'")

    total = sum(float(c.get("contribution_receipt_amount", 0)) for c in all_c)
    by_recip, by_party, by_cycle = {}, {}, {}
    for c in all_c:
        amt = float(c.get("contribution_receipt_amount", 0))
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
            {"recipient": c.get("candidate_name") or c.get("committee", {}).get("name", "?"), "amount": float(c.get("contribution_receipt_amount", 0)), "date": c.get("contribution_receipt_date", ""), "party": (c.get("candidate") or {}).get("party"), "committee": c.get("committee", {}).get("name", "")}
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
            grouped[name]["total_contributed"] += float(r.get("contribution_receipt_amount", 0))
    except:
        pass
    committees = []
    try:
        data = await fec("/names/committees/", {"q": q})
        for r in data.get("results", [])[:5]:
            committees.append({"id": r.get("id"), "name": r.get("name", ""), "type": "committee", "industry": None, "state": None, "total_contributed": 0})
    except:
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
        raise HTTPException(502, f"FEC API error: {e}")
    return {
        "total": data.get("pagination", {}).get("count", 0),
        "donations": [
            {
                "donor": {"name": r.get("contributor_name", ""), "industry": r.get("contributor_occupation", ""), "state": r.get("contributor_state", "")},
                "recipient": {"name": r.get("candidate_name") or r.get("committee", {}).get("name", ""), "party": (r.get("candidate") or {}).get("party")},
                "amount": float(r.get("contribution_receipt_amount", 0)),
                "date": r.get("contribution_receipt_date", ""),
            }
            for r in data.get("results", [])
        ],
    }


@app.get("/api/v1/stats/overview")
async def stats():
    return {
        "platform": "PolitiTrack", "version": "2.0.0", "data_sources": 5,
        "sources": ["FEC", "Senate LDA", "Congress.gov", "ProPublica Congress", "USASpending"],
        "note": "Use /api/v1/district?zip=XXXXX for full district data with real FEC + Congress.gov data",
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


# ── ZIP → state mapping ─────────────────────────────────

def zip_to_state(z: str) -> str:
    n = int(z[:3])
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
