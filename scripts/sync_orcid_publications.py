#!/usr/bin/env python3
"""Sync Hugo publication pages from ORCID, arXiv, and Crossref metadata."""

from __future__ import annotations

import argparse
import html
import json
import re
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import xml.etree.ElementTree as ET


DEFAULT_ORCID_ID = "0000-0002-5507-7537"
DEFAULT_AUTHOR_NAME = "Carlos Sevilla-Salcedo"
ORCID_API = "https://pub.orcid.org/v3.0"
CROSSREF_API = "https://api.crossref.org/works"
ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "sevisal.github.io ORCID/arXiv/Crossref publication sync (https://sevisal.github.io)"

PUBLISHED_OVERRIDES = {
    "170593803": {
        "title": "Automated web-based typing of Clostridioides difficile ribotypes via MALDI-TOF MS",
        "year": "2025",
        "month": "07",
        "day": "17",
        "work_type": "journal-article",
        "journal": "BMC Bioinformatics",
        "doi": "10.1186/s12859-025-06200-6",
    },
    "205861114": {
        "title": "Impact of evaluation noise in the context of multi-objective Bayesian optimization with the intrinsic coregionalization model",
        "year": "2026",
        "month": "11",
        "day": "01",
        "work_type": "journal-article",
        "journal": "Neurocomputing",
        "doi": "10.1016/j.neucom.2026.134400",
    },
}

METADATA_OVERRIDES = {
    "171071327": {
        "title": "Automatic surveillance of Escherichia coli bacteriological strains within clinical settings",
    },
}

FILENAME_OVERRIDES = {
    "171071327": "auto-2024-automatic-surveillance-ofescherichia-colibacteriological-strains-within-clinical",
}


@dataclass
class Work:
    put_code: str
    title: str
    year: str
    month: str
    day: str
    work_type: str
    journal: str
    doi: str
    url: str
    authors: list[str]
    source: str = "orcid"

    @property
    def date(self) -> str:
        year = self.year or "1900"
        month = self.month or "01"
        day = self.day or "01"
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    @property
    def link(self) -> str:
        if self.doi:
            return f"https://doi.org/{self.doi}"
        return self.url or f"https://orcid.org/{DEFAULT_ORCID_ID}/work/{self.put_code}"


@dataclass
class ManualPublication:
    title: str
    state: str
    path: Path


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.orcid+json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ORCID request failed: {exc.code} {url}\n{body}") from exc


def fetch_crossref_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def value(node: dict | None) -> str:
    if isinstance(node, dict):
        return str(node.get("value") or "").strip()
    if isinstance(node, str):
        return node.strip()
    return ""


def publication_date(node: dict | None) -> tuple[str, str, str]:
    if not isinstance(node, dict):
        return "", "", ""
    return value(node.get("year")), value(node.get("month")), value(node.get("day"))


def external_id(work: dict, wanted: str) -> str:
    external_ids_node = work.get("external-ids") or {}
    external_ids = external_ids_node.get("external-id") or []
    for item in external_ids:
        kind = value(item.get("external-id-type")).lower()
        if kind == wanted.lower():
            return value(item.get("external-id-value"))
    return ""


def title_from(work: dict) -> str:
    title = work.get("title", {})
    return value(title.get("title")) or value(title.get("translated-title"))


def contributors_from(work: dict) -> list[str]:
    contributors_node = work.get("contributors") or {}
    contributors = contributors_node.get("contributor") or []
    names: list[str] = []
    for contributor in contributors:
        name = value(contributor.get("credit-name"))
        if name:
            names.append(name)
    return names


def normalize_title(title: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", title))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def title_key(title: str) -> str:
    normalized = normalize_title(title)
    return re.sub(r"\b(the|a|an|and|of|for|with|in|on|to|from|via)\b", "", normalized).replace(" ", "")


TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "applications",
    "approach",
    "based",
    "for",
    "from",
    "in",
    "of",
    "on",
    "study",
    "studies",
    "the",
    "to",
    "using",
    "via",
    "with",
}


def significant_title_tokens(title: str) -> set[str]:
    return {
        token
        for token in normalize_title(title).split()
        if len(token) > 2 and token not in TITLE_STOPWORDS
    }


def likely_preprint_of_manual_publication(work: Work, manual: ManualPublication) -> bool:
    if publication_state(work) != "Under review" or manual.state.lower() != "published":
        return False
    work_tokens = significant_title_tokens(work.title)
    manual_tokens = significant_title_tokens(manual.title)
    if not work_tokens or not manual_tokens:
        return False
    shared = work_tokens & manual_tokens
    shorter_size = min(len(work_tokens), len(manual_tokens))
    return len(shared) >= 4 and len(shared) / shorter_size >= 0.55


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "work"


