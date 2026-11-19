#!/usr/bin/env python3
"""Publish a Klubbdigest issue to the static site, and optionally email it.

Takes an issue as Markdown (with YAML frontmatter) or JSON, renders the
issue HTML, then rebuilds index.html, archive.html, 404.html and feed.xml.

Usage:
  python3 scripts/publish_issue.py issues/2026-11-18.md
  python3 scripts/publish_issue.py --date 2026-11-18
  python3 scripts/publish_issue.py path/to/draft.md --date 2026-11-18
  python3 scripts/publish_issue.py path/to/issue.json
  python3 scripts/publish_issue.py --rebuild
  python3 scripts/publish_issue.py issues/2026-11-18.md --no-commit --no-push
  python3 scripts/publish_issue.py issues/2026-11-18.md --send

--send posts the issue to the Buttondown API
(POST https://api.buttondown.com/v1/emails, Authorization: Token
$BUTTONDOWN_API_KEY, JSON fields subject, body, status=about_to_send).
It does not run unless you pass the flag and set BUTTONDOWN_API_KEY.

Replace BUTTONDOWN_USERNAME in _config.yml once the Buttondown account exists.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
ISSUES_DIR = REPO / "issues"
CONFIG_PATH = REPO / "_config.yml"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISSUE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.(md|json)$")

BUTTONDOWN_API = "https://api.buttondown.com/v1/emails"
FONTS = (
    "https://fonts.googleapis.com/css2?"
    "family=Syne:wght@600;700;800&family=Outfit:wght@300;400;500;600&display=swap"
)

# ---------------------------------------------------------------------------
# Config and frontmatter (no third-party YAML dependency)
# ---------------------------------------------------------------------------


def parse_simple_yaml(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip("'\"")
    return data


def load_config() -> dict[str, str]:
    if not CONFIG_PATH.is_file():
        sys.exit(f"Missing config: {CONFIG_PATH}")
    cfg = parse_simple_yaml(CONFIG_PATH.read_text(encoding="utf-8"))
    for required in ("site_name", "site_url", "buttondown_username"):
        if not cfg.get(required):
            sys.exit(f"_config.yml is missing {required}")
    cfg["site_url"] = cfg["site_url"].rstrip("/")
    cfg.setdefault("site_tagline", "Weekly club nights in Kristiansand and Oslo")
    cfg.setdefault(
        "description",
        "A weekly digest of dance and club nights in Kristiansand and Oslo.",
    )
    cfg.setdefault("github_repo", "reikaas/club-digest")
    cfg.setdefault("timezone", "Europe/Oslo")
    return cfg


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text.lstrip("\n")
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    return parse_simple_yaml(parts[1]), parts[2].lstrip("\n")


# ---------------------------------------------------------------------------
# Markdown (subset: headings, lists, hr, links, emphasis, nl2br)
# ---------------------------------------------------------------------------


def inline_md(text: str) -> str:
    placeholders: list[str] = []

    def stash(fragment: str) -> str:
        placeholders.append(fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    def code(m: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(m.group(1))}</code>")

    def link(m: re.Match[str]) -> str:
        label = _apply_emphasis(html.escape(m.group(1)))
        url = html.escape(m.group(2).strip(), quote=True)
        return stash(f'<a href="{url}">{label}</a>')

    text = re.sub(r"`([^`]+)`", code, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    text = _apply_emphasis(html.escape(text))
    text = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
    return text


def _apply_emphasis(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__(.+?)__", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<em>\1</em>", text)
    return text


def markdown_to_html(src: str) -> str:
    lines = src.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    def flush_para(buf: list[str]) -> None:
        if not buf:
            return
        inner = "<br>\n".join(inline_md(p) for p in buf)
        out.append(f"<p>{inner}</p>")

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{inline_md(heading.group(2))}</h{level}>")
            i += 1
            continue

        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            out.append("<hr>")
            i += 1
            continue

        if re.match(r"^[-*+]\s+", stripped) or re.match(r"^\d+\.\s+", stripped):
            ordered = bool(re.match(r"^\d+\.\s+", stripped))
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while i < n:
                s = lines[i].strip()
                if not s:
                    # Allow a blank line only if the next line continues the list.
                    if i + 1 < n and (
                        re.match(r"^[-*+]\s+", lines[i + 1].strip())
                        or re.match(r"^\d+\.\s+", lines[i + 1].strip())
                    ):
                        i += 1
                        continue
                    break
                m = re.match(r"^[-*+]\s+(.+)$", s) or re.match(r"^\d+\.\s+(.+)$", s)
                if not m:
                    break
                items.append(f"<li>{inline_md(m.group(1))}</li>")
                i += 1
            out.append(f"<{tag}>\n" + "\n".join(items) + f"\n</{tag}>")
            continue

        buf: list[str] = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,4})\s+", lines[i].strip()) and not re.match(
            r"^[-*+]\s+", lines[i].strip()
        ) and not re.match(r"^\d+\.\s+", lines[i].strip()) and not re.match(
            r"^(-{3,}|\*{3,}|_{3,})$", lines[i].strip()
        ):
            buf.append(lines[i].strip())
            i += 1
        flush_para(buf)

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Issue loading
# ---------------------------------------------------------------------------


def parse_date(value: str) -> str:
    value = value.strip()
    if not DATE_RE.match(value):
        sys.exit(f"Invalid date {value!r}; expected YYYY-MM-DD")
    dt.date.fromisoformat(value)  # raises if the calendar date is impossible
    return value


def load_issue_from_markdown(text: str, fallback_date: str | None = None) -> dict:
    meta, body = split_frontmatter(text)
    date = parse_date(fallback_date or meta.get("date") or "")
    title = meta.get("title") or f"Klubbdigest, {pretty_date(date)}"
    summary = meta.get("summary") or first_sentence(body)
    subject = meta.get("subject") or title
    return {
        "date": date,
        "title": title,
        "summary": summary,
        "subject": subject,
        "body": body.strip() + "\n",
    }


def load_issue_from_json(text: str, fallback_date: str | None = None) -> dict:
    raw = json.loads(text)
    if not isinstance(raw, dict):
        sys.exit("JSON issue must be an object")
    body = raw.get("body") or raw.get("body_markdown") or ""
    date = parse_date(str(fallback_date or raw.get("date") or ""))
    title = raw.get("title") or f"Klubbdigest, {pretty_date(date)}"
    summary = raw.get("summary") or first_sentence(body)
    subject = raw.get("subject") or title
    return {
        "date": date,
        "title": title,
        "summary": summary,
        "subject": subject,
        "body": str(body).strip() + "\n",
    }


def first_sentence(body: str) -> str:
    text = re.sub(r"^#+\s+", "", body.strip().split("\n", 1)[0]).strip()
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return (text[:240] + "…") if len(text) > 240 else text


def issue_to_markdown(issue: dict) -> str:
    return (
        "---\n"
        f'title: "{issue["title"]}"\n'
        f'date: {issue["date"]}\n'
        f'summary: "{issue["summary"]}"\n'
        f'subject: "{issue["subject"]}"\n'
        "---\n\n"
        f'{issue["body"].rstrip()}\n'
    )


def load_all_issues() -> list[dict]:
    issues: list[dict] = []
    if not ISSUES_DIR.is_dir():
        return issues
    for path in sorted(ISSUES_DIR.iterdir()):
        m = ISSUE_FILE_RE.match(path.name)
        if not m or path.suffix != ".md":
            continue
        issues.append(load_issue_from_markdown(path.read_text(encoding="utf-8"), m.group(1)))
    issues.sort(key=lambda i: i["date"], reverse=True)
    return issues


def pretty_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B')} {d.year}"


def rfc822(iso: str, timezone: str = "Europe/Oslo") -> str:
    d = dt.date.fromisoformat(iso)
    # The issue date is a calendar date, not a timestamp; noon local is a stable choice.
    stamp = dt.datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=ZoneInfo(timezone))
    return stamp.strftime("%a, %d %b %Y %H:%M:%S %z")


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------


def rel(depth: int, path: str) -> str:
    return path if depth == 0 else ("../" * depth) + path


def subscribe_block(cfg: dict, depth: int) -> str:
    action = (
        "https://buttondown.com/api/emails/embed-subscribe/"
        + cfg["buttondown_username"]
    )
    return f"""
