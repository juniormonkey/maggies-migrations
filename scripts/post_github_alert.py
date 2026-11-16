"""Open a GitHub Issue notification when sync_discord.py writes an alert payload."""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests


def main() -> None:
    alert_path = Path(os.environ.get("ALERT_JSON_PATH", "/tmp/maggie_alert.json"))
    if not alert_path.exists():
        print("No alert needed.")
        return

    payload = json.loads(alert_path.read_text(encoding="utf-8"))
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ.get("GITHUB_REPOSITORY", "juniormonkey/maggies-migrations")

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "title": payload["title"],
            "body": payload["body"],
            "assignees": payload.get("assignees", []),
        },
        timeout=30,
    )
    resp.raise_for_status()
    issue = resp.json()
    print(f"Created notification issue: {issue['html_url']}")


if __name__ == "__main__":
    main()