def yaml_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def bibtex_key(work: Work) -> str:
    first_author = "sevilla"
    if work.authors:
        first_author = re.sub(r"[^a-z0-9]", "", work.authors[0].split()[-1].lower())
    return f"{first_author}{work.year or 'unknown'}{slugify(work.title).split('-')[0]}"


def bibtex_for(work: Work) -> str:
    entry_type = "article" if work.work_type == "journal-article" else "misc"
    fields = {
        "title": work.title,
        "author": " and ".join(work.authors),
        "journal": work.journal,
        "year": work.year,
        "doi": work.doi,
        "url": work.link,
    }
    lines = [f"@{entry_type}{{{bibtex_key(work)},"]
    for key, field_value in fields.items():
        if field_value:
            lines.append(f"  {key} = {{{field_value}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines) + "\n"


def text_citation_for(work: Work) -> str:
    author_text = ", ".join(work.authors) if work.authors else "Authors not listed in ORCID summary"
    venue = f" {work.journal}." if work.journal else ""
    return f"{author_text} ({work.year or 'n.d.'}). {work.title}.{venue}\n"


def is_preprint(work: Work) -> bool:
    doi = work.doi.lower()
    link = work.link.lower()
    return (
        doi.startswith("10.1101/")
        or doi.startswith("10.48550/arxiv")
        or doi.startswith("10.2139/ssrn")
        or "arxiv.org" in link
        or "biorxiv.org" in link
        or "ssrn.com" in link
    )


def publication_state(work: Work) -> str:
    if is_preprint(work):
        return "Under review"
    if work.journal or work.work_type in {"journal-article", "conference-paper", "conference-proceedings"}:
        return "Published"
    return "Publication"


def publication_rank(work: Work) -> tuple[int, str]:
    if publication_state(work) == "Published":
        return (2, work.date)
    if is_preprint(work):
        return (1, work.date)
    return (0, work.date)


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_title(left), normalize_title(right)).ratio()


def normalized_person_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def target_author_names(author_name: str) -> set[str]:
    normalized = normalized_person_name(author_name)
    names = {normalized}
    if "-" in author_name:
        names.add(normalized_person_name(author_name.replace("-", " ")))
    return names


def has_target_author(authors: list[str], author_name: str) -> bool:
    targets = target_author_names(author_name)
    return any(normalized_person_name(author) in targets for author in authors)


def crossref_date(item: dict) -> tuple[str, str, str]:
    for key in ("published-print", "published-online", "issued"):
        parts = item.get(key, {}).get("date-parts", [])
        if parts and parts[0]:
            date = [str(part) for part in parts[0]]
            return (
                date[0] if len(date) > 0 else "",
                date[1].zfill(2) if len(date) > 1 else "01",
                date[2].zfill(2) if len(date) > 2 else "01",
            )
    return "", "", ""


def crossref_authors(item: dict) -> list[str]:
    authors = []
    for author in item.get("author", []):
        given = author.get("given", "")
        family = author.get("family", "")
        name = " ".join(part for part in [given, family] if part).strip()
        if name:
            authors.append(name)
    return authors


def work_from_crossref(item: dict, source: Work) -> Work | None:
    titles = item.get("title") or []
    title = titles[0] if titles else ""
    doi = item.get("DOI", "")
    journals = item.get("container-title") or []
    journal = journals[0] if journals else ""
    if not title or not doi or not journal:
        return None
    year, month, day = crossref_date(item)
    return Work(
        put_code=source.put_code,
        title=title,
        year=year or source.year,
        month=month or source.month,
        day=day or source.day,
        work_type=item.get("type", "journal-article"),
        journal=journal,
        doi=doi,
        url=item.get("URL", ""),
        authors=crossref_authors(item) or source.authors,
        source="crossref",
    )


