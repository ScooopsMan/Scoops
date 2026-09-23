#!/usr/bin/env python3
"""
Scoops Event Radar morning source watcher.

The job discovers candidate links and source changes. It intentionally does NOT
auto-promote a scraped page into the verified opportunity database. A discovery
is a lead to review, not proof of a vendor opening.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SOURCES_PATH = ROOT / "data" / "sources.json"
DISCOVERIES_PATH = ROOT / "data" / "discoveries.json"
EVENTS_PATH = ROOT / "data" / "events.json"

WEIGHTS = {
    "food truck": 7,
    "foodtruck": 7,
    "vendor application": 7,
    "vendor": 5,
    "food vendor": 6,
    "food & beverage": 6,
    "food and beverage": 6,
    "application": 4,
    "festival": 3,
    "market": 3,
    "fair": 3,
    "harvest": 3,
    "rodeo": 3,
    "tournament": 3,
    "fundraiser": 2,
    "community": 2,
    "holiday": 2,
    "school": 2,
    "sports": 2,
    "race": 2,
    "arts": 2,
    "craft": 2,
    "family": 1,
    "truck": 2,
    "touch-a-truck": 4,
}
NEGATIVE = {
    "privacy", "terms", "login", "sign in", "facebook", "instagram", "youtube",
    "linkedin", "weather", "employment", "careers", "minutes", "agenda", "meeting",
    "grant", "procurement", "contractor", "solicitation", "request for bid", "request for proposal"
}
GENERIC_LABELS = {
    "events", "calendar", "read more", "learn more", "more details", "view all",
    "register", "home", "contact", "contact us", "about us", "food trucks",
    "vendors and contractors", "sign up", "explore new applications", "start application",
    "back to school", "join the chamber", "your applications",
    "artist, vendor & exhibitor management", "artists, vendors & exhibitors",
    "your sponsorships"
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ScoopsEventRadar/2.0; private business research)"
}

def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback

def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def clean_url(url: str) -> str:
    try:
        p = urlsplit(url)
        query = [
            (k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
        ]
        path = re.sub(r"/+$", "", p.path) or "/"
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, urlencode(query), ""))
    except Exception:
        return url

def score_candidate(label: str, href: str):
    normalized_label = " ".join(label.lower().split())
    hay = f"{normalized_label} {href.lower()}"
    if not normalized_label or normalized_label in GENERIC_LABELS:
        return 0, []
    if any(term in hay for term in NEGATIVE):
        return 0, []
    hits = sorted({term for term in WEIGHTS if term in hay})
    score = sum(WEIGHTS[h] for h in hits)
    # Prevent URL boilerplate from making an empty/generic anchor look useful.
    if len(normalized_label) < 5:
        score = max(0, score - 3)
    return score, hits

def known_urls(events):
    urls = set()
    for event in events:
        for key in ("sourceUrl", "applicationUrl"):
            if event.get(key):
                urls.add(clean_url(event[key]))
    return urls

def source_host(url: str) -> str:
    return urlsplit(url).netloc.lower().replace("www.", "")

def distance_band(miles):
    try:
        miles = float(miles)
    except (TypeError, ValueError):
        return "DISTANCE UNKNOWN"
    if miles <= 10:
        return "HOME TURF"
    if miles <= 20:
        return "NEARBY"
    if miles <= 40:
        return "REASONABLE DRIVE"
    return "STRETCH"

def travel_tags(miles):
    band = distance_band(miles)
    tags = [band]
    if band == "HOME TURF":
        tags.append("LOCAL / LOW TRAVEL COST")
    elif band == "STRETCH":
        tags.append("LONG DRIVE")
    return tags

def fetch_source(src):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    src["lastChecked"] = now
    try:
        response = requests.get(src["url"], headers=HEADERS, timeout=25, allow_redirects=True)
        src["lastHttpStatus"] = response.status_code
        if response.status_code >= 400:
            src["lastStatus"] = f"HTTP {response.status_code}"
            src["lastError"] = f"Request returned {response.status_code}"
            return src, []

        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        src["lastTitle"] = title[:180]
        src["lastStatus"] = "OK"
        src["lastError"] = ""

        base_host = source_host(response.url)
        found, seen = [], set()
        for anchor in soup.find_all("a", href=True):
            label = " ".join(anchor.get_text(" ", strip=True).split())
            href = urljoin(response.url, anchor.get("href", "").strip())
            if not href.startswith(("http://", "https://")):
                continue

            # The watcher is intentionally conservative. External ad/social links
            # do not become local event discoveries.
            if source_host(href) != base_host:
                continue

            url = clean_url(href)
            if url in seen or url == clean_url(response.url):
                continue

            score, hits = score_candidate(label, url)
            if score < 2:
                continue

            seen.add(url)
            found.append({
                "url": url,
                "title": label[:220] or url,
                "keywordHits": hits,
                "score": score,
                "signal": "high" if score >= 7 else ("medium" if score >= 4 else "low"),
            })

        found.sort(key=lambda item: (-item["score"], item["title"].lower()))
        src["lastCandidateCount"] = len(found)
        src["pageFingerprint"] = hashlib.sha1(response.content).hexdigest()[:16]
        return src, found[:150]
    except Exception as exc:
        src["lastStatus"] = "ERROR"
        src["lastError"] = str(exc)[:240]
        return src, []

def main():
    sources = load_json(SOURCES_PATH, [])
    discoveries = load_json(DISCOVERIES_PATH, [])
    events = load_json(EVENTS_PATH, [])
    verified_urls = known_urls(events)

    # Re-score old discoveries using the current rules so previous noisy links
    # do not live forever after the scoring model improves.
    existing = {}
    for item in discoveries:
        if not item.get("url"):
            continue
        score, hits = score_candidate(item.get("title", ""), item["url"])
        if score < 2:
            continue
        item["score"] = score
        item["signal"] = "high" if score >= 7 else ("medium" if score >= 4 else "low")
        item["keywordHits"] = hits
        existing[clean_url(item["url"])] = item

    today = datetime.now(timezone.utc).date().isoformat()
    updated_sources = []

    for src in sources:
        updated, candidates = fetch_source(dict(src))
        updated_sources.append(updated)

        for candidate in candidates:
            url = clean_url(candidate["url"])
            if url in verified_urls:
                continue

            if url in existing:
                item = existing[url]
                item["lastSeen"] = today
                item["keywordHits"] = sorted(set(item.get("keywordHits", [])) | set(candidate["keywordHits"]))
                item["score"] = max(int(item.get("score", 0)), candidate["score"])
                item["signal"] = "high" if item["score"] >= 7 else ("medium" if item["score"] >= 4 else "low")
                item["priority"] = src.get("priority", item.get("priority"))
                item["approxMiles"] = src.get("approxMiles", item.get("approxMiles"))
                item["distanceTag"] = src.get("distanceTag") or distance_band(item.get("approxMiles"))
                item["profitPotential"] = item.get("profitPotential") or "UNKNOWN"
                item["profitTags"] = item.get("profitTags") or travel_tags(item.get("approxMiles"))
                continue

            fingerprint = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
            existing[url] = {
                "id": f"discovery-{fingerprint}",
                "sourceId": src.get("id"),
                "sourceName": src.get("name"),
                "sourceKind": src.get("kind"),
                "sourceTier": src.get("tier"),
                "county": src.get("county"),
                "priority": src.get("priority"),
                "approxMiles": src.get("approxMiles"),
                "distanceTag": src.get("distanceTag") or distance_band(src.get("approxMiles")),
                "profitPotential": "UNKNOWN",
                "profitTags": travel_tags(src.get("approxMiles")),
                "title": candidate["title"],
                "url": url,
                "keywordHits": candidate["keywordHits"],
                "score": candidate["score"],
                "signal": candidate["signal"],
                "firstSeen": today,
                "lastSeen": today,
                "needsReview": True,
            }

    # Highest-signal and newest discoveries remain in the file. Keep enough
    # history to recognize reappearing links without turning this into a junk drawer.
    ordered = sorted(
        existing.values(),
        key=lambda item: (
            item.get("lastSeen", ""),
            int(item.get("score", 0)),
            item.get("title", ""),
        ),
    )[-400:]

    save_json(SOURCES_PATH, updated_sources)
    save_json(DISCOVERIES_PATH, ordered)

    ok = sum(1 for s in updated_sources if s.get("lastStatus") == "OK")
    high = sum(1 for d in ordered if d.get("signal") == "high")
    print(f"Checked {len(updated_sources)} sources: {ok} OK. Discovery inbox: {len(ordered)} links ({high} high signal).")

# Batch 2 watcher — tuned discovery noise + rescoring
if __name__ == "__main__":
    main()