<section class="subscribe" id="subscribe">
  <h2>Get the Wednesday email</h2>
  <p>A short digest of dance and club nights in Kristiansand and Oslo, once a week.</p>
  <form class="subscribe-form embeddable-buttondown-form" action="{html.escape(action, quote=True)}" method="post">
    <input type="email" name="email" placeholder="you@example.com" required autocomplete="email">
    <button type="submit">Subscribe</button>
  </form>
  <p class="fineprint">Every email includes a one-click unsubscribe link.</p>
</section>
""".strip()


def layout(
    cfg: dict,
    *,
    title: str,
    depth: int,
    content: str,
    description: str | None = None,
    canonical: str | None = None,
    current: str | None = None,
) -> str:
    site = cfg["site_name"]
    desc = html.escape(description or cfg["description"])
    canon = html.escape(canonical or cfg["site_url"], quote=True)
    home = rel(depth, "index.html")
    archive = rel(depth, "archive.html")
    feed = rel(depth, "feed.xml")
    css = rel(depth, "assets/style.css")
    icon = rel(depth, "assets/favicon.svg")
    nav_latest = ' aria-current="page"' if current == "latest" else ""
    nav_archive = ' aria-current="page"' if current == "archive" else ""
    page_title = site if title == site else f"{title} · {site}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <meta name="description" content="{desc}">
  <link rel="canonical" href="{canon}">
  <link rel="icon" href="{icon}" type="image/svg+xml">
  <link rel="alternate" type="application/rss+xml" title="{html.escape(site)}" href="{feed}">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="{FONTS}">
  <link rel="stylesheet" href="{css}">
</head>
<body>
  <header class="site-header wrap">
    <a class="brand" href="{home}">
      <span class="brand-mark">Klubb<span>digest</span></span>
      <span class="brand-sub">Kristiansand &amp; Oslo</span>
    </a>
    <nav class="nav" aria-label="Primary">
      <a href="{home}"{nav_latest}>Latest</a>
      <a href="{archive}"{nav_archive}>Archive</a>
      <a href="{feed}">RSS</a>
    </nav>
  </header>
  <main class="wrap">
{content}
  </main>
  <footer class="site-footer wrap">
    <p>{html.escape(site)} is a free weekly look at dance and club nights worth showing up for.</p>
    <p>Every email includes a one-click unsubscribe link. Source on <a href="https://github.com/{html.escape(cfg['github_repo'])}">GitHub</a>.</p>
  </footer>
</body>
</html>
"""