def find_published_version(work: Work) -> Work | None:
    if not is_preprint(work):
        return None
    query = urllib.parse.urlencode(
        {
            "query.title": work.title,
            "filter": "type:journal-article",
            "rows": "5",
            "select": "DOI,title,container-title,published-print,published-online,issued,type,URL,author",
        }
    )
    try:
        payload = fetch_crossref_json(f"{CROSSREF_API}?{query}")
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    candidates = []
    for item in payload.get("message", {}).get("items", []):
        published = work_from_crossref(item, work)
        if not published:
            continue
        score = title_similarity(work.title, published.title)
        if score >= 0.78:
            candidates.append((score, published))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def promote_preprint(work: Work) -> Work:
    override = PUBLISHED_OVERRIDES.get(work.put_code)
    if override:
        for field_name, field_value in override.items():
            setattr(work, field_name, field_value)
        return apply_metadata_overrides(work)
    return apply_metadata_overrides(find_published_version(work) or work)


def apply_metadata_overrides(work: Work) -> Work:
    override = METADATA_OVERRIDES.get(work.put_code)
    if override:
        for field_name, field_value in override.items():
            setattr(work, field_name, field_value)
    return work


def arxiv_text(entry: ET.Element, name: str) -> str:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    return (entry.findtext(f"atom:{name}", default="", namespaces=ns) or "").strip()


def arxiv_authors(entry: ET.Element) -> list[str]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    return [
        (author.findtext("atom:name", default="", namespaces=ns) or "").strip()
        for author in entry.findall("atom:author", ns)
    ]


def arxiv_doi(entry: ET.Element, arxiv_id: str) -> str:
    ns = {"arxiv": "http://arxiv.org/schemas/atom"}
    doi = entry.findtext("arxiv:doi", default="", namespaces=ns).strip()
    return doi or f"10.48550/arXiv.{arxiv_id}"


def parse_arxiv_entry(entry: ET.Element, author_name: str) -> Work | None:
    authors = arxiv_authors(entry)
    if not has_target_author(authors, author_name):
        return None
    identifier = arxiv_text(entry, "id").rsplit("/", 1)[-1]
    arxiv_id = re.sub(r"v\d+$", "", identifier)
    title = re.sub(r"\s+", " ", arxiv_text(entry, "title"))
    published = arxiv_text(entry, "published")
    year, month, day = (published[:10].split("-") + ["", "", ""])[:3]
    return Work(
        put_code=f"arxiv:{arxiv_id}",
        title=title,
        year=year,
        month=month,
        day=day,
        work_type="preprint",
        journal="",
        doi=arxiv_doi(entry, arxiv_id),
        url=f"https://arxiv.org/abs/{arxiv_id}",
        authors=authors,
        source="arxiv",
    )


