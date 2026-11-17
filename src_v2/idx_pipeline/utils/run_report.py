from dataclasses import dataclass, field
from pathlib import Path

import json
import logging
import os


LOGGER = logging.getLogger(__name__)

CORRECTIONS_PATH = Path("data_v2/audit/corrections.json")


@dataclass
class RunSummary:
    window: str = "-"
    announcements: int = 0
    pdfs_downloaded: int = 0
    pdfs_queued: int = 0
    filings_parsed: int = 0
    already_in_db: int = 0
    filings_to_insert: int = 0
    replacements: int = 0
    news: int = 0
    highlights: list[str] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)
    pushed_to_db: bool = False
    fetch: str = "-"

    @property
    def amended(self) -> list[dict]:
        return [entry for entry in self.flagged if entry.get("status") == "amended"]

    @property
    def kept_as_filed(self) -> list[dict]:
        return [entry for entry in self.flagged if entry.get("status") == "kept_as_filed"]


def ids(entries: list[dict]) -> str:
    return ", ".join(entry.get("pdf_id") or "-" for entry in entries) or "-"


def log_run_summary(summary: RunSummary, logger: logging.Logger = LOGGER) -> None:
    inserted = "inserted" if summary.pushed_to_db else "would insert (dry run)"

    logger.info(
        "RUN SUMMARY  window %s\n"
        "    announcements %d | PDFs downloaded %d (queued for retry %d) | filings parsed %d\n"
        "    amended %d (%s) | kept as filed %d (%s)\n"
        "    already in DB %d | %s %d filing(s), %d news | correction replacements %d\n"
        "    idx.co.id requests: %s",
        summary.window,
        summary.announcements, summary.pdfs_downloaded,
        summary.pdfs_queued, summary.filings_parsed,
        len(summary.amended), ids(summary.amended),
        len(summary.kept_as_filed), ids(summary.kept_as_filed),
        summary.already_in_db, inserted, summary.filings_to_insert, summary.news,
        summary.replacements,
        summary.fetch,
    )


def write_corrections(flagged: list[dict], path: Path = CORRECTIONS_PATH) -> None:
    """
    Every filing whose holdings did not add up, amended or not.
    Rewritten every run, the history lives in the run artifacts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w") as file:
        json.dump(flagged, file, ensure_ascii=False, indent=2)


def number(value) -> str:
    return f"{int(value):,}" if value is not None else "-"


def write_job_summary(summary: RunSummary) -> None:
    """
    Markdown shown at the top of the GitHub run page, no log reading needed
    """
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")

    if not summary_path:
        return

    lines = [
        "## IDX filings run",
        "",
        f"Window: **{summary.window}**",
        "",
        "| Announcements | PDFs downloaded | Filings parsed | Amended | Kept as filed | Already in DB | Inserted | News |",
        "|---|---|---|---|---|---|---|---|",
        f"| {summary.announcements} | {summary.pdfs_downloaded}"
        f"{f' ({summary.pdfs_queued} queued)' if summary.pdfs_queued else ''} "
        f'| {summary.filings_parsed} | {len(summary.amended)} | {len(summary.kept_as_filed)} '
        f'| {summary.already_in_db} | {summary.filings_to_insert if summary.pushed_to_db else "dry run"} '
        f'| {summary.news} |',
        "",
        f"idx.co.id requests: {summary.fetch}",
    ]

    if summary.flagged:
        lines += [
            "",
            "### Holdings checks that did not add up",
            "",
            "| Filing | Symbol | Holder | Before + net = expected / filed after | Wrong | Found by | Action |",
            "|---|---|---|---|---|---|---|",
        ]

        for entry in summary.flagged:
            net = entry.get("net_shares") or 0
            sum_text = (
                f"{number(entry.get('holding_before'))} {'+' if net >= 0 else '−'} {number(abs(net))}"
                f" = {number(entry.get('expected_holding_after'))} / {number(entry.get('filed_holding_after'))}"
            )
            evidence = "<br>".join(entry.get("evidence") or ["-"])

            if entry.get("securities_report_url"):
                evidence += f"<br>[securities report]({entry.get('securities_report_url')})"

            lines.append(
                f"| [{entry.get('pdf_id')}]({entry.get('source')}) | {entry.get('symbol')} "
                f"| {entry.get('holder_name')} | {sum_text} | {entry.get('wrong') or 'unknown'} "
                f"| {evidence} | {entry.get('action') or '-'} |"
            )

    try:
        with open(summary_path, "a") as file:
            file.write("\n".join(lines) + "\n")

    except OSError as error:
        LOGGER.warning("could not write the GitHub job summary: %s", error)
