#!/usr/bin/env python3
"""HYDRA public paid-work monitor; reports candidate links, never applies or pays."""
import datetime as dt
import html
import xml.etree.ElementTree as ET
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
REPO = os.getenv("GITHUB_REPOSITORY", "")
TOKEN = os.getenv("GITHUB_TOKEN", "")
TITLE = "[HYDRA] Paid work lead watch"

def request(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "hydra-revenue-watch/1.0"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=20) as response:
        return json.load(response)

def search():
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).date().isoformat()
    queries = [
        f"is:issue is:open label:bounty updated:>={since}",
        f'is:issue is:open "bounty" created:>={since}',
        f'is:issue is:open "paid" "Shopify" created:>={since}',
    ]
    found = {}
    for query in queries:
        url = API + "/search/issues?" + urllib.parse.urlencode({"q": query, "sort": "updated", "order": "desc", "per_page": 30})
        try:
            for item in request(url).get("items", []):
                if item.get("pull_request"):
                    continue
                title = item.get("title", "")
                body = (item.get("body") or "")[:3000]
                if not re.search(r"\$\s*\d{1,5}|\b(?:usd|paid bounty|cash bounty|payment)\b", title + "\n" + body, re.I):
                    continue
                if re.search(r"\b(?:bounty watcher|bounty plaza|leaderboard|bounty close)\b", title, re.I):
                    continue
                if item.get("user", {}).get("type") == "Bot":
                    continue
                found[item["html_url"]] = {"title": title[:160], "url": item["html_url"], "source": "GitHub", "updated": item.get("updated_at", "")}
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print("Search failed:", query, str(exc), file=sys.stderr)
    return list(found.values())


KEYWORDS = re.compile(r"shopify|e.?commerce|product list|catalog|data entry|python|automation|web develop|website|content writ|technical writ|virtual assistant|quality assurance|\\bqa\\b|freelance|contract|research|spreadsheet|bounty|template|workflow|agent|skill", re.I)

def public_get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": "hydra-work-watch/1.1", "Accept": accept})
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read(1_500_000)
    return json.loads(raw) if accept == "application/json" else raw

def qualify(title, url, source, description="", updated=""):
    title = html.unescape(re.sub(r"<[^>]+>", " ", str(title or ""))).strip()[:150]
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname or not title:
        return None
    description = html.unescape(re.sub(r"<[^>]+>", " ", str(description or "")))
    if not KEYWORDS.search(title + " " + description[:1200]):
        return None
    return {"title": title, "url": url, "source": source, "updated": str(updated or "")[:40]}

def remotive():
    try:
        jobs = public_get("https://remotive.com/api/remote-jobs?limit=100").get("jobs", [])
        return [item for job in jobs if (item := qualify(job.get("title"), job.get("url"), "Remotive", str(job.get("category", "")) + " " + str(job.get("description", "")), job.get("publication_date")))]
    except Exception as exc:
        print("Remotive fetch failed:", str(exc), file=sys.stderr)
        return []

def rss(url, source):
    try:
        root = ET.fromstring(public_get(url, "application/rss+xml"))
        entries = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")
        results = []
        for entry in entries:
            def field(*names):
                for name in names:
                    node = entry.find(name)
                    if node is not None:
                        return node.get("href") or node.text or ""
                return ""
            item = qualify(field("title", "{http://www.w3.org/2005/Atom}title"), field("link", "{http://www.w3.org/2005/Atom}link"), source, field("description", "{http://www.w3.org/2005/Atom}summary"), field("pubDate", "{http://www.w3.org/2005/Atom}updated"))
            if item:
                results.append(item)
        return results
    except Exception as exc:
        print(f"RSS fetch failed for {source}:", str(exc), file=sys.stderr)
        return []

def gather():
    sources = [
        ("GitHub", search),
        ("Remotive", remotive),
        ("We Work Remotely", lambda: rss("https://weworkremotely.com/remote-jobs.rss", "We Work Remotely")),
        ("Remote OK", lambda: rss("https://remoteok.com/remote-jobs.rss", "Remote OK"))
    ]
    for url in os.getenv("HYDRA_APPROVED_RSS_FEEDS", "").split(","):
        if url.strip().startswith("https://"):
            sources.append(("Approved RSS", lambda u=url.strip(): rss(u, "Approved RSS")))
    leads, health = {}, {}
    for name, fn in sources:
        try:
            items = fn()
            health[name] = {"status": "ok", "matches": len(items)}
            for item in items:
                leads[item["url"]] = item
        except (urllib.error.URLError, TimeoutError, ValueError, ET.ParseError) as exc:
            health[name] = {"status": "error", "detail": str(exc)[:140]}
    return list(leads.values()), health

def main():
    leads, health = gather()
    print(json.dumps({"source_health": health, "candidate_count": len(leads), "candidates": leads[:10]}, indent=2))
    if not (REPO and TOKEN):
        print("Read-only mode: no repository token.")
        return
    url = f"{API}/repos/{REPO}/issues"
    try:
        issues = request(url + "?state=open&per_page=100")
    except Exception as exc:
        print("Failed to fetch repository issues:", str(exc), file=sys.stderr)
        return
    
    issue = next((x for x in issues if x.get("title") == TITLE and not x.get("pull_request")), None)
    
    # Extract seen URLs flexibly from any seen comment format in body or comments
    existing_body = (issue or {}).get("body") or ""
    seen = set(re.findall(r"<!-- seen:(https?://[^ ]+) -->", existing_body))
    
    fresh = [x for x in leads if x["url"] not in seen][:5]
    if not fresh:
        print("No new candidates.")
        return
        
    lines = [f'- **{x.get("source", "GitHub")}:** [{re.sub(r"[][\\\\]", " ", x["title"])}]({x["url"]})' for x in fresh]
    note = "\n\n**Unverified leads, not contracts.** Confirm listing, payment, eligibility, and AI-use rules. No proposal was submitted."
    markers = "\n".join(f'<!-- seen:{x["url"]} -->' for x in fresh)
    
    if issue:
        updated_body = (existing_body.strip() + "\n" + "\n".join(lines) + "\n" + markers)[-55000:]
        try:
            request(issue["url"], "PATCH", {"body": updated_body})
            request(issue["comments_url"], "POST", {"body": "New paid-work candidates:\n\n" + "\n".join(lines) + note})
            print("Updated", issue["html_url"])
        except Exception as exc:
            print("Failed to update issue:", str(exc), file=sys.stderr)
    else:
        body = "HYDRA lead watch. Public candidate links only; no automatic proposals, purchases, or contracts.\n\n" + "\n".join(lines) + note + "\n" + markers
        try:
            created = request(url, "POST", {"title": TITLE, "body": body})
            print("Created", created["html_url"])
        except Exception as exc:
            print("Failed to create issue:", str(exc), file=sys.stderr)

if __name__ == "__main__":
    main()
