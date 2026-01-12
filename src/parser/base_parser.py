import os, json, logging
from datetime import datetime
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import pdfplumber

from services.alert.schema import build_alert
from src.parser.utils.company_resolver import (
    load_symbol_to_name_from_file,
    build_reverse_map,
)

from config import (
    ALERTS_OUTPUT_DIR,
    ALERTS_INSERTED_FILENAME,
    ALERTS_NOT_INSERTED_FILENAME,
)

logger = logging.getLogger(__name__)

# PDFMiner noise control
# pdfminer loggers that often emit noise (seek/xref/nextline/nexttoken)
_PDFMINER_LOGGERS = [
    "pdfminer",
    "pdfminer.psparser",
    "pdfminer.pdfparser",
    "pdfminer.pdfdocument",
    "pdfminer.pdfinterp",
    "pdfminer.pdfpage",
    "pdfminer.cmapdb",
    "pdfminer.layout",
    "pdfminer.image",
    "pdfminer.converter",
    "pdfminer.pdfdevice",
    "pdfminer.utils",
]

def _basic_root_config():
    """Configure root logging if no handler exists yet (avoid double config)."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )

def silence_pdfminer(level: int = logging.WARNING) -> None:
    """Lower pdfminer logger levels and disable propagation to keep logs quiet."""
    for name in _PDFMINER_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.propagate = False

class _PdfMinerChatterFilter(logging.Filter):
    """Filter out very trivial pdfminer messages as an extra layer."""
    NOISE = ("seek:", "find_xref", "xref found", "nextline:", "nexttoken:", "read_xref_from")
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(x in msg for x in self.NOISE)

def init_logging(pdf_debug: Optional[bool] = None) -> None:
    """
    Initialize logging and control pdfminer noise.
    - If pdf_debug is None, read ENV PDF_DEBUG (1/true/on).
    - When pdf_debug is False (default), silence pdfminer to WARNING + apply filter.
    """
    _basic_root_config()

    if pdf_debug is None:
        env = os.getenv("PDF_DEBUG", "0").strip().lower()
        pdf_debug = env in ("1", "true", "yes", "on")

    if not pdf_debug:
        silence_pdfminer(logging.WARNING)
        logging.getLogger("pdfminer").addFilter(_PdfMinerChatterFilter())

    # Reduce noise from other common libs
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)


# Base Parser
class BaseParser(ABC):
    """Base class for PDF parsers."""

    def __init__(
        self,
        pdf_folder: str,
        output_file: str,
        announcement_json: Optional[str] = None,
    ):
        # Ensure logging control is active as early as possible (honor PDF_DEBUG env)
        init_logging(pdf_debug=None)

        self.pdf_folder = pdf_folder
        self.output_file = output_file
        self.announcement_json = announcement_json

        os.makedirs(os.path.dirname(self.output_file), exist_ok=True)
        self._alerts_inserted: List[Dict[str, Any]] = []
        self._alerts_not_inserted: List[Dict[str, Any]] = []

        # Determine per-day alert files (v2 unified)
        today = datetime.today().date().isoformat()  # "YYYY-MM-DD"

        # Final paths (e.g., artifacts/alerts_inserted_2025-11-14.json)
        self._alerts_inserted_file = os.path.join(
            ALERTS_OUTPUT_DIR,
            ALERTS_INSERTED_FILENAME.format(date=today),
        )
        self._alerts_not_inserted_file = os.path.join(
            ALERTS_OUTPUT_DIR,
            ALERTS_NOT_INSERTED_FILENAME.format(date=today),
        )

        # Optional: parser_type, can be set in subclasses (idx / non_idx)
        self.parser_type: Optional[str] = getattr(self, "parser_type", None)

        # current context for the file being parsed (announcement, urls, etc.)
        self._current_alert_context: Dict[str, Any] = {}

        self.symbol_to_name: Dict[str, str] = load_symbol_to_name_from_file() or {}
        self.rev_company_map: Dict[str, List[str]] = build_reverse_map(self.symbol_to_name)
        self.company_names: List[str] = sorted({
            (name or "").strip() for name in self.symbol_to_name.values() if name
        })

        if self.company_names:
            logger.info(f"Loaded {len(self.company_names)} company names from company_map.json")
        else:
            logger.warning("No company names loaded. Check data/company/company_map.json or env COMPANY_MAP_FILE")

    def build_pdf_mapping(self) -> Dict[str, Any]:
        """Build mapping from PDF files to announcement metadata."""
        if not self.announcement_json or not os.path.exists(self.announcement_json):
            return {}

        try:
            with open(self.announcement_json, "r", encoding="utf-8") as f:
                announcements = json.load(f)
        except Exception as e:
            logger.error(f"Error loading announcement JSON: {e}")
            return {}

        file_to_announcement: Dict[str, Any] = {}

        for ann in announcements:
            main_link = ann.get("main_link", "")
            if main_link:
                main_file = os.path.basename(main_link.strip())
                file_to_announcement[main_file.lower()] = ann

            for attachment in ann.get("attachments", []):
                filename = attachment.get("filename", "").strip()
                url = attachment.get("url", "").strip()

                if filename:
                    file_to_announcement[filename] = ann
                if url:
                    file_from_url = os.path.basename(url)
                    file_to_announcement[file_from_url] = ann

        return file_to_announcement

    def extract_text_from_pdf(self, filepath: str) -> Optional[str]:
        """Extract text from PDF file."""
        try:
            with pdfplumber.open(filepath) as pdf:
                logger.debug(f"Opened {filepath} with {len(pdf.pages)} pages")
                text_parts: List[str] = []
                for page in pdf.pages:
                    try:
                        page_text = page.extract_text()
                    except Exception as e:
                        logger.warning(f"extract_text error on {os.path.basename(filepath)} page {page.page_number}: {e}")
                        page_text = None
                    if page_text:
                        text_parts.append(page_text)
                text = "\n".join(text_parts)
                return text.strip() if text and text.strip() else None
        except Exception as e:
            logger.error(f"Error extracting text from {filepath}: {e}")
            return None

    @abstractmethod
    def parse_single_pdf(self, filepath: str, filename: str, pdf_mapping: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a single PDF file. Must be implemented by subclasses."""
        pass

    @abstractmethod
    def validate_parsed_data(self, data: Dict[str, Any]) -> bool:
        """Validate parsed data. Must be implemented by subclasses."""
        pass

    def save_debug_output(self, filename: str, text: str):
        """Save extracted text for debugging."""
        debug_dir = "debug_output"
        os.makedirs(debug_dir, exist_ok=True)
        debug_file = os.path.join(debug_dir, f"{filename}.txt")

        try:
            with open(debug_file, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            logger.error(f"Error saving debug output for {filename}: {e}")


    # Parser alert helpers (v2)
    def _build_parser_alert(
        self,
        *,
        category: str,  # "inserted" | "not_inserted"
        code: str,
        filename: str,
        reasons: Optional[List[Dict[str, Any]]] = None,
        ctx: Optional[Dict[str, Any]] = None,
        severity: Optional[str] = None,
        needs_review: bool = True,
    ) -> Dict[str, Any]:
        """
        Construct a v2 alert for the parser stage, enriched with current announcement context.
        """
        # pdf mapping dict key=filename data=ingestion.json
        ann = self._current_alert_context or {}

        doc_url = (
            ann.get("url")
            or ann.get("download_url")
            or ann.get("attachment_url")
            or ann.get("main_link")
        )
        doc_title = ann.get("title") or ann.get("announcement_title")

        ctx = ctx or {}
        if "parser_type" not in ctx:
            ctx["parser_type"] = getattr(self, "parser_type", None)

        return build_alert(
            category=category,
            stage="parser",
            code=code,
            doc_filename=filename,
            context_doc_url=doc_url,
            context_doc_title=doc_title,
            announcement=ann,
            reasons=reasons,
            ctx=ctx,
            severity=severity,
            needs_review=needs_review,
        )

    def _parser_warn(
        self,
        *,
        code: str,
        filename: str,
        reasons: Optional[List[Dict[str, Any]]] = None,
        ctx: Optional[Dict[str, Any]] = None,
        severity: Optional[str] = None,
        needs_review: bool = True,
    ) -> None:
        """
        Non-fatal alert: the document/rows are still inserted, but need review.
        """
        alert = self._build_parser_alert(
            category="inserted",
            code=code,
            filename=filename,
            reasons=reasons,
            ctx=ctx,
            severity=severity,
            needs_review=needs_review,
        )
        self._alerts_inserted.append(alert)

    def _parser_fail(
        self,
        *,
        code: str,
        filename: str,
        reasons: Optional[List[Dict[str, Any]]] = None,
        ctx: Optional[Dict[str, Any]] = None,
        severity: Optional[str] = None,
    ) -> None:
        """
        Fatal alert: the document cannot be processed/inserted at all.
        """
        alert = self._build_parser_alert(
            category="not_inserted",
            code=code,
            filename=filename,
            reasons=reasons,
            ctx=ctx,
            severity=severity,
            needs_review=True,
        )
        self._alerts_not_inserted.append(alert)

    def _flush_parser_alerts(self) -> None:
        """
        Write parser alerts to alerts_inserted_parser.json / alerts_not_inserted_parser.json.
        """
        try:
            if self._alerts_inserted:
                os.makedirs(os.path.dirname(self._alerts_inserted_file), exist_ok=True)
                tmp = self._alerts_inserted_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._alerts_inserted, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._alerts_inserted_file)

            if self._alerts_not_inserted:
                os.makedirs(os.path.dirname(self._alerts_not_inserted_file), exist_ok=True)
                tmp = self._alerts_not_inserted_file + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._alerts_not_inserted, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._alerts_not_inserted_file)
        except Exception as e:
            logger.error(f"Error saving parser alerts: {e}")


    def parse_folder(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.pdf_folder):
            logger.error(f"Folder not found: {self.pdf_folder}")
            return []

        try:
            with open(self.output_file, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error pre-reset output file {self.output_file}: {e}")

        parsed_results: List[Dict[str, Any]] = []
        pdf_mapping = self.build_pdf_mapping()
        # print(f'\nraw pdf_mapping output: {pdf_mapping}\n')

        pdf_files = [f for f in os.listdir(self.pdf_folder) if f.lower().endswith(".pdf")]

        logger.info(f"Found {len(pdf_files)} PDF files to process")

        for filename in pdf_files:
            filepath = os.path.join(self.pdf_folder, filename)
            logger.info(f"Processing {filename}...")

            ann_ctx = pdf_mapping.get(filename, {}) or {}
            self._current_alert_context = ann_ctx

            try:
                result = self.parse_single_pdf(filepath, filename, pdf_mapping)
                # Skip from new parser if shares unchanged
                if result is None:
                    continue 

                items = []
                if isinstance(result, (list, tuple)): 
                    items = [record for record in result if record is not None]
                else: 
                    items = [result]

                for item in items:
                    if self.validate_parsed_data(item):
                        parsed_results.append(item)
                        logger.info(f"Successfully parsed {filename}")
                    
                    else:
                        if not (isinstance(item, dict) and item.get("skip_filing")):
                            self._parser_warn(
                                code="validation_failed",
                                filename=filename,
                                reasons=[
                                    {
                                        "scope": "parser",
                                        "code": "validation_failed",
                                        "message": "Parsed result failed validate_parsed_data check.",
                                        "details": {
                                            "filename": filename,
                                            "result_type": type(item).__name__,
                                        },
                                    }
                                ],
                                needs_review=True,
                            )

            except Exception as error:
                logger.error(f"Error processing {filename}: {error}")
                self._parser_warn(
                    code="parse_exception",
                    filename=filename,
                    ctx={"announcement": ann_ctx, "message": str(error)},
                    needs_review=True,
                )

        # Save results (overwrite)
        self.save_results(parsed_results)

        # Save v2 parser alerts
        self._flush_parser_alerts()

        logger.info(f"Processing complete. {len(parsed_results)} files successfully parsed")
        return parsed_results

    def save_results(self, results: List[Dict[str, Any]]):
        """Save parsing results to output file (overwrite)."""
        try:
            with open(self.output_file, "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved {len(results)} results to {self.output_file}")
        except Exception as e:
            logger.error(f"Error saving results: {e}")