def render_issue_page(cfg: dict, issue: dict, issues: list[dict], *, include_subscribe: bool) -> str:
    dates = [i["date"] for i in issues]
    idx = dates.index(issue["date"])
    newer = issues[idx - 1] if idx > 0 else None
    older = issues[idx + 1] if idx + 1 < len(issues) else None
    # Issue pages live in /issues/, so sibling links are bare filenames;
    # index/archive are one level up.
    nav_bits = []
    if older:
        nav_bits.append(
            f'<a href="{older["date"]}.html">← {html.escape(older["title"])}</a>'
        )
    else:
        nav_bits.append("<span></span>")
    if newer:
        nav_bits.append(
            f'<a href="{newer["date"]}.html">{html.escape(newer["title"])} →</a>'
        )
    nav_html = f'<nav class="issue-nav" aria-label="Issues">{" ".join(nav_bits)}</nav>'
    body = markdown_to_html(issue["body"])
    sub = (subscribe_block(cfg, 1) + "\n") if include_subscribe else ""
    content = f"""    {sub}<article class="content">
      <p class="kicker">Issue · {html.escape(pretty_date(issue["date"]))}</p>
      <h1>{html.escape(issue["title"])}</h1>
      {body}
    </article>
    {nav_html}"""
    return layout(
        cfg,
        title=issue["title"],
        depth=1,
        content=content,
        description=issue["summary"],
        canonical=f'{cfg["site_url"]}/issues/{issue["date"]}.html',
    )


def render_index(cfg: dict, issues: list[dict]) -> str:
    if not issues:
        content = subscribe_block(cfg, 0) + "\n    <p>No issues yet.</p>"
        return layout(cfg, title=cfg["site_name"], depth=0, content=content, current="latest")
    latest = issues[0]
    body = markdown_to_html(latest["body"])
    content = f"""    {subscribe_block(cfg, 0)}
    <article class="content">
      <p class="kicker">Latest issue · {html.escape(pretty_date(latest["date"]))}</p>
      <h1>{html.escape(latest["title"])}</h1>
      {body}
    </article>
    <p class="fineprint"><a href="issues/{latest["date"]}.html">Permalink for this issue</a> · <a href="archive.html">All issues</a></p>"""
    return layout(
        cfg,
        title=cfg["site_name"],
        depth=0,
        content=content,
        description=latest["summary"],
        canonical=cfg["site_url"] + "/",
        current="latest",
    )


