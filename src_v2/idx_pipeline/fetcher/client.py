from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic, sleep

import json
import logging
import requests


LOGGER = logging.getLogger(__name__)

SCRAPINGANT_URL = "https://api.scrapingant.com/v2/general"
SCRAPINGANT_USAGE_URL = "https://api.scrapingant.com/v2/usage"
SCRAPINGANT_STATE_PATH = Path("data_v2/state/scrapingant.json")

# One datacenter try (1 credit, often 423 on PDFs), then residential (25 credits).
# A 423 is not charged, so one extra residential try costs nothing when it fails
SCRAPINGANT_ATTEMPTS = ("datacenter", "residential", "residential")
RESIDENTIAL_CREDITS = 25
CREDITS = {"datacenter": 1, "residential": RESIDENTIAL_CREDITS}

SCRAPINGANT_TIMEOUT = (15, 120)

# Web Unlocker answers only once idx is unlocked, 50-90s in our tests. A retry
# restarts the unlock from zero, so wait long and retry once at most
PROXY_TIMEOUT = (15, 150)

STATS: Counter = Counter()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_scrapingant_state() -> dict:
    try:
        return json.loads(SCRAPINGANT_STATE_PATH.read_text())

    except (OSError, ValueError):
        return {"enabled": True}


def save_scrapingant_state(state: dict) -> None:
    SCRAPINGANT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCRAPINGANT_STATE_PATH.write_text(json.dumps(state, indent=2) + "\n")


def short_id(url: str) -> str:
    return url.split("?")[0].rsplit("/", 1)[-1][:60]


class IdxClient:
    """
    Drop-in for the requests.Session the pipeline used: get(url, params=..., stream=...)
    returns a requests.Response whose status is idx.co.id's own status.
    """
    def __init__(self, proxy_session: requests.Session, scrapingant_key: str | None):
        self.proxy_session = proxy_session
        self.scrapingant_key = scrapingant_key

    def credits_left(self) -> tuple[int | None, str | None]:
        try:
            response = requests.get(
                SCRAPINGANT_USAGE_URL,
                headers={"x-api-key": self.scrapingant_key},
                timeout=30,
            )
            data = response.json()
            return data.get("remained_credits"), data.get("end_date")

        except (requests.RequestException, ValueError) as error:
            LOGGER.debug("scrapingant usage check failed: %s", error)
            return None, None

    def scrapingant_enabled(self) -> bool:
        if not self.scrapingant_key:
            if not STATS["scrapingant_no_key_logged"]:
                LOGGER.info("[FETCH] no SCRAPINGANT_API_KEY, using Bright Data only")
                STATS["scrapingant_no_key_logged"] = 1
            return False

        state = load_scrapingant_state()

        if state.get("enabled", True):
            return True

        recheck_after = state.get("recheck_after")

        if recheck_after and now_utc() < datetime.fromisoformat(recheck_after):
            return False

        # The plan period may have renewed, see if there are credits again
        credits, plan_end = self.credits_left()

        if credits is not None and credits >= RESIDENTIAL_CREDITS:
            save_scrapingant_state(
                {"enabled": True, "enabled_at": now_utc().isoformat()}
            )
            LOGGER.info(
                "[FETCH] ScrapingAnt has %d credits again, using it as the main route", 
                credits
            )
            return True

        state["recheck_after"] = self.next_recheck(plan_end)
        save_scrapingant_state(state)
        return False

    def next_recheck(self, plan_end: str | None) -> str:
        if plan_end:
            try:
                end = datetime.fromisoformat(plan_end)

                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)

                if end > now_utc():
                    return end.isoformat()
                
            except ValueError:
                pass

        return (now_utc() + timedelta(days=1)).isoformat()

    def disable_scrapingant(self, reason: str) -> None:
        credits, plan_end = self.credits_left()
        recheck_after = self.next_recheck(plan_end)

        save_scrapingant_state({
            "enabled": False,
            "reason": reason,
            "disabled_at": now_utc().isoformat(),
            "credits_left": credits,
            "recheck_after": recheck_after,
        })

        LOGGER.warning(
            "[FETCH] ScrapingAnt switched off (%s), using Bright Data until %s",
            reason, recheck_after[:10]
        )

    def via_scrapingant(self, url: str) -> requests.Response | None:
        for attempt, proxy_type in enumerate(SCRAPINGANT_ATTEMPTS, start=1):
            started = monotonic()

            try:
                response = requests.get(
                    SCRAPINGANT_URL,
                    params={"url": url, "browser": "false", "proxy_type": proxy_type},
                    headers={"x-api-key": self.scrapingant_key},
                    timeout=SCRAPINGANT_TIMEOUT,
                )

            except requests.RequestException as error:
                LOGGER.debug(
                    "scrapingant %s request failed for %s: %s", proxy_type, url, error
                )
                return None

            elapsed = monotonic() - started

            if response.status_code == 200:
                # ScrapingAnt answers 200 whenever it reached the site, idx's own
                # status is in this header
                page_status = int(response.headers.get("Ant-page-status-code") or 200)

                STATS["scrapingant"] += 1
                STATS["scrapingant_credits"] += CREDITS[proxy_type]

                LOGGER.debug(
                    "scrapingant %s %s -> idx %s in %.1fs", proxy_type, short_id(url),
                    page_status, elapsed
                )

                if page_status >= 400 and page_status != 404:
                    return None

                response.status_code = page_status
                return response

            body = response.text[:200]

            # idx blocks some IPs, and a 423 is not charged, so another attempt costs nothing
            if response.status_code == 423:
                LOGGER.debug(
                    "scrapingant %s blocked for %s (attempt %d)", 
                    proxy_type, short_id(url), attempt
                )
                continue

            # Free plan allows one request at a time
            if response.status_code == 409:
                sleep(2)
                continue

            # Retrying a used-up quota or a bad key only fails again, so hand over to Bright Data
            if response.status_code == 403:
                self.disable_scrapingant(body or "HTTP 403")
                return None

            LOGGER.debug(
                "scrapingant HTTP %s for %s: %s", 
                response.status_code, short_id(url), body
            )
            return None

        return None

    def via_proxy(self, url: str, params: dict | None, stream: bool) -> requests.Response:
        STATS["brightdata"] += 1
        started = monotonic()

        response = self.proxy_session.get(
            url, params=params, stream=stream, timeout=PROXY_TIMEOUT
        )

        LOGGER.debug(
            "bright data %s -> %s in %.1fs", short_id(url), response.status_code,
            monotonic() - started
        )
        return response

    def get(
        self,
        url: str,
        params: dict | None = None,
        stream: bool = False,
        **_ignored,
    ) -> requests.Response:
        if self.scrapingant_enabled():
            full_url = requests.Request("GET", url, params=params).prepare().url
            response = self.via_scrapingant(full_url)

            if response is not None:
                return response

            STATS["fallback"] += 1
            LOGGER.info(
                "[FETCH] ScrapingAnt could not get %s, trying Bright Data", 
                short_id(full_url)
            )

        return self.via_proxy(url, params, stream)


def fetch_summary() -> str:
    parts = [f"ScrapingAnt {STATS['scrapingant']} (~{STATS['scrapingant_credits']} credits)"]
    parts.append(f"Bright Data {STATS['brightdata']}")

    if STATS["fallback"]:
        parts.append(f"fallbacks {STATS['fallback']}")

    return " | ".join(parts)
