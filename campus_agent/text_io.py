from __future__ import annotations

from pathlib import Path


SUPPORTED_SUFFIXES = {".txt", ".pdf", ".docx", ".xlsx", ".json", ".jsonl", ".md"}


def fix_mojibake(text: str) -> str:
    """Repair common UTF-8 text decoded as GBK, often seen in copied group notices."""
    if not text:
        return ""
    try:
        candidate = text.encode("gbk", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text

    def score(value: str) -> int:
        common = sum(value.count(ch) for ch in "通知时间地点提交报名截止活动学生学院")
        chinese = sum("\u4e00" <= ch <= "\u9fff" for ch in value)
        replacement = value.count("�")
        return common * 8 + chinese - replacement * 20

    return candidate if score(candidate) > score(text) else text


def read_text_file(path: Path, max_pdf_pages: int = 12) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return fix_mojibake(path.read_text(encoding="utf-8", errors="ignore"))
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = reader.pages[:max_pdf_pages]
        return fix_mojibake("\n".join(page.extract_text() or "" for page in pages))
    if suffix == ".docx":
        from docx import Document

        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), data_only=True, read_only=True)
        rows: list[str] = []
        for sheet in workbook.worksheets[:5]:
            rows.append(f"【工作表】{sheet.title}")
            for row in sheet.iter_rows(max_row=80, values_only=True):
                values = [str(value).strip() for value in row if value is not None and str(value).strip()]
                if values:
                    rows.append(" | ".join(values))
        return "\n".join(rows)
    if suffix in {".json", ".jsonl"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    return ""


def collect_documents(root: Path) -> list[dict[str, str]]:
    docs: list[dict[str, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            text = read_text_file(path)
        except Exception as exc:
            text = f"[读取失败] {exc}"
        docs.append(
            {
                "path": str(path),
                "name": path.name,
                "category": path.parent.name,
                "suffix": path.suffix.lower(),
                "text": text,
                "chars": str(len(text)),
            }
        )
    return docs