def render_archive(cfg: dict, issues: list[dict]) -> str:
    items = []
    for issue in issues:
        items.append(
            "<li>"
            f'<a href="issues/{issue["date"]}.html">'
            f'<span class="archive-date">{html.escape(pretty_date(issue["date"]))}</span>'
            f'<span class="archive-title">{html.escape(issue["title"])}</span>'
            f'<span class="archive-summary">{html.escape(issue["summary"])}</span>'
            "</a></li>"
        )
    listing = "\n      ".join(items) if items else "<li>No issues yet.</li>"
    content = f"""    {subscribe_block(cfg, 0)}
    <p class="kicker">Archive</p>
    <h1>All issues</h1>
    <ul class="archive-list">
      {listing}
    </ul>"""
    return layout(
        cfg,
        title="Archive",
        depth=0,
        content=content,
        description="Archive of Klubbdigest weekly club-event issues for Kristiansand and Oslo.",
        canonical=cfg["site_url"] + "/archive.html",
        current="archive",
    )


def render_404(cfg: dict) -> str:
    content = f"""    <p class="kicker">404</p>
    <h1>Nothing on this floor</h1>
    <p>That page is missing. Head back to the <a href="{rel(0, "index.html")}">latest issue</a> or the <a href="{rel(0, "archive.html")}">archive</a>.</p>"""
    return layout(
        cfg,
        title="Page not found",
        depth=0,
        content=content,
        canonical=cfg["site_url"] + "/404.html",
    )


def render_feed(cfg: dict, issues: list[dict]) -> str:
    items = []
    for issue in issues:
        link = f'{cfg["site_url"]}/issues/{issue["date"]}.html'
        description = markdown_to_html(issue["body"])
        items.append(
            "    <item>\n"
            f"      <title>{html.escape(issue['title'])}</title>\n"
            f"      <link>{html.escape(link)}</link>\n"
            f"      <guid isPermaLink=\"true\">{html.escape(link)}</guid>\n"
            f"      <pubDate>{rfc822(issue['date'])}</pubDate>\n"
            f"      <description><![CDATA[{description}]]></description>\n"
            "    </item>"
        )
    built = dt.datetime.now(ZoneInfo(cfg.get("timezone", "Europe/Oslo"))).strftime(
        "%a, %d %b %Y %H:%M:%S %z"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">\n'
        "  <channel>\n"
        f"    <title>{html.escape(cfg['site_name'])}</title>\n"
        f"    <link>{html.escape(cfg['site_url'])}/</link>\n"
        f"    <description>{html.escape(cfg['description'])}</description>\n"
        "    <language>en</language>\n"
        f"    <lastBuildDate>{built}</lastBuildDate>\n"
        f'    <atom:link href="{html.escape(cfg["site_url"])}/feed.xml" rel="self" type="application/rss+xml"/>\n'
        + ("\n".join(items) + "\n" if items else "")
        + "  </channel>\n"
        "</rss>\n"
    )


# ---------------------------------------------------------------------------
# Write, git, Buttondown
# ---------------------------------------------------------------------------


def write_site(cfg: dict, issues: list[dict]) -> list[Path]:
    written: list[Path] = []
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    (REPO / "assets").mkdir(parents=True, exist_ok=True)

    def put(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        written.append(path)

    put(REPO / "index.html", render_index(cfg, issues))
    put(REPO / "archive.html", render_archive(cfg, issues))
    put(REPO / "404.html", render_404(cfg))
    put(REPO / "feed.xml", render_feed(cfg, issues))
    existing_html = {p for p in ISSUES_DIR.glob("*.html")}
    keep: set[Path] = set()
    for issue in issues:
        path = ISSUES_DIR / f"{issue['date']}.html"
        put(path, render_issue_page(cfg, issue, issues, include_subscribe=True))
        keep.add(path)
    for stale in existing_html - keep:
        stale.unlink()
        print(f"Removed stale {stale.relative_to(REPO)}")
    return written


def git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        check=check,
        text=True,
        capture_output=True,
    )


def commit_and_push(paths: list[Path], message: str, do_commit: bool, do_push: bool) -> None:
    if not do_commit:
        print("Skipping commit (--no-commit).")
        return
    rels = [str(p.relative_to(REPO)) for p in paths]
    # Also stage the source markdown in issues/.
    rels.append("issues")
    git(["add", *rels, "index.html", "archive.html", "404.html", "feed.xml"])
    status = git(["status", "--porcelain"])
    if not status.stdout.strip():
        print("Nothing to commit.")
        return
    git(["commit", "-m", message])
    print(f"Committed: {message}")
    if not do_push:
        print("Skipping push (--no-push).")
        return
    pull = git(["pull", "--rebase", "origin", "main"], check=False)
    if pull.returncode != 0:
        sys.exit(f"git pull --rebase failed:\n{pull.stdout}\n{pull.stderr}")
    push = git(["push", "origin", "main"], check=False)
    if push.returncode != 0:
        sys.exit(f"git push failed:\n{push.stdout}\n{push.stderr}")
    print("Pushed to origin/main.")


