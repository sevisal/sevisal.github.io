#!/usr/bin/env python3
"""Sync the Projects & Funding page from the public UC3M Research Portal."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin


DEFAULT_PROFILE_URL = "https://researchportal.uc3m.es/display/inv45809"
USER_AGENT = "sevisal.github.io UC3M project sync (https://sevisal.github.io)"


@dataclass
class Project:
    title: str
    funder: str
    start_year: int | None
    end_year: int | None
    role: str
    url: str

    @property
    def year_range(self) -> str:
        start = str(self.start_year) if self.start_year else "n.d."
        end = str(self.end_year) if self.end_year else "present"
        return f"{start}-{end}"

    def is_ongoing(self, year: int) -> bool:
        return self.end_year is None or self.end_year >= year


def fetch_html(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"UC3M request failed: {exc.code} {url}") from exc
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
        print(
            "Warning: UC3M certificate verification failed; retrying without certificate verification.",
            file=sys.stderr,
        )
        context = ssl._create_unverified_context()
        with urllib.request.urlopen(request, timeout=30, context=context) as response:
            body = response.read()

    decoded = body.decode("utf-8", errors="replace")
    if "\ufffd" in decoded:
        decoded = body.decode("latin-1", errors="replace")
    return decoded


def clean_text(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def project_group(html_text: str) -> str:
    match = re.search(
        r'<div[^>]+id="projectsGroup"[^>]*>(.*?)(?:<div[^>]+id="otherGroup"|<div[^>]+id="identityGroup")',
        html_text,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        raise RuntimeError("Could not find the Projects section in the UC3M profile.")
    return match.group(1)


def parse_years(text: str) -> tuple[int | None, int | None]:
    years = [int(year) for year in re.findall(r"\b(?:19|20)\d{2}\b", clean_text(text))]
    if not years:
        return None, None
    if len(years) == 1:
        return years[0], years[0]
    return years[0], years[-1]


def parse_projects(profile_url: str, html_text: str) -> list[Project]:
    group = project_group(html_text)
    projects: list[Project] = []
    role_labels = {
        "principal researcher on": "Principal investigator",
        "researcher on": "Researcher",
    }
    articles = re.findall(r'<article class="property"[^>]*>(.*?)</article>', group, re.DOTALL | re.IGNORECASE)
    for article in articles:
        heading_match = re.search(r"<h3[^>]*>(.*?)</h3>", article, re.DOTALL | re.IGNORECASE)
        if not heading_match:
            continue
        heading = clean_text(heading_match.group(1)).lower()
        role = role_labels.get(heading)
        if not role:
            continue

        items = re.findall(r"<li[^>]*>(.*?)</li>", article, re.DOTALL | re.IGNORECASE)
        for item in items:
            activity = re.search(
                r'<a href="([^"]+)"[^>]*title="activity name"[^>]*>(.*?)</a>',
                item,
                re.DOTALL | re.IGNORECASE,
            )
            funder = re.search(
                r'title="awarded by"[^>]*>(.*?)</a>',
                item,
                re.DOTALL | re.IGNORECASE,
            )
            dates = re.search(
                r'<span[^>]+class="listDateTime"[^>]*>(.*?)</span>',
                item,
                re.DOTALL | re.IGNORECASE,
            )
            if not activity:
                continue
            start_year, end_year = parse_years(dates.group(1) if dates else "")
            projects.append(
                Project(
                    title=clean_text(activity.group(2)),
                    funder=clean_text(funder.group(1)) if funder else "",
                    start_year=start_year,
                    end_year=end_year,
                    role=role,
                    url=urljoin(profile_url, activity.group(1)),
                )
            )
    return projects


def project_card(project: Project) -> str:
    title = html.escape(project.title)
    url = html.escape(project.url, quote=True)
    role = html.escape(project.role)
    funder = html.escape(project.funder) if project.funder else "n.d."
    duration = html.escape(project.year_range)
    return "\n".join(
        [
            '<div class="project-card">',
            f'  <h4><a href="{url}">{title}</a></h4>',
            "  <dl>",
            f"    <div><dt>Role</dt><dd>{role}</dd></div>",
            f"    <div><dt>Funder</dt><dd>{funder}</dd></div>",
            f"    <div><dt>Duration</dt><dd>{duration}</dd></div>",
            "  </dl>",
            "</div>",
        ]
    )


def write_projects(projects: list[Project], output: Path, year: int) -> None:
    ongoing = sorted(
        [project for project in projects if project.is_ongoing(year)],
        key=lambda project: (project.end_year or 9999, project.start_year or 0, project.title.lower()),
        reverse=True,
    )
    completed = sorted(
        [project for project in projects if not project.is_ongoing(year)],
        key=lambda project: (project.end_year or 0, project.start_year or 0, project.title.lower()),
        reverse=True,
    )

    lines: list[str] = [
        "---",
        'title: "Projects & Funding"',
        "type: page",
        "---",
        "",
        "### Ongoing",
        "",
    ]
    for project in ongoing:
        lines.extend([project_card(project), ""])
    lines.extend(["", "### Completed", ""])
    for project in completed:
        lines.extend([project_card(project), ""])
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the Hugo projects page from the UC3M Research Portal.")
    parser.add_argument("--profile-url", default=DEFAULT_PROFILE_URL)
    parser.add_argument("--output", default="content/projects/_index.md")
    parser.add_argument("--year", type=int, default=dt.date.today().year)
    args = parser.parse_args()

    projects = parse_projects(args.profile_url, fetch_html(args.profile_url))
    if not projects:
        raise RuntimeError("No projects were found in the UC3M profile.")
    write_projects(projects, Path(args.output), args.year)
    print(f"Synced {len(projects)} UC3M projects.")


if __name__ == "__main__":
    main()
