#!/usr/bin/env python3
"""Refresh the generated sections of README.md.

Sources, all strictly public:
  releases  GitHub Releases across repos owned by USER
  oss       merged PRs into repos USER does not own, via `is:public` search
  posts     the RSS feed at BLOG_FEED
  dl:<pkg>  last-30-day npm download counts, one block per package

Nothing here can read a private repo: the search query is pinned to
`is:public`, and the release scan skips forks and archived repos.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

USER = "damngamerz"
BLOG_FEED = "https://saurav.eu/feed.xml"
README = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")

MAX_RELEASES = 5
MAX_POSTS = 3
# npm packages to report download counts for, each rendered into its own
# `<!-- dl:<name> -->` block so the surrounding prose stays hand-written.
PACKAGES = ["@damngamerz/pi-otel", "pi-agentarium"]
# Below this, render nothing rather than publish a weak number. A package
# crossing the floor starts showing its count on its own.
MIN_DOWNLOADS = 100
# Hide the posts block entirely while the newest post is older than this.
POST_STALE_AFTER = timedelta(days=365)
# Repos below this PR count collapse into a single trailing line...
MIN_PRS = 2
# ...unless they are listed here.
ALWAYS_LIST = {"python/cpython"}

# One line of context for the work worth describing. Repos absent from this
# map still render, just with the count alone.
NOTES = {
    "coala/coala": "documentation extraction API (`DocBaseClass`, "
                   "`DocumentationComment`, padding and marker handling)",
    "coala/coala-bears": "`DocumentationStyleBear` and `DocGrammarBear`",
    "capacitor-community/camera-preview": "`toBack` on Android and iOS, "
                                          "landscape orientation, lateral "
                                          "inversion, permission handling",
    "gastromatic/pytest-docker-postgres": "`load_database` support and CI "
                                          "environment handling",
    "coala/projects": "Google Summer of Code project pages",
}


def get(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USER}-readme-bot",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def releases():
    repos = get(f"https://api.github.com/users/{USER}/repos"
                f"?per_page=100&type=owner")
    out = []
    for repo in repos:
        if repo["fork"] or repo["archived"]:
            continue
        for rel in get(f"https://api.github.com/repos/{repo['full_name']}"
                       f"/releases?per_page=5"):
            if rel["draft"] or not rel.get("published_at"):
                continue
            # Sort on created_at, not published_at: for a release cut later
            # from an older tag, published_at is when the release object was
            # made, while created_at stays the real tag date. They are
            # identical for releases cut at ship time.
            out.append((rel.get("created_at") or rel["published_at"],
                        repo["name"], rel["tag_name"], rel["html_url"]))
    out.sort(reverse=True)
    if not out:
        return ""
    lines = ["**Recent releases**", ""]
    for published, name, tag, url in out[:MAX_RELEASES]:
        lines.append(f"- [{name} {tag}]({url}) — {published[:10]}")
    return "\n".join(lines)


def open_source():
    """Merged PRs into repos USER does not own. `is:public` is load-bearing."""
    query = (f"author:{USER}+type:pr+is:merged+is:public+-user:{USER}")
    repos, page = {}, 1
    while page <= 5:
        data = get("https://api.github.com/search/issues"
                   f"?q={query}&per_page=100&page={page}")
        items = data.get("items", [])
        for pr in items:
            full = pr["repository_url"].split("/repos/", 1)[1]
            entry = repos.setdefault(
                full, {"count": 0, "latest": "", "url": "", "number": 0})
            entry["count"] += 1
            closed = pr.get("closed_at") or ""
            if closed > entry["latest"]:
                entry["latest"] = closed
                entry["url"] = pr["html_url"]
                entry["number"] = pr["number"]
        if len(items) < 100:
            break
        page += 1

    if not repos:
        return ""

    ranked = sorted(repos.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    listed = [(f, d) for f, d in ranked
              if d["count"] >= MIN_PRS or f in ALWAYS_LIST]
    rest = [f for f, d in ranked
            if d["count"] < MIN_PRS and f not in ALWAYS_LIST]

    lines = []
    for full, data in listed:
        count = data["count"]
        plural = "PR" if count == 1 else "PRs"
        line = f"- **[{full}](https://github.com/{full})** — {count} merged {plural}"
        if count == 1 and data["url"]:
            # A lone PR is more useful linked directly than via its repo.
            line += f" ([#{data['number']}]({data['url']}))"
        if full in NOTES:
            line += f" · {NOTES[full]}"
        lines.append(line)

    if rest:
        links = " · ".join(
            f"[{f}]({repos[f]['url'] or 'https://github.com/' + f})"
            for f in rest)
        lines.append("")
        lines.append(f"Single merged PRs to {links}.")

    return "\n".join(lines)


def posts():
    # The feed host rejects urllib's default user-agent, so send our own.
    req = urllib.request.Request(
        BLOG_FEED, headers={"User-Agent": f"{USER}-readme-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        feed = ET.fromstring(r.read())

    items = []
    for item in feed.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        raw = (item.findtext("pubDate") or "").strip()
        if not (title and link and raw):
            continue
        try:
            when = datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            continue
        items.append((when, title, link))

    if not items:
        return ""
    items.sort(reverse=True)
    # A feed whose newest entry is years old is not a writing habit. Render
    # nothing until there is something current to point at.
    if datetime.now(timezone.utc) - items[0][0] > POST_STALE_AFTER:
        return ""
    return "\n".join(f"- [{title}]({link}) — {when:%Y-%m-%d}"
                     for when, title, link in items[:MAX_POSTS])


def downloads(package):
    """Last-30-day npm downloads, rounded. Empty below MIN_DOWNLOADS."""
    quoted = urllib.parse.quote(package, safe="")
    req = urllib.request.Request(
        f"https://api.npmjs.org/downloads/point/last-month/{quoted}",
        headers={"User-Agent": f"{USER}-readme-bot"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            count = json.load(r).get("downloads", 0)
    except urllib.error.HTTPError as exc:
        # An unpublished or renamed package 404s; that is not a build failure.
        if exc.code == 404:
            return ""
        raise
    if count < MIN_DOWNLOADS:
        return ""
    # Round off the false precision — this is a signal, not a metric.
    return f"~{round(count, -1):,} downloads in the last 30 days."


def splice(text, name, body):
    start, end = f"<!-- {name} starts -->", f"<!-- {name} ends -->"
    pattern = re.compile(f"{re.escape(start)}.*?{re.escape(end)}", re.S)
    if not pattern.search(text):
        raise SystemExit(f"markers for '{name}' missing from README.md")
    filled = f"{start}\n\n{body}\n\n{end}" if body.strip() else f"{start}\n{end}"
    return pattern.sub(lambda _: filled, text)


def main():
    try:
        blocks = {"releases": releases(), "oss": open_source(), "posts": posts()}
        for package in PACKAGES:
            blocks[f"dl:{package}"] = downloads(package)
    except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError) as exc:
        # Leave README.md untouched rather than blanking a section on a blip.
        print(f"refresh failed, README left as-is: {exc}", file=sys.stderr)
        return 1

    with open(README, encoding="utf-8") as fh:
        text = original = fh.read()
    for name, body in blocks.items():
        text = splice(text, name, body)

    if text == original:
        print("no change")
        return 0
    with open(README, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("README.md updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
