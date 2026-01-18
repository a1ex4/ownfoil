import logging
import re
from urllib.parse import urljoin

import requests

logger = logging.getLogger("downloads.prowlarr")


class ProwlarrClient:
    def __init__(self, base_url, api_key, timeout_seconds=15):
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def _headers(self):
        return {"X-Api-Key": self.api_key}

    def _get(self, path, params=None):
        url = urljoin(self.base_url, path.lstrip("/"))
        resp = requests.get(
            url,
            headers=self._headers(),
            params=params or {},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()

    def system_status(self):
        return self._get("/api/v1/system/status")

    def list_indexers(self):
        return self._get("/api/v1/indexer")

    def search(self, query, indexer_ids=None, limit=None):
        params = {"query": query}
        if indexer_ids:
            params["indexerIds"] = ",".join(str(i) for i in indexer_ids)
        results = self._get("/api/v1/search", params=params)
        normalized = [_normalize_result(item) for item in results or []]
        if limit:
            return normalized[:limit]
        return normalized


def _normalize_result(item):
    return {
        "title": item.get("title") or "",
        "size": int(item.get("size") or 0),
        "seeders": int(item.get("seeders") or 0),
        "leechers": int(item.get("leechers") or 0),
        "download_url": item.get("downloadUrl") or "",
        "info_url": item.get("infoUrl") or "",
        "indexer_id": item.get("indexerId"),
        "raw": item,
    }


def _normalize_text(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _has_version(text, version):
    if not version:
        return False
    version_str = str(version).lower()
    return version_str in text or f"v{version_str}" in text


def filter_results(results, min_seeders=0, required_terms=None, blacklist_terms=None):
    required_terms = [_normalize_text(t) for t in (required_terms or []) if t]
    blacklist_terms = [_normalize_text(t) for t in (blacklist_terms or []) if t]
    filtered = []
    for result in results:
        title = _normalize_text(result.get("title") or "")
        if min_seeders and result.get("seeders", 0) < min_seeders:
            continue
        if required_terms and not all(term in title for term in required_terms):
            continue
        if blacklist_terms and any(term in title for term in blacklist_terms):
            continue
        filtered.append(result)
    return filtered


def _score_result(result, title_id=None, version=None):
    title = _normalize_text(result.get("title") or "")
    seeders = result.get("seeders", 0)

    has_title_id = bool(title_id and title_id.lower() in title)
    has_version = _has_version(title, version)
    has_update = "update" in title
    has_nsp = "nsp" in title or "nsz" in title

    score = 0
    if has_title_id:
        score += 50
    if has_version:
        score += 30
    if has_update:
        score += 10
    if has_nsp:
        score += 5

    seed_bonus = min(max(seeders, 0), 200)
    score += seed_bonus / 10
    return score


def pick_best_result(results, title_id=None, version=None, min_seeders=0, required_terms=None, blacklist_terms=None):
    filtered = filter_results(
        results,
        min_seeders=min_seeders,
        required_terms=required_terms,
        blacklist_terms=blacklist_terms,
    )
    if not filtered:
        return None
    scored = [
        (result, _score_result(result, title_id=title_id, version=version))
        for result in filtered
    ]
    scored.sort(
        key=lambda item: (
            item[1],
            item[0].get("seeders", 0),
            -item[0].get("size", 0),
        ),
        reverse=True,
    )
    best = scored[0][0]
    logger.info("Selected prowlarr result: %s", best.get("title"))
    return best
