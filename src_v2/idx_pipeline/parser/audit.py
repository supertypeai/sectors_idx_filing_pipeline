from dataclasses import asdict, dataclass, field


RECONCILES = "reconciles"
NOT_CHECKED = "not_checked"
AMENDED = "amended"
KEPT_AS_FILED = "kept_as_filed"


@dataclass
class HoldingsCheck:
    holding_before: int | None
    net_shares: int | None
    filed_holding_after: int | None
    status: str = NOT_CHECKED
    wrong: str | None = None
    evidence: list[str] = field(default_factory=list)
    action: str | None = None
    securities_report_url: str | None = None

    @property
    def expected_holding_after(self) -> int | None:
        if self.holding_before is None or self.net_shares is None:
            return None

        return self.holding_before + self.net_shares

    @property
    def is_flagged(self) -> bool:
        return self.status in (AMENDED, KEPT_AS_FILED)

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "expected_holding_after": self.expected_holding_after,
        }


_CHECKS: dict[str, HoldingsCheck] = {}


def record_check(pdf_url: str, check: HoldingsCheck) -> None:
    # The LLM retry re-runs the check on the same PDF, the later one wins
    _CHECKS[pdf_url] = check


def pop_check(pdf_url: str) -> HoldingsCheck | None:
    return _CHECKS.pop(pdf_url, None)


def number(value) -> str:
    if value is None:
        return "-"

    if isinstance(value, float) and not value.is_integer():
        return f"{value:,}"

    return f"{int(value):,}"


def sum_line(check: HoldingsCheck) -> str:
    net = check.net_shares or 0
    sign = "+" if net >= 0 else "−"
    expected = check.expected_holding_after
    filed = check.filed_holding_after

    gap = ""
    if expected is not None and filed is not None:
        gap = f" (off by {number(abs(expected - filed))})"

    return (
        f"{number(check.holding_before)} {sign} {number(abs(net))} = {number(expected)}"
        f"  vs filed {number(filed)}{gap}"
    )


def marker(check: HoldingsCheck | None) -> str:
    if check is None:
        return ""

    return {
        RECONCILES: "✓ reconciles",
        AMENDED: "✎ AMENDED",
        KEPT_AS_FILED: "⚠ kept as filed",
        NOT_CHECKED: "· holdings check n/a",
    }.get(check.status, "")


def format_card(check: HoldingsCheck, pdf_url: str | None = None, indent: str = "    ") -> str:
    lines = [
        "flagged  : holding_before + net_shares ≠ holding_after",
        f"           {sum_line(check)}",
        f"wrong    : {check.wrong or 'unknown'}",
    ]

    evidence = check.evidence or ["-"]
    lines.append(f"found by : {evidence[0]}")
    lines.extend(f"           {item}" for item in evidence[1:])
    lines.append(f"action   : {check.action or '-'}")

    # The card is what a reviewer checks the decision against, so point at both documents
    if pdf_url:
        lines.append(f"filing   : {pdf_url}")

    if check.securities_report_url:
        lines.append(f"report   : {check.securities_report_url}")

    return "\n".join(indent + line for line in lines)
