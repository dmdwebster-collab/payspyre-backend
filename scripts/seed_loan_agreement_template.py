"""Load the real loan-agreement template into the DB from a LOCAL .docx.

WHY THIS SCRIPT EXISTS
----------------------
This backend repository is PUBLIC. The platform owner's default loan agreement
(a .docx he supplies) is confidential and proprietary, so its text must never be
committed. But the agreement-preview QC step
(``GET /admin/applications/{id}/documents/agreement-preview``) needs the real
template to render the real document.

So the template lives in the DATABASE, not in git. This script is the loader:
it reads the .docx from a path YOU supply on the machine that holds it,
converts it, and writes it into ``platform_document_templates`` as a new
version of kind ``loan_agreement``. Nothing it reads is ever written back to
the repository.

Until this has been run, the preview falls back to the generic terms data sheet
in ``app/services/application_agreement_preview.BUILTIN_QC_SKELETON_HTML``
(headings + merge fields only, no contract text) and says so via
``template_source: "builtin_skeleton"``.

WHAT IT CONVERTS
----------------
* Word merge fields ``«FieldName»``  ->  the engine's ``{{FieldName}}``.
  The .docx's field NAMES are preserved verbatim, so they line up with
  ``application_agreement_preview.AGREEMENT_MERGE_FIELDS`` with no renaming.
* The repeating amortization block
  ``«TableStart:Schedule»…«TableEnd:Schedule»``  ->  ``{{Rows:Schedule}}``,
  which the preview expands into one ``<tr>`` per installment. Dave's own
  header and Totals rows on that table are preserved.
* Paragraphs -> ``<p>``, Word tables -> ``<table>``. Character-level formatting
  (bold/italic/fonts) is NOT carried over — this produces a clean structural
  HTML template. Refine it afterwards through the normal admin surface
  (``POST /admin/document-templates``), which versions every edit.

USAGE
-----
    source .venv/bin/activate
    python scripts/seed_loan_agreement_template.py \\
        --docx "/path/to/loan-agreement.docx" \\
        --title "PaySpyre Loan Agreement v<n>"

    # See the converted HTML without touching the database:
    python scripts/seed_loan_agreement_template.py --docx <path> --dry-run

Re-running creates a NEW version (append-only, like every template edit);
existing versions are never modified. Use ``--deactivate-previous`` to pull the
older versions out of resolution at the same time.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import zipfile
from typing import Iterator, Optional
from xml.etree import ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Word merge field: «FieldName» -> {{FieldName}}
_MERGE_FIELD_RE = re.compile(r"«\s*([A-Za-z0-9_]+)\s*»")
#: The repeat-block markers Word/Aspose mail-merge uses for a table region.
_TABLE_START_RE = re.compile(r"«\s*TableStart:([A-Za-z0-9_]+)\s*»")
#: Aspose renders a broken merge field as this; the source .docx has one in the
#: schedule's "Remaining Principal Balance" cell. Strip it — the preview
#: computes that column itself.
_SYNTAX_ERROR_RE = re.compile(r"\$?!?\s*Syntax Error,?\s*«?")


def _paragraph_text(node: ET.Element) -> str:
    """All run text in a paragraph, joined (a merge field can be split across
    runs, so this must be joined BEFORE the «…» substitution)."""
    return "".join(t.text or "" for t in node.iter(W + "t"))


def _to_placeholders(text: str) -> str:
    """HTML-escape literal text, then turn «Field» into {{Field}}."""
    text = _SYNTAX_ERROR_RE.sub("", text)
    return _MERGE_FIELD_RE.sub(r"{{\1}}", html.escape(text))


def _row_cells(row: ET.Element) -> Iterator[ET.Element]:
    for cell in row:
        if cell.tag == W + "tc":
            yield cell


def _convert_table(tbl: ET.Element) -> str:
    """One Word table -> ``<table>``.

    A row carrying ``«TableStart:Name»`` is the mail-merge repeat block: it is
    replaced wholesale by ``{{Rows:Name}}`` (the preview emits the ``<tr>``\\ s),
    and Dave's header/Totals rows around it survive untouched.
    """
    out: list[str] = ['<table class="agreement-table">']
    for row in tbl:
        if row.tag != W + "tr":
            continue
        raw = "".join(_paragraph_text(p) for p in row.iter(W + "p"))
        start = _TABLE_START_RE.search(raw)
        if start is not None:
            out.append("{{Rows:%s}}" % start.group(1))
            continue
        cells = []
        for cell in _row_cells(row):
            body = " ".join(
                _to_placeholders(_paragraph_text(p)).strip()
                for p in cell.iter(W + "p")
            ).strip()
            cells.append(f"<td>{body}</td>")
        if cells:
            out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _style_of(p: ET.Element) -> str:
    style = p.find(f"{W}pPr/{W}pStyle")
    return (style.get(W + "val") or "") if style is not None else ""


def convert_docx_to_template_html(path: str) -> str:
    """Convert a .docx into an HTML body with ``{{MergeField}}`` placeholders."""
    with zipfile.ZipFile(path) as zf:
        document_xml = zf.read("word/document.xml")
    # nosec B314 — a .docx the operator supplies from their own machine, parsed
    # by a local admin script. stdlib ElementTree does not resolve external
    # entities, so there is no XXE surface here and no new dependency is worth it.
    root = ET.fromstring(document_xml)  # nosec B314
    body = root.find(W + "body")
    if body is None:
        raise SystemExit(f"{path}: no <w:body> — is this a Word document?")

    parts: list[str] = []
    for node in body:
        if node.tag == W + "p":
            text = _to_placeholders(_paragraph_text(node)).strip()
            if not text:
                continue
            style = _style_of(node).lower()
            if style.startswith("heading1") or style == "title":
                parts.append(f"<h1>{text}</h1>")
            elif style.startswith("heading"):
                parts.append(f"<h2>{text}</h2>")
            else:
                parts.append(f"<p>{text}</p>")
        elif node.tag == W + "tbl":
            parts.append(_convert_table(node))
    return "\n".join(parts)


def _next_version(db, kind: str) -> int:
    from sqlalchemy import func

    from app.models.platform.document_template import PlatformDocumentTemplate

    current = (
        db.query(func.max(PlatformDocumentTemplate.version))
        .filter(
            PlatformDocumentTemplate.kind == kind,
            PlatformDocumentTemplate.scope == "global",
        )
        .scalar()
    )
    return (current or 0) + 1


def seed(
    docx_path: str,
    *,
    title: str,
    description: Optional[str],
    deactivate_previous: bool,
) -> None:
    from app.db.base import SessionLocal
    from app.models.platform.document_template import PlatformDocumentTemplate

    body_html = convert_docx_to_template_html(docx_path)
    db = SessionLocal()
    try:
        version = _next_version(db, "loan_agreement")
        if deactivate_previous:
            db.query(PlatformDocumentTemplate).filter(
                PlatformDocumentTemplate.kind == "loan_agreement",
                PlatformDocumentTemplate.scope == "global",
                PlatformDocumentTemplate.active.is_(True),
            ).update({"active": False}, synchronize_session=False)
        db.add(
            PlatformDocumentTemplate(
                kind="loan_agreement",
                scope="global",
                version=version,
                title=title,
                description=description,
                body_html=body_html,
                active=True,
            )
        )
        db.commit()
    finally:
        db.close()
    print(
        f"Loaded loan_agreement template v{version} "
        f"({len(body_html):,} chars) from {os.path.basename(docx_path)}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docx", required=True, help="Local path to the loan-agreement .docx"
    )
    parser.add_argument("--title", default="PaySpyre Loan Agreement")
    parser.add_argument("--description", default=None)
    parser.add_argument(
        "--deactivate-previous",
        action="store_true",
        help="Deactivate existing active global loan_agreement versions",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the converted HTML to stdout; touch no database",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.docx):
        raise SystemExit(f"No such file: {args.docx}")
    if args.dry_run:
        print(convert_docx_to_template_html(args.docx))
        return
    seed(
        args.docx,
        title=args.title,
        description=args.description,
        deactivate_previous=args.deactivate_previous,
    )


if __name__ == "__main__":
    main()
