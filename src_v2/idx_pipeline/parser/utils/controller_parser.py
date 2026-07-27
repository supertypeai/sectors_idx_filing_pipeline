import fitz
import re 


def open_pdf(pdf_path: str) -> str:
    with fitz.open(pdf_path) as document:
        return "\n".join(page.get_text() for page in document)


def parse_controller(pdf_path: str) -> bool | None:
    pdf_text = open_pdf(pdf_path)
    normalized_text = re.sub(r"\s+", " ", pdf_text).lower()

    # Some old IDX PDFs extract "Pengendali" as "Pengedali". Search by
    # contents because production filenames do not always identify the layout.
    old_layout_match = re.search(
        r"status\s+penge(?:n)?dali\s+(bukan\s+penge(?:n)?dali|penge(?:n)?dali)\b",
        normalized_text,
    )

    if old_layout_match:
        return not old_layout_match.group(1).startswith("bukan")

    new_layout_match = re.search(
        r"keterangan\s+pengendali\s*:\s*(ya|tidak)\b",
        normalized_text,
    )

    if new_layout_match:
        return new_layout_match.group(1) == "ya"

    return None 