def fetch_arxiv_works(author_name: str, max_results: int) -> list[Work]:
    queries = ["au:Sevilla-Salcedo", "au:Sevilla_Salcedo"]
    works: dict[str, Work] = {}
    for search_query in queries:
        query = urllib.parse.urlencode(
            {
                "search_query": search_query,
                "start": "0",
                "max_results": str(max_results),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        try:
            with urllib.request.urlopen(f"{ARXIV_API}?{query}", timeout=30) as response:
                root = ET.fromstring(response.read())
        except (TimeoutError, urllib.error.URLError, ET.ParseError):
            continue
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            work = parse_arxiv_entry(entry, author_name)
            if work:
                works[work.put_code] = promote_preprint(work)
    return list(works.values())


def fetch_crossref_author_works(author_name: str, rows: int, from_year: int) -> list[Work]:
    query = urllib.parse.urlencode(
        {
            "query.author": author_name,
            "filter": f"type:journal-article,from-pub-date:{from_year}-01-01",
            "rows": str(rows),
            "select": "DOI,title,container-title,published-print,published-online,issued,type,URL,author",
        }
    )
    try:
        payload = fetch_crossref_json(f"{CROSSREF_API}?{query}")
    except (TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    works = []
    source = Work("", "", "", "", "", "", "", "", "", [], source="crossref")
    for item in payload.get("message", {}).get("items", []):
        work = work_from_crossref(item, source)
        if work and has_target_author(work.authors, author_name):
            work.put_code = f"crossref:{work.doi.lower()}"
            works.append(work)
    return works


def work_summary_to_put_codes(group: dict) -> list[str]:
    summaries = group.get("work-summary", [])
    put_codes: list[str] = []
    for summary in summaries:
        put_code = summary.get("put-code")
        if put_code:
            put_codes.append(str(put_code))
    return put_codes


def details_from_bulk(orcid_id: str, put_codes: list[str]) -> list[dict]:
    if not put_codes:
        return []
    works: list[dict] = []
    for index in range(0, len(put_codes), 100):
        batch = ",".join(put_codes[index : index + 100])
        payload = fetch_json(f"{ORCID_API}/{orcid_id}/works/{batch}")
        bulk_items = payload.get("bulk", [])
        for item in bulk_items:
            work = item.get("work") if isinstance(item, dict) else None
            if isinstance(work, dict):
                works.append(work)
    return works


def parse_work(work: dict) -> Work | None:
    title = title_from(work)
    if not title:
        return None
    year, month, day = publication_date(work.get("publication-date"))
    return apply_metadata_overrides(Work(
        put_code=str(work.get("put-code", "")),
        title=title,
        year=year,
        month=month,
        day=day,
        work_type=value(work.get("type")),
        journal=value(work.get("journal-title")),
        doi=external_id(work, "doi"),
        url=value(work.get("url")),
        authors=contributors_from(work),
        source="orcid",
    ))


def replace_front_matter_field(front_matter: str, key: str, value_text: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.MULTILINE)
    line = f"{key}: {value_text}"
    if pattern.search(front_matter):
        return pattern.sub(line, front_matter, count=1)
    return front_matter.rstrip() + f"\n{line}\n"


def update_manual_publication(path: Path, work: Work) -> bool:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        return False
    front_matter, body = match.groups()
    state_match = re.search(r"^state:\s*(.+?)\s*$", front_matter, re.MULTILINE)
    if state_match and state_match.group(1).strip().lower() == "published":
        return False
    description = f"Published in {work.journal}" if work.journal else "Published"
    updates = {
        "date": work.date,
        "description": yaml_string(description),
        "link": work.link,
        "state": "Published",
    }
    updated_front_matter = front_matter
    for key, field_value in updates.items():
        updated_front_matter = replace_front_matter_field(updated_front_matter, key, field_value)
    updated = f"---\n{updated_front_matter}\n---\n{body}"
    if updated != text:
        path.write_text(updated, encoding="utf-8")
        return True
    return False


def front_matter_value(text: str, key: str) -> str:
    match = re.search(rf'^{re.escape(key)}:\s*["\']?(.+?)["\']?\s*$', text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def manual_publications(publications_dir: Path) -> dict[str, ManualPublication]:
    titles: dict[str, ManualPublication] = {}
    for path in publications_dir.glob("*.md"):
        if path.name.startswith(("orcid-", "auto-")):
            continue
        text = path.read_text(encoding="utf-8")
        title = front_matter_value(text, "title")
        if title:
            titles[title_key(title)] = ManualPublication(
                title=title,
                state=front_matter_value(text, "state"),
                path=path,
            )
    return titles


def existing_generated_works(publications_dir: Path) -> list[Work]:
    works: list[Work] = []
    for path in publications_dir.glob("*.md"):
        if not path.name.startswith(("orcid-", "auto-")):
            continue
        text = path.read_text(encoding="utf-8")
        if front_matter_value(text, "auto_generated").lower() != "true":
            continue

        title = front_matter_value(text, "title")
        date = front_matter_value(text, "date")
        link = front_matter_value(text, "link")
        source_id = front_matter_value(text, "source_id") or path.stem
        state = front_matter_value(text, "state")
        source = front_matter_value(text, "source") or "auto"
        description = front_matter_value(text, "description")
        if not title or not date:
            continue

        year, month, day = (date.split("-") + ["", "", ""])[:3]
        journal = ""
        if description.startswith("Published in "):
            journal = description.removeprefix("Published in ").strip()
        venue_match = re.search(r"^\*\*Venue:\*\*\s*(.+)$", text, re.MULTILINE)
        if venue_match:
            journal = venue_match.group(1).strip()

        doi = ""
        doi_match = re.search(r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\S+)", link)
        if doi_match:
            doi = doi_match.group(1)

        authors: list[str] = []
        authors_match = re.search(r"^\*\*Authors:\*\*\s*(.+)$", text, re.MULTILINE)
        if authors_match:
            authors = [author.strip() for author in authors_match.group(1).split(",") if author.strip()]

        work_type = "journal-article" if state == "Published" else "preprint"
        works.append(
            Work(
                put_code=source_id,
                title=title,
                year=year,
                month=month,
                day=day,
                work_type=work_type,
                journal=journal,
                doi=doi,
                url=link if not doi else "",
                authors=authors,
                source=source,
            )
        )
    return works


def remove_previous_generated(publications_dir: Path) -> None:
    for pattern in ("orcid-*.*", "auto-*.*"):
        for path in publications_dir.glob(pattern):
            path.unlink()


def write_work(publications_dir: Path, work: Work, weight: int, orcid_id: str) -> None:
    filename_base = FILENAME_OVERRIDES.get(work.put_code, f"auto-{work.year or 'undated'}-{slugify(work.title)}")
    state = publication_state(work)
    if state == "Published" and work.journal:
        description = f"Published in {work.journal}"
    elif state == "Under review":
        description = "Under review"
    else:
        description = "Publication"

    body_lines = []
    if work.authors:
        body_lines.append(f"**Authors:** {', '.join(work.authors)}")
    if work.journal:
        body_lines.append("")
        body_lines.append(f"**Venue:** {work.journal}")

    markdown = "\n".join(
        [
            "---",
            f"title: {yaml_string(work.title)}",
            f"date: {work.date}",
            f"description: {yaml_string(description)}",
            'tags: ["Paper"]',
            f"link: {work.link}",
            f"cite: {filename_base}.bib",
            f"state: {state}",
            "type: post",
            f"weight: {weight}",
            "showTableOfContents: true",
            "auto_generated: true",
            f"source_id: {work.put_code}",
            f"source: {work.source}",
            "---",
            "",
            "\n".join(body_lines),
            "",
        ]
    )
    (publications_dir / f"{filename_base}.md").write_text(markdown, encoding="utf-8")
    (publications_dir / f"{filename_base}.bib").write_text(bibtex_for(work), encoding="utf-8")
    (publications_dir / f"{filename_base}.txt").write_text(text_citation_for(work), encoding="utf-8")


def sync(orcid_id: str, publications_dir: Path, author_name: str, arxiv_max_results: int, crossref_rows: int, crossref_from_year: int) -> int:
    payload = fetch_json(f"{ORCID_API}/{orcid_id}/works")
    grouped_put_codes = [work_summary_to_put_codes(group) for group in payload.get("group", [])]
    put_codes = [code for group in grouped_put_codes for code in group]
    works_by_put_code = {
        str(item.get("put-code")): work
        for item in details_from_bulk(orcid_id, put_codes)
        if (work := parse_work(item))
    }
    works = []
    for group in grouped_put_codes:
        candidates = [works_by_put_code[code] for code in group if code in works_by_put_code]
        if candidates:
            promoted = [promote_preprint(candidate) for candidate in candidates]
            works.append(max(promoted, key=publication_rank))
    works.extend(fetch_arxiv_works(author_name, arxiv_max_results))
    works.extend(fetch_crossref_author_works(author_name, crossref_rows, crossref_from_year))
    works.extend(existing_generated_works(publications_dir))

    manual_pages = manual_publications(publications_dir)
    unique: dict[str, Work] = {}
    for work in works:
        key = title_key(work.title)
        manual_page = manual_pages.get(key)
        if manual_page:
            if publication_state(work) == "Published":
                update_manual_publication(manual_page.path, work)
            continue
        if any(likely_preprint_of_manual_publication(work, manual_page) for manual_page in manual_pages.values()):
            continue
        previous = unique.get(key)
        if previous is None or publication_rank(work) > publication_rank(previous):
            unique[key] = work

    ordered = sorted(unique.values(), key=lambda item: item.date, reverse=True)
    remove_previous_generated(publications_dir)
    for index, work in enumerate(ordered):
        write_work(publications_dir, work, weight=10 + index, orcid_id=orcid_id)
    return len(ordered)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Hugo publication pages from ORCID, arXiv, and Crossref metadata.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            The script only deletes files named orcid-* or auto-* inside the target directory.
            ORCID is prioritized as the source of truth for your curated work
            list. arXiv is queried by exact author match to catch preprints not
            yet listed in ORCID. Crossref is queried to discover or promote
            matching journal articles. Existing hand-written publication pages
            are left untouched unless they are not yet marked Published and a
            matching published article is found.
            """
        ),
    )
    parser.add_argument("--orcid-id", default=DEFAULT_ORCID_ID)
    parser.add_argument("--publications-dir", default="content/publications")
    parser.add_argument("--author-name", default=DEFAULT_AUTHOR_NAME)
    parser.add_argument("--arxiv-max-results", type=int, default=100)
    parser.add_argument("--crossref-rows", type=int, default=100)
    parser.add_argument("--crossref-from-year", type=int, default=2019)
    args = parser.parse_args()

    count = sync(
        args.orcid_id,
        Path(args.publications_dir),
        args.author_name,
        args.arxiv_max_results,
        args.crossref_rows,
        args.crossref_from_year,
    )
    print(f"Synced {count} publications from ORCID, arXiv, and Crossref.")


if __name__ == "__main__":
    main()
