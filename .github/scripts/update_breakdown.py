"""Update the README development breakdown using WakaTime or a GitHub-native fallback."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

BAR_WIDTH = 25
TOP_N = 5
LOOKBACK_DAYS = 180
START_MARKER = "<!--START_SECTION:waka-->"
END_MARKER = "<!--END_SECTION:waka-->"


def format_date(dt: datetime) -> str:
    """Format a datetime as 'D Month YYYY' matching hunkim98's breakdown header."""
    return f"{dt.day} {dt:%B %Y}"


def render_bar(percent: float, width: int = BAR_WIDTH) -> str:
    """Render a 25-char progress bar using quarter-block characters."""
    clamped = max(0.0, min(100.0, percent))
    filled = clamped / 100.0 * width
    full = int(filled)
    frac = filled - full
    partial = ""
    if full < width:
        if frac >= 0.875:
            full += 1
        elif frac >= 0.625:
            partial = "▓"
        elif frac >= 0.375:
            partial = "▒"
        elif frac >= 0.125:
            partial = "░"
    empty = max(0, width - full - len(partial))
    return ("█" * full) + partial + ("░" * empty)


def render_breakdown(title: str, items: list[tuple[str, float]]) -> str:
    """Render the ASCII language breakdown block."""
    lines = [title, ""]
    for name, percent in items[:TOP_N]:
        bar = render_bar(percent)
        lines.append(f"{name:<36}{bar}   {percent:05.2f} %")
    return "```txt\n" + "\n".join(lines) + "\n```"


def request_json(url: str, headers: dict[str, str], timeout: float = 15.0) -> tuple[int, dict | list]:
    """Fetch JSON from a URL and return (status_code, parsed_payload)."""
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def fetch_wakatime_breakdown(api_key: str) -> str | None:
    """Try fetching last-7-days language stats from WakaTime; return None on failure."""
    if not api_key:
        return None
    token = base64.b64encode(api_key.encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {token}",
        "User-Agent": "rvira-profile-readme-updater",
    }
    try:
        status, payload = request_json(
            "https://wakatime.com/api/v1/users/current/stats/last_7_days",
            headers=headers,
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None

    if status != 200 or not isinstance(payload, dict):
        return None

    data = payload.get("data") or {}
    languages = [
        (entry["name"], float(entry["percent"]))
        for entry in data.get("languages") or []
        if entry.get("name") and float(entry.get("percent") or 0) > 0
    ]
    if not languages:
        return None

    start_raw = (data.get("start") or "")[:10]
    end_raw = (data.get("end") or "")[:10]
    if start_raw and end_raw:
        start_dt = datetime.strptime(start_raw, "%Y-%m-%d")
        end_dt = datetime.strptime(end_raw, "%Y-%m-%d")
        title = f"From: {format_date(start_dt)} - To: {format_date(end_dt)}"
    else:
        now = datetime.now(timezone.utc)
        title = f"From: {format_date(now - timedelta(days=7))} - To: {format_date(now)}"
    return render_breakdown(title, languages)


def fetch_github_breakdown(username: str, token: str | None) -> str:
    """Aggregate byte counts across recently active non-fork GitHub repositories."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "rvira-profile-readme-updater",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    _, repos = request_json(
        f"https://api.github.com/users/{username}/repos?per_page=100&type=owner&sort=pushed",
        headers=headers,
    )
    if not isinstance(repos, list):
        raise RuntimeError("Unexpected GitHub /repos response")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    own_repos = [
        repo
        for repo in repos
        if not repo.get("fork")
        and not repo.get("archived")
        and (repo.get("name") or "").lower() != username.lower()
    ]

    recent = [
        repo
        for repo in own_repos
        if repo.get("pushed_at")
        and datetime.strptime(repo["pushed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) >= cutoff
    ]
    selected = recent if len(recent) >= 3 else own_repos[:5]

    totals: dict[str, int] = {}
    for repo in selected:
        lang_url = repo.get("languages_url")
        if not lang_url:
            continue
        _, lang_bytes = request_json(lang_url, headers=headers)
        if isinstance(lang_bytes, dict):
            for lang, count in lang_bytes.items():
                totals[lang] = totals.get(lang, 0) + int(count)

    total_bytes = sum(totals.values())
    if total_bytes <= 0:
        raise RuntimeError("No language bytes returned from GitHub repositories")

    items = [
        (lang, (count / total_bytes) * 100.0)
        for lang, count in sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    title = f"From: {format_date(cutoff)} - To: {format_date(now)}"
    return render_breakdown(title, items)


def update_readme_text(readme_text: str, block: str) -> str:
    """Replace the delimited breakdown section in README.md."""
    pattern = re.compile(
        re.escape(START_MARKER) + r"[\s\S]*?" + re.escape(END_MARKER),
        re.MULTILINE,
    )
    if not pattern.search(readme_text):
        raise ValueError("Could not find Waka/breakdown markers in README.md")
    replacement = f"{START_MARKER}\n\n{block}\n\n{END_MARKER}"
    return pattern.sub(replacement, readme_text, count=1)


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    readme_path = repo_root / "README.md"
    username = os.environ.get("GITHUB_USERNAME", "rvira").strip() or "rvira"
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    waka_key = os.environ.get("WAKATIME_API_KEY", "").strip()

    block = fetch_wakatime_breakdown(waka_key)
    source = "WakaTime"
    if block is None:
        block = fetch_github_breakdown(username, gh_token)
        source = "GitHub"

    original = readme_path.read_text(encoding="utf-8")
    updated = update_readme_text(original, block)
    if updated != original:
        readme_path.write_text(updated, encoding="utf-8")
        print(f"Updated README.md from {source}.")
    else:
        print(f"README.md already up to date ({source}).")


if __name__ == "__main__":
    main()
