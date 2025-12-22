# parser_idx.py
from __future__ import annotations
from typing import List, Dict, Optional, Any
from datetime import datetime
from pathlib import Path

from src.common.log import get_logger
from src.common.datetime import MONTHS_EN

from .base_parser import BaseParser
from .parser_idx_new import parser_new_document
from .utils.text_extractor import TextExtractor
from .utils.number_parser import NumberParser
from .utils.name_cleaner import NameCleaner
from .utils.transaction_classifier import TransactionClassifier
from .utils.company_resolver import (
    build_reverse_map,
    resolve_symbol_from_emiten,
    canonical_name_for_symbol,
    normalize_company_name,
    suggest_symbols,
    resolve_symbol_and_name,
    pretty_company_name,
)

import os, re

logger = get_logger(__name__)

EN_DATE_PATTERN = (
    r"(?:\d{1,2})\s+"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"\d{4}"
)

_EN_DATE_RE = re.compile(
    r"\b(?P<d>\d{1,2})\s+(?P<m>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<y>\d{4})\b",
    flags=re.I
)

SYMBOL_TOKEN_RE = re.compile(r"^[A-Z0-9]{3,6}$")

def _en_date_to_iso(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = _EN_DATE_RE.search(s)
    if not m:
        return None
    d = int(m.group("d"))
    mnum = MONTHS_EN.get(m.group("m").lower())
    y = int(m.group("y"))
    if not mnum:
        return None
    return f"{y:04d}-{mnum:02d}-{d:02d}"


class IDXParser(BaseParser):
    def __init__(
        self,
        pdf_folder: str = "downloads/idx-format",
        output_file: str = "data/parsed_idx_output.json",
        announcement_json: str = "data/idx_announcements.json",
    ):
        super().__init__(
            pdf_folder=pdf_folder,
            output_file=output_file,
            announcement_json=announcement_json,
        )
        # Parser label
        self.parser_type = "idx"

        self._current_alert_context: Optional[Dict[str, Any]] = None

        self.company_map = self._load_company_mapping() or self.symbol_to_name or {}
        self._rev_company_map = build_reverse_map(self.company_map)
        self.company_names = set(self.company_map.values())

    def _load_company_mapping(self) -> Dict[str, Any]:
        try:
            import json
            path = os.getenv("COMPANY_MAP_FILE", "data/company/company_map.json")
            if not os.path.exists(path):
                logger.warning(f"Company mapping not found: {path}")
                return {}

            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            out: Dict[str, Any] = {}

            def add(sym: str, nm: Optional[str]):
                if not sym or not nm:
                    return
                s = str(sym).strip().upper()
                n = str(nm).strip()
                if not s or not n:
                    return
                if s.endswith(".JK"):
                    out[s] = n
                    out[s[:-3]] = n
                else:
                    out[s] = n
                    out[f"{s}.JK"] = n

            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        add(k, v.get("company_name") or v.get("name") or v.get("legal_name"))
                    elif isinstance(v, str):
                        add(k, v)
            elif isinstance(raw, list):
                for item in raw:
                    add(item.get("symbol", ""), item.get("company_name", ""))
            else:
                logger.error(f"Unsupported company_map.json structure: {type(raw).__name__}")
                return {}

            logger.info(f"Loaded {len(out)} company symbols from local mapping")
            return out

        except Exception as e:
            logger.error(f"load company_map error: {e}")
            return {}

    # Entry point
    def parse_single_pdf(
        self,
        filepath: str,
        filename: str,
        pdf_mapping: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Parse a single IDX-format PDF into a normalized dict.

        Alerts:
          - no_text_extracted  (not_inserted)
          - parse_exception    (not_inserted)
          - symbol_missing     (not_inserted, when symbol cannot be resolved)
          - symbol_name_mismatch      (inserted, warning)
        """
        self._current_alert_context = (pdf_mapping or {}).get(filename) or {}

        text = self.extract_text_from_pdf(filepath)
        if not text:
            self._parser_fail(
                code="no_text_extracted",
                filename=filename,
                reasons=[
                    {
                        "scope": "parser",
                        "code": "no_text_extracted",
                        "message": "No text could be extracted from PDF at parser stage.",
                        "details": {"announcement": self._current_alert_context},
                    }
                ],
            )
            return None

        pdf_file = pdf_mapping.get(filename)
        source_pdf_url = pdf_file.get('main_link', '')
        # print(f'\nraw pdf mapping: {pdf_file}')
        # print(f'\nfilename: {filename}')
        # print(f'\npdf url to if condition: {source_pdf_url}\n')

        if 'From_KSEI' in source_pdf_url:
            try:
                timestamp = pdf_file.get('date', '')
                timestamp_object = datetime.fromisoformat(timestamp)
                timestamp_str = timestamp_object.strftime('%Y-%m-%d')

                data = parser_new_document(f'downloads/idx-format/{filename}')

                # Build tags
                purpose = data.get('purpose')
                shares_percentage_before = data.get('shares_percentage_before')
                shares_percentage_after = data.get('shares_percentage_after')
                transaction_type = data.get('transaction_type')

                tags = TransactionClassifier.detect_tags_for_new_document(
                    purpose, shares_percentage_before, shares_percentage_after, transaction_type
                )

                # Build holder type
                holder_type = NameCleaner.classify_holder_type(data.get('holder_name'))

                # Update the payload parser
                data.update({'tags': tags})
                data.update({'holder_type': holder_type})
                data.update({'source': source_pdf_url})    
                data.update({'timestamp': timestamp_str})            
                return data 

            except Exception as e:
                logger.error(f"extract_fields error {filename}: {e}", exc_info=True)
                # Fatal: structural parse error
                self._parser_fail(
                    code="parse_exception",
                    filename=filename,
                    reasons=[
                        {
                            "scope": "parser",
                            "code": "parse_exception",
                            "message": f"Error while extracting fields from IDX NEW FORMAT PDF: {e}",
                            "details": {"announcement": self._current_alert_context},
                        }
                    ],
                )
                return None
        else:
            text = self._slice_to_english(text)
            self.save_debug_output(filename, text)

            try:
                data = self.extract_fields_from_text(text, filename)
                data["source"] = filename
                
                # Compute standardized tags
                # Flags from text (MESOP, free-float, inheritance/transfer hints)
                flags = TransactionClassifier.detect_flags_from_text(text)

                # Build txns list from parsed rows
                txns = (data.get("transactions") or [])
                # If rows empty, synthesize from doc-level type
                if not txns and data.get("transaction_type") in {"buy", "sell", "transfer"}:
                    txns = [{"type": data["transaction_type"], "amount": data.get("amount_transacted") or 0}]

                data["tags"] = TransactionClassifier.compute_filings_tags(
                    txns=txns,
                    share_percentage_before=data.get("share_percentage_before"),
                    share_percentage_after=data.get("share_percentage_after"),
                    flags=flags,
                )

                return data
            
            except Exception as e:
                logger.error(f"extract_fields error {filename}: {e}", exc_info=True)
                # Fatal: structural parse error
                self._parser_fail(
                    code="parse_exception",
                    filename=filename,
                    reasons=[
                        {
                            "scope": "parser",
                            "code": "parse_exception",
                            "message": f"Error while extracting fields from IDX PDF: {e}",
                            "details": {"announcement": self._current_alert_context},
                        }
                    ],
                )
                return None

    def _slice_to_english(self, text: str) -> str:
        lines = (text or "").splitlines()
        for i, ln in enumerate(lines):
            if "go to indonesian page" in (ln or "").lower():
                return "\n".join(lines[i + 1:])
        return text

    def extract_fields_from_text(self, text: str, filename: str) -> Dict[str, Any]:
        ex = TextExtractor(text)
        res: Dict[str, Any] = {"lang": "en"}

        # Header-ish fields (beware swapped labels on some docs)
        res["issuer_code"] = (
            ex.find_table_value("Issuer Name")
            or ex.find_value_in_line("Issuer Name")
            or ""
        ).strip()

        res["attachments"] = (
            ex.find_table_value("Listing Board")
            or ex.find_value_in_line("Listing Board")
            or ""
        ).strip()

        res["subject"] = (
            ex.find_table_value("Attachments")
            or ex.find_value_in_line("Attachments")
            or ""
        ).strip()

        issuer_name_raw = (
            ex.find_table_value("Name of Share of Public Company")
            or ex.find_value_in_line("Name of Share of Public Company")
            or ""
        ).strip()

        sym: Optional[str] = None
        company_name_out: str = issuer_name_raw

        if issuer_name_raw:
            token = issuer_name_raw.strip().upper()

            # Case A: issuer_name_raw is a ticker
            if SYMBOL_TOKEN_RE.fullmatch(token) and (
                token in self.company_map or f"{token}.JK" in self.company_map
            ):
                sym = token if token in self.company_map else f"{token}.JK"
                if not sym.endswith(".JK"):
                    sym = f"{sym}.JK"

                company_name_out = (
                    canonical_name_for_symbol(self.company_map, sym) or issuer_name_raw
                )

            # Case B: resolve from emiten name (fuzzy)
            if not sym:
                min_score = int(os.getenv("COMPANY_RESOLVE_MIN_SCORE", "85"))
                sym2, _k, _t = resolve_symbol_from_emiten(
                    issuer_name_raw,
                    symbol_to_name=self.company_map,
                    rev_map=self._rev_company_map,
                    fuzzy=True,
                    min_score=min_score,
                )
                if sym2:
                    sym2 = sym2.upper()
                    if not sym2.endswith(".JK"):
                        sym2 = f"{sym2}.JK"
                    sym = sym2
                    company_name_out = (
                        canonical_name_for_symbol(self.company_map, sym) or issuer_name_raw
                    )
            
            if sym:
                sym_from_name = sym
                sym_doc: Optional[str] = None

                issuer_code_token = (res.get("issuer_code") or "").strip().upper()
                if issuer_code_token and SYMBOL_TOKEN_RE.fullmatch(issuer_code_token):
                    # Normalize to .JK format for consistency
                    if not issuer_code_token.endswith(".JK"):
                        issuer_code_token = f"{issuer_code_token}.JK"
                    sym_doc = issuer_code_token

                if sym_from_name and sym_doc and sym_from_name != sym_doc:
                    self._alert_symbol_mismatch(
                        filename=filename,
                        raw=issuer_name_raw,
                        canon=company_name_out,
                        sym_from_name=sym_from_name,
                        sym_doc=sym_doc,
                    )

        if issuer_name_raw and not sym:
            norm_key = normalize_company_name(issuer_name_raw)
            suggestions = suggest_symbols(
                issuer_name_raw,
                self.company_map,
                self._rev_company_map,
                top_k=int(os.getenv("COMPANY_SUGGEST_TOPK", "3")),
            )

            self._parser_fail(
                code="symbol_missing",
                filename=filename,
                reasons=[
                    {
                        "scope": "parser",
                        "code": "symbol_missing",
                        "message": "Symbol could not be resolved from issuer name in IDX parser.",
                        "details": {
                            "company_name_raw": issuer_name_raw,
                            "normalized_key": norm_key,
                            "missing_in_company_map": norm_key
                            not in (self._rev_company_map or {}),
                            "suggestions": suggestions,
                            "announcement": self._current_alert_context,
                        },
                    }
                ],
            )

            res["skip_filing"] = True
            res["skip_reason"] = "Symbol Not Resolved from Name"
            res.setdefault("parse_warnings", []).append("Symbol Not Resolved from Name")
            company_name_out = pretty_company_name(issuer_name_raw)

        # Persist company fields
        res["company_name_raw"] = issuer_name_raw or ""
        res["company_name"] = company_name_out or ""
        res["symbol"] = sym or None

        res["classification_of_shareholder"] = (
            ex.find_table_value("Classification of Shareholder")
            or ex.find_value_in_line("Classification of Shareholder")
            or ""
        ).strip()

        res["controlling_shareholder"] = (
            ex.find_table_value("Controlling Shareholder")
            or ex.find_value_in_line("Controlling Shareholder")
            or ex.find_table_value("Controling Shareholder")
            or ex.find_value_in_line("Controling Shareholder")
            or ""
        ).strip()

        res["citizenship"] = (
            ex.find_table_value("Citizenship")
            or ex.find_value_in_line("Citizenship")
            or ""
        ).strip()

        res["percentage_of_shares_traded"] = NumberParser.parse_percentage(
            ex.find_table_value("Percentage of Shares traded")
            or ex.find_value_in_line("Percentage of Shares traded")
        )

        res["share_ownership_status"] = (
            ex.find_table_value("Share Ownership Status")
            or ex.find_value_in_line("Share Ownership Status")
            or ""
        ).strip()

        res["purpose"] = (
            ex.find_table_value("Purposes of transaction")
            or ex.find_value_in_line("Purposes of transaction")
            or ""
        ).strip()

        # Holder
        holder_name_raw = (
            ex.find_table_value("Name of Shareholder")
            or ex.find_value_in_line("Name of Shareholder")
            or ""
        ).strip()
        res["holder_name_raw"] = holder_name_raw

        holder_type = NameCleaner.classify_holder_type(holder_name_raw)
        res["holder_type"] = holder_type

        if holder_type == "institution":
            hsym, disp, _key, _tried = resolve_symbol_and_name(
                holder_name_raw,
                self.company_map,
                rev_map=self._rev_company_map,
                fuzzy=True,
                min_score=int(os.getenv("COMPANY_RESOLVE_MIN_SCORE", "80")),
            )
            res["holder_name"] = disp
            res["holder_symbol"] = hsym
        else:
            res["holder_name"] = NameCleaner.clean_holder_name(holder_name_raw, "insider")
            res["holder_symbol"] = None

        # Validate holder
        if not NameCleaner.is_valid_holder(res.get("holder_name")):
            res["skip_filing"] = True
            res["skip_reason"] = "Invalid holder_name"
            res.setdefault("parse_warnings", []).append("Invalid holder_name")
            return res

        # Holdings / percentages
        res["holding_before"] = NumberParser.parse_number(
            ex.find_number_after_keyword("Number of shares owned before the transaction")
        )
        res["holding_after"] = NumberParser.parse_number(
            ex.find_number_after_keyword("Number of shares owned after the transaction")
        )
        res["share_percentage_before"] = NumberParser.parse_percentage(
            ex.find_percentage_after_keyword("Percentage of ownership before the transaction")
        )
        res["share_percentage_after"] = NumberParser.parse_percentage(
            ex.find_percentage_after_keyword("Percentage of ownership after the transaction")
        )
        res["share_percentage_transaction"] = abs(
            (res.get("share_percentage_after") or 0.0) - (res.get("share_percentage_before") or 0.0)
        )

        # Address/phone (best-effort)
        addr = (
            ex.find_value_in_line("Address")
            or ex.find_value_after_keyword("Address")
            or ""
        ).strip()
        if not addr:
            for ln in ex.lines or []:
                L = (ln or "").strip().lower()
                if L.startswith(("graha", "gedung", "tower", "jl", "jalan")):
                    addr = ln.strip()
                    break
        if addr:
            res["company_address"] = addr

        phone = (
            ex.find_value_in_line("Telephone Number")
            or ex.find_value_after_keyword("Telephone Number")
            or ""
        ).strip()
        if phone:
            res["company_phone"] = phone

        # Transactions parse (fills res["transactions"] and doc-level res["transaction_type"])
        self._extract_transactions_en(ex, res)
        self._postprocess_transactions(res)

        return res

    def _extract_transactions_en(self, ex: TextExtractor, res: Dict[str, Any]) -> None:
        # Doc-level declared type
        for i, line in enumerate(ex.lines or []):
            if "transaction type" in (line or "").lower():
                for j in range(i + 1, min(i + 8, len(ex.lines))):
                    t = (ex.lines[j] or "").lower()
                    if "buy" in t:
                        res["transaction_type"] = "buy"; break
                    if "sell" in t:
                        res["transaction_type"] = "sell"; break
                    if "transfer" in t:
                        res["transaction_type"] = "transfer"; break
                break

        full_text = "\n".join(ex.lines or [])
        rows = self._parse_transactions_text_en(full_text)
        if not rows:
            rows = self._parse_transactions_lines_en(ex.lines or [])
        res["transactions"] = rows

    def _parse_transactions_text_en(self, text: str) -> List[Dict[str, Any]]:
        if not text:
            return []
        pat = re.compile(
            rf"Type of Transaction:\s*(?P<typ>Buy|Sell|Transfer)\s*.*?"
            rf"Transaction Price:\s*(?P<price>[\d\.,]+)\s*.*?"
            rf"Transaction Date:\s*(?P<date>{EN_DATE_PATTERN})\s*.*?"
            rf"Number of Shares Transacted:\s*(?P<amount>[\d\.,]+)",
            flags=re.I | re.S,
        )
        out: List[Dict[str, Any]] = []
        for m in pat.finditer(text):
            typ_raw = (m.group("typ") or "").strip().lower()
            typ = "buy" if typ_raw.startswith("b") else ("sell" if typ_raw.startswith("s") else "transfer")
            price = NumberParser.parse_number(m.group("price")) or 0.0
            amt = NumberParser.parse_number(m.group("amount")) or 0
            datestr = (m.group("date") or "").strip()
            out.append({
                "type": typ,
                "price": price,
                "amount": amt,
                "date": datestr,
                "date_iso": _en_date_to_iso(datestr),  # <-- ISO per row
                "value": price * amt,
            })
        return out


    def _parse_transactions_lines_en(self, lines: List[str]) -> List[Dict[str, Any]]:
        if not lines:
            return []
        row_re = re.compile(
            rf"\b(?P<typ>Buy|Sell|Transfer)\b\s+(?P<price>[\d\.,]+)\s+(?P<date>{EN_DATE_PATTERN})\s+(?P<amount>[\d\.,]+)",
            flags=re.I,
        )
        out: List[Dict[str, Any]] = []
        for raw in lines:
            m = row_re.search(raw or "")
            if not m:
                continue
            typ_raw = (m.group("typ") or "").lower()
            typ = "buy" if typ_raw.startswith("b") else ("sell" if typ_raw.startswith("s") else "transfer")
            price = NumberParser.parse_number(m.group("price")) or 0.0
            amt = NumberParser.parse_number(m.group("amount")) or 0
            datestr = (m.group("date") or "").strip()
            out.append({
                "type": typ,
                "price": price,
                "amount": amt,
                "date": datestr,
                "date_iso": _en_date_to_iso(datestr),  # <-- ISO per row
                "value": price * amt,
            })
        return out


    def _postprocess_transactions(self, res: Dict[str, Any]) -> None:
        txs = res.get("transactions") or []
        buy_sell = [t for t in txs if t.get("type") in {"buy", "sell"}]
        transfers = [t for t in txs if t.get("type") == "transfer"]
        any_tx = buy_sell or transfers

        # Totals derived from table rows
        rows_amt_buy_sell = sum(int(t.get("amount") or 0) for t in buy_sell)
        rows_val_buy_sell = sum(float(t.get("value") or 0.0) for t in buy_sell)
        rows_amt_transfer = sum(int(t.get("amount") or 0) for t in transfers)
        rows_val_transfer = sum(float(t.get("value") or 0.0) for t in transfers)

        # Delta from before/after holdings (when available)
        hb = res.get("holding_before")
        ha = res.get("holding_after")
        delta_amt = None
        try:
            if isinstance(hb, (int, float)) and isinstance(ha, (int, float)):
                delta_amt = abs(int(ha) - int(hb))
        except Exception:
            delta_amt = None

        res["amount_transacted_rows"] = rows_amt_buy_sell or rows_amt_transfer
        # Prefer holdings delta; fall back to buy/sell rows, then transfer rows
        res["amount_transacted"] = (
            delta_amt
            if (delta_amt is not None)
            else (rows_amt_buy_sell or rows_amt_transfer)
        )
        res["transaction_value"] = rows_val_buy_sell or rows_val_transfer

        res["has_transfer"] = bool(transfers)
        res["amount_transferred"] = sum(int(t.get("amount") or 0) for t in transfers)
        res["value_transferred"] = sum(float(t.get("value") or 0.0) for t in transfers)

        # Determine document-level type if not yet set
        if not res.get("transaction_type"):
            kinds = {t.get("type") for t in txs}
            if kinds == {"transfer"}:
                res["transaction_type"] = "transfer"
            elif kinds <= {"buy", "sell"} and len({t["type"] for t in buy_sell}) == 1:
                res["transaction_type"] = buy_sell[0]["type"]

        # Weighted average price (prefer buy/sell; fall back to transfers if needed)
        priced_rows = buy_sell if buy_sell else transfers
        total_amt = sum(int(t.get("amount") or 0) for t in priced_rows if int(t.get("amount") or 0) > 0)
        if total_amt:
            wavg = sum(float(t.get("price") or 0.0) * int(t.get("amount") or 0) for t in priced_rows) / total_amt
            res["price"] = round(wavg, 2)

        res["price_transaction"] = [
            {
                "date": (t.get("date_iso") or _en_date_to_iso(t.get("date"))),  # ISO date from the document
                "type": t.get("type"),
                "price": float(t.get("price")) if t.get("price") is not None else None,
                "amount_transacted": int(t.get("amount") or 0),
            }
            for t in (buy_sell or transfers or [])
            if int(t.get("amount") or 0) > 0
        ]

        # Combined flags
        res["is_transfer"] = res.get("is_transfer", False) or res["has_transfer"]


    def _alert_symbol_mismatch(
        self,
        filename: str,
        raw: str,
        canon: str,
        sym_from_name: Optional[str],
        sym_doc: Optional[str],
    ) -> None:
        """
        Emit an inserted warning when company name and symbol combination
        looks suspicious but still parseable.
        """
        self._parser_warn(
            code="symbol_name_mismatch",
            filename=filename,
            reasons=[
                {
                    "scope": "parser",
                    "code": "symbol_name_mismatch",
                    "message": "Possible mismatch between company name and symbol in IDX document.",
                    "details": {
                        "company_name_raw": raw,
                        "company_name_canonical": canon,
                        "symbol_from_name": sym_from_name,
                        "symbol_in_doc": sym_doc,
                        "announcement": self._current_alert_context,
                    },
                }
            ],
        )

    def validate_parsed_data(self, d: Dict[str, Any]) -> bool:
        if d.get("skip_filing"):
            return False

        all_zero = (
            not d.get("holder_name")
            and (d.get("holding_before", 0) == 0)
            and (d.get("holding_after", 0) == 0)
            and (d.get("share_percentage_before", 0.0) == 0.0)
            and (d.get("share_percentage_after", 0.0) == 0.0)
        )
        if all_zero:
            return False

        has_old_tx = bool(d.get("transactions"))
        has_new_tx = bool(d.get("price_transaction"))

        return has_old_tx or has_new_tx
