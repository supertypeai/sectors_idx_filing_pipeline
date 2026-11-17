from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import logging
import os
import sys


STAGE_COUNT = 6

IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"

# Third party loggers that are pure noise at INFO (one line per HTTP call)
NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "requests",
    "botocore",
    "boto3",
    "hpack",
    "langchain",
    "langsmith",
    "groq",
    "google_genai",
)


class OwnDebugOnly(logging.Filter):
    """
    pipeline.log keeps our own DEBUG lines, but only WARNING and up from libraries,
    otherwise boto, httpx and friends would bury it
    """
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True

        return record.name.startswith("idx_pipeline") or record.name == "__main__"


class GitHubErrorAnnotation(logging.Handler):
    """
    Stages are collapsed groups in the GitHub log, which would hide an error inside
    them. An ::error:: annotation also shows it on the run page.
    """
    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage().splitlines()[0][:300]
        print(f"::error::{message}", flush=True)


def setup_logging(log_file: str = "pipeline.log") -> None:
    console_level = getattr(
        logging,
        os.getenv("LOG_LEVEL", "INFO").upper(),
        logging.INFO
    )

    logging.addLevelName(logging.WARNING, "WARN")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    ))

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(OwnDebugOnly())
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    handlers = [console, file_handler]

    if IS_GITHUB_ACTIONS:
        annotation = GitHubErrorAnnotation()
        annotation.setLevel(logging.ERROR)
        handlers.append(annotation)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    for handler in handlers:
        root.addHandler(handler)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # urllib3 logs every retry as a WARNING. The downloader reports its own
    # failures, so only keep urllib3 errors
    logging.getLogger("urllib3").setLevel(logging.ERROR)


@contextmanager
def stage(number: int, name: str, logger: logging.Logger):
    """
    One collapsible group per stage in GitHub Actions, a plain header elsewhere
    """
    title = f"[{number}/{STAGE_COUNT}] {name}"
    started = monotonic()

    if IS_GITHUB_ACTIONS:
        print(f"::group::{title}", flush=True)
    else:
        logger.info("=== %s ===", title)

    try:
        yield

    finally:
        logger.debug("%s took %.0fs", name, monotonic() - started)

        if IS_GITHUB_ACTIONS:
            print("::endgroup::", flush=True)


def pdf_id(pdf_url: str | None) -> str:
    """
    'https://.../From_KSEI/LK-06112026-5111-00.pdf-0.pdf' -> 'LK-06112026-5111'
    The short id is what people search the log for.
    """
    if not pdf_url:
        return "-"

    name = Path(pdf_url).name

    for suffix in (".pdf-0.pdf", ".pdf"):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
            break

    # local copies are lowercased, keep the id the same as in the IDX url
    if name.upper().startswith("LK-"):
        name = name.upper()

        if name.endswith("-00"):
            name = name[:-3]

    return name


def short_error(error: Exception) -> str:
    """
    requests wraps a timeout in three layers of repr, keep the part a person needs
    """
    text = str(error)

    for marker, label in (
        ("Read timed out", "read timed out"),
        ("ConnectTimeout", "connect timed out"),
        ("Connection refused", "connection refused"),
        ("ProxyError", "proxy error"),
    ):
        if marker in text:
            return label

    return f"{type(error).__name__}: {text[:160]}"