def send_buttondown(cfg: dict, issue: dict) -> None:
    api_key = os.environ.get("BUTTONDOWN_API_KEY", "").strip()
    if not api_key:
        sys.exit(
            "--send requires the BUTTONDOWN_API_KEY environment variable. "
            "Nothing was sent."
        )
    web = f'{cfg["site_url"]}/issues/{issue["date"]}.html'
    body = (
        f"Read this issue on the web: {web}\n\n"
        f"{issue['body'].strip()}\n"
    )
    payload = json.dumps(
        {
            "subject": issue["subject"],
            "body": body,
            "status": "about_to_send",
            "canonical_url": web,
        }
    ).encode("utf-8")

    def request(extra_headers: dict[str, str] | None = None) -> tuple[int, str]:
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(BUTTONDOWN_API, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    status, text = request()
    # API version 2026-04-01 requires a one-time confirmation header
    # (X-Buttondown-Live-Dangerously: true) on the first about_to_send
    # for a given API key. Retry once if we hit that gate.
    if status == 400 and "sending_requires_confirmation" in text:
        print("Buttondown asked for first-send confirmation; retrying with the confirmation header.")
        status, text = request({"X-Buttondown-Live-Dangerously": "true"})
    if status not in (200, 201):
        sys.exit(f"Buttondown API error ({status}): {text}")
    print(f"Buttondown accepted the email ({status}).")
    try:
        data = json.loads(text)
        url = data.get("absolute_url") or data.get("id")
        if url:
            print(url)
    except json.JSONDecodeError:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_issue(args: argparse.Namespace) -> dict | None:
    if args.rebuild and not args.source:
        return None
    source = Path(args.source).resolve() if args.source else None
    fallback_date = parse_date(args.date) if args.date else None

    if source is None:
        if not fallback_date:
            sys.exit("Pass an issue file, --date, or --rebuild.")
        md = ISSUES_DIR / f"{fallback_date}.md"
        js = ISSUES_DIR / f"{fallback_date}.json"
        if md.is_file():
            source = md
        elif js.is_file():
            source = js
        else:
            sys.exit(f"No issue file found for {fallback_date} in {ISSUES_DIR}")

    if not source.is_file():
        sys.exit(f"File not found: {source}")

    text = source.read_text(encoding="utf-8")
    if source.suffix.lower() == ".json":
        issue = load_issue_from_json(text, fallback_date)
    else:
        issue = load_issue_from_markdown(text, fallback_date or _date_from_name(source))

    dest = ISSUES_DIR / f"{issue['date']}.md"
    ISSUES_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(issue_to_markdown(issue), encoding="utf-8")
    if source != dest:
        print(f"Wrote {dest.relative_to(REPO)}")
    return issue


def _date_from_name(path: Path) -> str | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    return m.group(1) if m else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Render and publish a Klubbdigest issue.")
    parser.add_argument("source", nargs="?", help="Issue markdown or JSON file")
    parser.add_argument("--date", help="Issue date (YYYY-MM-DD). Overrides frontmatter if set.")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild index, archive, feed and issue HTML from issues/*.md",
    )
    parser.add_argument("--no-commit", action="store_true", help="Do not create a git commit")
    parser.add_argument("--no-push", action="store_true", help="Commit but do not push")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send the issue via the Buttondown API (requires BUTTONDOWN_API_KEY)",
    )
    parser.add_argument("-m", "--message", help="Commit message")
    args = parser.parse_args()

    cfg = load_config()
    issue = resolve_issue(args) if (args.source or args.date) else None
    if issue is None and not args.rebuild:
        sys.exit("Pass an issue file, --date, or --rebuild.")

    issues = load_all_issues()
    if not issues and not args.rebuild:
        sys.exit("No issues found after loading.")

    written = write_site(cfg, issues)
    print(f"Rendered {len(issues)} issue(s); wrote {len(written)} files.")

    if issue:
        message = args.message or f"Publish issue {issue['date']}"
        commit_and_push(written + [ISSUES_DIR / f"{issue['date']}.md"], message, not args.no_commit, not args.no_push)
        if args.send:
            send_buttondown(cfg, issue)
    else:
        message = args.message or "Rebuild Klubbdigest site"
        commit_and_push(written, message, not args.no_commit, not args.no_push)
        if args.send:
            sys.exit("--send needs an issue file or --date, not just --rebuild.")


if __name__ == "__main__":
    main()
