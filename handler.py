"""
Stack Exchange Tool Handler — Q&A search across 170+ Stack Exchange sites.

Uses the Stack Exchange API v2.3 (no key required for 300 req/day).
Responses are gzip-compressed by default; requests handles decompression.
Returns questions with top answer excerpts, scores, and tags.
"""

import html
import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

_API_BASE = "https://api.stackexchange.com/2.3"
_HEADERS = {"Accept-Encoding": "gzip", "User-Agent": "Chalie/1.0"}

_SORT_MAP = {
    "relevance": "relevance",
    "votes": "votes",
    "activity": "activity",
    "creation": "creation",
}


def execute(topic: str, params: dict, config: dict = None, telemetry: dict = None) -> dict:
    """
    Search Stack Exchange sites and return questions with answer excerpts.

    Args:
        topic: Conversation topic (unused directly)
        params: {
            "query": str (required),
            "site": str (optional, default "stackoverflow"),
            "limit": int (optional, default 5, clamped 1-8),
            "sort": str (optional: relevance/votes/activity/creation),
            "has_accepted": bool (optional)
        }
        config: Tool config (unused — no API key needed)
        telemetry: Client telemetry (unused)

    Returns:
        {
            "results": [{"title", "url", "score", "answer_count",
                         "accepted_answer_id", "tags", "top_answer_body",
                         "view_count", "creation_date"}],
            "count": int,
            "_meta": {observability fields}
        }
    """
    query = (params.get("query") or "").strip()
    if not query:
        return {"results": [], "count": 0, "_meta": {}}

    site = (params.get("site") or "stackoverflow").strip().lower()
    limit = max(1, min(8, int(params.get("limit") or 5)))
    sort = _SORT_MAP.get((params.get("sort") or "relevance").strip().lower(), "relevance")
    has_accepted = params.get("has_accepted")

    t0 = time.time()
    results, quota_remaining, error = _search_questions(query, site, limit, sort, has_accepted)
    fetch_latency_ms = int((time.time() - t0) * 1000)

    if error and not results:
        logger.error(
            '{"event":"se_fetch_error","query":"%s","site":"%s","error":"%s","latency_ms":%d}',
            query, site, str(error)[:120], fetch_latency_ms,
        )
        return {"results": [], "count": 0, "error": str(error)[:200], "_meta": {}}

    logger.info(
        '{"event":"se_search_ok","query":"%s","site":"%s","count":%d,"quota_remaining":%s,"latency_ms":%d}',
        query, site, len(results), quota_remaining, fetch_latency_ms,
    )

    return {
        "results": results,
        "count": len(results),
        "_meta": {
            "fetch_latency_ms": fetch_latency_ms,
            "site": site,
            "quota_remaining": quota_remaining,
            "has_accepted_filter": bool(has_accepted),
            "result_count": len(results),
        },
    }


# ── API fetch ─────────────────────────────────────────────────────────────────

def _search_questions(query: str, site: str, limit: int, sort: str, has_accepted) -> tuple:
    """
    Call SE API search/advanced and return (results, quota_remaining, error).

    Filter 'withbody' returns the top answer body inline, avoiding a second
    API call per question. The body is HTML — we strip tags before returning.
    """
    api_params = {
        "q": query,
        "site": site,
        "pagesize": limit,
        "sort": sort,
        "order": "desc",
        "filter": "withbody",
    }
    if has_accepted:
        api_params["accepted"] = "true"

    try:
        resp = requests.get(
            f"{_API_BASE}/search/advanced",
            params=api_params,
            headers=_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return [], None, e

    # SE returns error_id + error_message on API-level errors
    if "error_id" in data:
        msg = f"SE API error {data['error_id']}: {data.get('error_message', 'unknown')}"
        return [], None, Exception(msg)

    quota_remaining = data.get("quota_remaining")
    items = data.get("items", [])

    results = []
    seen_ids = set()
    for item in items:
        qid = item.get("question_id")
        if qid in seen_ids:
            continue
        seen_ids.add(qid)

        # Top answer body is in the question item when filter=withbody
        body_raw = item.get("body", "")
        top_answer_body = _strip_html(body_raw)[:500] if body_raw else ""

        results.append({
            "title": html.unescape(item.get("title", "")),
            "url": item.get("link", ""),
            "score": item.get("score", 0),
            "answer_count": item.get("answer_count", 0),
            "accepted_answer_id": item.get("accepted_answer_id"),
            "tags": item.get("tags", []),
            "top_answer_body": top_answer_body,
            "view_count": item.get("view_count", 0),
            "creation_date": item.get("creation_date"),
        })

    return results, quota_remaining, None


# ── Utilities ─────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities from SE answer bodies."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s{2,}", " ", text.strip())
    return text
