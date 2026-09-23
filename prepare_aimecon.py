# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml", "pypandoc-binary", "pypdf"]
# ///
"""
Prepare aclpub2 input directories for the three AIME-Con proceedings volumes.

Run from anywhere with uv (no environment setup needed):

    uv run prepare_aimecon.py <input_dir> \
        --start-date YYYY-MM-DD --end-date YYYY-MM-DD --location "..."

Example:

    uv run prepare_aimecon.py aimecon2026 \
        --start-date 2026-10-26 --end-date 2026-10-28 \
        --location "Wyndham Grand Pittsburgh, Downtown, Pittsburgh, PA"

Expected files in <input_dir> (all provided by the conference organizers):

    Vol1.csv, Vol2.csv, Vol3.csv       paper metadata; wide author columns
                                       "AuthorN First/Middle/Last/Affiliation/Email"
    Vol1/, Vol2/, Vol3/                camera-ready PDFs named Paper-<id>.pdf
    full-papers-preface.md             preface for Volume 1 (markdown, no title)
    wip-papers-preface.md              preface for Volume 2
    coordinated-session-papers-preface.md  preface for Volume 3
    sponsor_logos/<tier>/*.png         tier is one of platinum, gold, silver, supporter
    sponsor_ads.pdf                    sponsor advertisements, inserted after the logos
    isbn.csv                           header volume,isbn with rows
                                       full,<isbn> / wip,<isbn> / coordinated,<isbn>
    aimecon-logo.png                   conference logo, shown at the top of each preface
    ncme-leadership.csv                firstname,lastname,credentials,role
    organizing-committee.csv           firstname,lastname,credentials,institution,role
    full-papers-reviewers.csv          firstname,lastname,institution
    wip-papers-reviewers.csv           firstname,lastname,institution
    coordinated-papers-reviewers.csv   firstname,lastname,institution

Writes <input_dir>/volume1, volume2, volume3, each a complete aclpub2 input
directory ready for:

    python generate <input_dir>/volume<N> --proceedings --overwrite --outdir output/volume<N>

Exits nonzero if any required input is missing or a CSV row has no matching
PDF. PDFs without a matching CSV row are excluded with a warning.

Before generating anything, every source paper PDF is scanned with PAX (the
annotation extractor `python generate` uses) and any PDF that PAX's old
PDFBox cannot parse is rebuilt in place. The rebuild is a pypdf read/write
round trip that normalizes the internal structure without changing the
content; page count and extracted text are verified identical before the
original is replaced. Requires a JDK on the PATH. PDFs that still fail after
the rebuild are reported as errors and must be re-exported manually (e.g.
via Preview or Acrobat). Pass --skip-repair-pdfs to skip this scan.
"""

import argparse
import csv
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pypandoc
import yaml

EVENT_NAME = "Artificial Intelligence in Measurement and Education Conference (AIME-Con)"
ANTHOLOGY_VENUE_ID = "AIME-Con"
PUBLISHER = "National Council on Measurement in Education (NCME)"
NCME_PROCEEDINGS_ADDRESS = (
    "& National Council on Measurement in Education (NCME)\\\\\n"
    "& 520 S. Walnut St. Box 2388\\\\\n"
    "& Bloomington, IN 47402\\\\\n"
    "& USA\\\\\n"
    "& Tel: +1-812-245-8096\\\\\n"
    "&{\\tt ncme@ncme.org}\\\\"
)

SPONSOR_TIERS = [
    ("platinum", "Platinum"),
    ("gold", "Gold"),
    ("silver", "Silver"),
    ("supporter", "Supporter"),
]

LEADERSHIP_CSV = "ncme-leadership.csv"
ORGANIZING_CSV = "organizing-committee.csv"
LOGO_PNG = "aimecon-logo.png"
SPONSOR_ADS_PDF = "sponsor_ads.pdf"
ISBN_CSV = "isbn.csv"


@dataclass(frozen=True)
class Volume:
    number: int
    isbn_key: str
    title_suffix: str
    volume_name: str
    papers_csv: str
    papers_dir: str
    preface_md: str
    reviewers_csv: str

    @property
    def cover_subtitle(self) -> str:
        return f"Volume {self.number}: {self.title_suffix}"


VOLUMES = [
    Volume(1, "full", "Full Papers", "main", "Vol1.csv", "Vol1",
           "full-papers-preface.md", "full-papers-reviewers.csv"),
    Volume(2, "wip", "Works in Progress", "wip", "Vol2.csv", "Vol2",
           "wip-papers-preface.md", "wip-papers-reviewers.csv"),
    Volume(3, "coordinated", "Coordinated Session Papers", "sessions",
           "Vol3.csv", "Vol3", "coordinated-session-papers-preface.md",
           "coordinated-papers-reviewers.csv"),
]


class Reporter:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    def print_and_exit_if_errors(self):
        for warning in self.warnings:
            print(f"WARNING: {warning}")
        if self.errors:
            print("\nERRORS:")
            for error in self.errors:
                print(f"  - {error}")
            sys.exit(1)


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def collapse_whitespace(value):
    if value is None:
        return ""
    return " ".join(value.split())


def titleize_name(name):
    """Fix all-caps name tokens ("TIANQI LANG" -> "Tianqi Lang") while leaving
    mixed-case names and initials ("J.R.", "von Davier") untouched."""
    tokens = []
    for token in name.split():
        if len(token) > 1 and token.isupper() and "." not in token:
            token = "-".join(part.capitalize() for part in token.split("-"))
        tokens.append(token)
    return " ".join(tokens)


def format_credentials(credentials):
    credentials = collapse_whitespace(credentials)
    if credentials and not credentials.endswith("."):
        credentials += "."
    return credentials


def committee_member(row, with_institution=True):
    """Build a member entry. Credentials are appended to the last name,
    e.g. last_name: "Wilson, Ph.D."."""
    member = {
        "first_name": titleize_name(collapse_whitespace(row["firstname"])),
        "last_name": titleize_name(collapse_whitespace(row["lastname"])),
    }
    credentials = format_credentials(row.get("credentials", ""))
    if credentials:
        member["last_name"] += f", {credentials}"
    if with_institution:
        institution = collapse_whitespace(row.get("institution", ""))
        if institution:
            member["institution"] = institution
    return member


def _literal_block_strings(dumper, data):
    # Dump multi-line strings (e.g. proceedings_address) as literal blocks so
    # the newlines survive the round trip through yaml.safe_load.
    if isinstance(data, str) and "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


yaml.add_representer(str, _literal_block_strings)


def dump_yaml(data, path):
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, width=120)


def check_expected_inputs(input_dir, reporter):
    expected_files = [ISBN_CSV, LOGO_PNG, SPONSOR_ADS_PDF, LEADERSHIP_CSV, ORGANIZING_CSV]
    for volume in VOLUMES:
        expected_files += [volume.papers_csv, volume.preface_md, volume.reviewers_csv]
    for file_name in expected_files:
        if not Path(input_dir, file_name).is_file():
            reporter.error(f"missing required input file: {file_name}")
    for volume in VOLUMES:
        if not Path(input_dir, volume.papers_dir).is_dir():
            reporter.error(f"missing required input directory: {volume.papers_dir}/")
    sponsor_logos_dir = Path(input_dir, "sponsor_logos")
    if not sponsor_logos_dir.is_dir():
        reporter.error("missing required input directory: sponsor_logos/")
    else:
        for tier_dir, _ in SPONSOR_TIERS:
            if not Path(sponsor_logos_dir, tier_dir).is_dir():
                reporter.error(f"missing sponsor tier directory: sponsor_logos/{tier_dir}/")


def parse_isbns(input_dir, reporter):
    # isbn.csv has a header: volume,isbn with rows
    # full,<isbn1> / wip,<isbn2> / coordinated,<isbn3>
    isbns = {}
    for row in read_csv_rows(Path(input_dir, ISBN_CSV)):
        volume_key = collapse_whitespace(row.get("volume", ""))
        isbn = collapse_whitespace(row.get("isbn", ""))
        if not volume_key or not isbn:
            reporter.error(f"{ISBN_CSV} has a malformed row: {dict(row)}")
            continue
        isbns[volume_key] = isbn
    expected_keys = {volume.isbn_key for volume in VOLUMES}
    missing = expected_keys - set(isbns)
    extra = set(isbns) - expected_keys
    if missing:
        reporter.error(f"isbn.csv is missing rows for: {', '.join(sorted(missing))}")
    if extra:
        reporter.warn(f"isbn.csv has unexpected rows that will be ignored: {', '.join(sorted(extra))}")
    return isbns


def pax_classpath(reporter):
    jar_dir = Path(__file__).resolve().parent / "aclpub2"
    if not (jar_dir / "pax.jar").is_file() or not (jar_dir / "pdfbox.jar").is_file():
        reporter.error(f"PDF repair scan needs pax.jar and pdfbox.jar in {jar_dir}")
        return None
    if shutil.which("java") is None:
        reporter.error("PDF repair scan needs a JDK on the PATH (java not found)")
        return None
    return f"{jar_dir / 'pax.jar'}:{jar_dir / 'pdfbox.jar'}"


def pax_accepts(pdf_path, classpath):
    """True if PAX can parse the PDF. Any .pax file written next to the PDF
    is removed; `python generate` recreates it next to the copied PDF."""
    result = subprocess.run(
        ["java", "-cp", classpath, "pax.PDFAnnotExtractor", str(pdf_path)],
        capture_output=True)
    pdf_path.with_suffix(".pax").unlink(missing_ok=True)
    return result.returncode == 0


def rebuild_pdf(pdf_path):
    """Rewrite the PDF with pypdf, which normalizes the internal page tree.
    clone_document_from_reader preserves the document catalog, including the
    /Names name tree that PAX needs to resolve named link destinations; a
    plain page-by-page copy drops it and PAX then skips those links. Returns
    True if the rebuild preserved page count and extracted text."""
    from pypdf import PdfReader, PdfWriter

    before = PdfReader(str(pdf_path))
    text_before = "".join(page.extract_text() or "" for page in before.pages)
    writer = PdfWriter()
    writer.clone_document_from_reader(before)
    with open(pdf_path, "wb") as f:
        writer.write(f)
    after = PdfReader(str(pdf_path))
    text_after = "".join(page.extract_text() or "" for page in after.pages)
    return len(after.pages) == len(before.pages) and text_after == text_before


def repair_broken_pdfs(input_dir, reporter):
    classpath = pax_classpath(reporter)
    reporter.print_and_exit_if_errors()

    pdfs = []
    for volume in VOLUMES:
        pdfs += sorted(Path(input_dir, volume.papers_dir).glob("Paper-*.pdf"))

    print(f"Scanning {len(pdfs)} paper PDFs with PAX...")
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        broken = [pdf for pdf, ok in zip(pdfs, pool.map(
            lambda p: pax_accepts(p, classpath), pdfs)) if not ok]
    if not broken:
        print("All paper PDFs passed.\n")
        return

    for pdf in broken:
        if rebuild_pdf(pdf) and pax_accepts(pdf, classpath):
            print(f"repaired {pdf}")
        else:
            reporter.error(f"{pdf}: PAX cannot parse it and the automatic rebuild "
                           f"did not help; re-export it manually (e.g. via Preview "
                           f"or Acrobat) and run again")


def build_papers(volume, input_dir, out_dir, reporter):
    rows = read_csv_rows(Path(input_dir, volume.papers_csv))
    pdf_dir = Path(input_dir, volume.papers_dir)

    for required_column in ("Paper ID", "Title", "Abstract"):
        if rows and required_column not in rows[0]:
            reporter.error(f"{volume.papers_csv} is missing required column '{required_column}'")
            return []

    papers = []
    seen_ids = set()
    for row in rows:
        paper_id = collapse_whitespace(row["Paper ID"])
        if not paper_id.isdigit():
            reporter.error(f"{volume.papers_csv} has a row with invalid Paper ID: '{paper_id}'")
            continue
        pdf_path = Path(pdf_dir, f"Paper-{paper_id}.pdf")
        if not pdf_path.is_file():
            reporter.error(f"{volume.papers_csv}: no PDF found for Paper ID {paper_id} "
                           f"(expected {volume.papers_dir}/Paper-{paper_id}.pdf)")
            continue
        if paper_id in seen_ids:
            reporter.error(f"{volume.papers_csv}: duplicate Paper ID {paper_id}")
            continue
        seen_ids.add(paper_id)

        authors = []
        n = 1
        while f"Author{n} First" in row:
            first = collapse_whitespace(row.get(f"Author{n} First"))
            last = collapse_whitespace(row.get(f"Author{n} Last"))
            if first or last:
                author = {"first_name": to_latex(titleize_name(first)),
                          "last_name": to_latex(titleize_name(last))}
                middle = to_latex(titleize_name(collapse_whitespace(row.get(f"Author{n} Middle"))))
                affiliation = collapse_whitespace(row.get(f"Author{n} Affiliation"))
                email = collapse_whitespace(row.get(f"Author{n} Email"))
                if middle:
                    author["middle_name"] = middle
                if affiliation:
                    author["institution"] = affiliation
                if email:
                    author["email"] = email
                authors.append(author)
            n += 1
        if not authors:
            reporter.error(f"{volume.papers_csv}: Paper ID {paper_id} has no authors")
            continue

        title = to_latex(collapse_whitespace(row["Title"]))
        warn_unsafe_chars(title, f"{volume.papers_csv} paper {paper_id} title", reporter)
        papers.append({
            "id": paper_id,
            "title": title,
            "abstract": to_latex(collapse_whitespace(row["Abstract"])),
            "authors": authors,
            "file": f"{paper_id}.pdf",
            "_pdf_path": pdf_path,
        })

    papers.sort(key=lambda paper: int(paper["id"]))

    # Warn about PDFs that have no CSV row; they are excluded. seen_ids covers
    # every row with a valid ID, even rows later rejected for other reasons.
    csv_ids = seen_ids
    for pdf_path in sorted(pdf_dir.glob("Paper-*.pdf")):
        pdf_id = pdf_path.stem.removeprefix("Paper-")
        if pdf_id not in csv_ids:
            reporter.warn(f"{volume.papers_dir}/{pdf_path.name} has no row in "
                          f"{volume.papers_csv} and will be excluded")

    papers_out_dir = Path(out_dir, "papers")
    papers_out_dir.mkdir(parents=True)
    for paper in papers:
        shutil.copy2(paper.pop("_pdf_path"), Path(papers_out_dir, paper["file"]))
    dump_yaml(papers, Path(out_dir, "papers.yml"))
    return papers


def build_conference_details(volume, input_dir, out_dir, isbns, editors, args):
    book_title = f"Proceedings of the {EVENT_NAME}: {volume.title_suffix}"
    details = {
        "book_title": book_title,
        "event_name": EVENT_NAME,
        "volume_name": volume.volume_name,
        "cover_subtitle": volume.cover_subtitle,
        "anthology_venue_id": ANTHOLOGY_VENUE_ID,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "isbn": isbns[volume.isbn_key],
        "location": args.location,
        "editors": editors,
        "watermark_book_title": f"{book_title.split(':')[0]} -- {volume.cover_subtitle}",
        "publisher": PUBLISHER,
        "proceedings_address": NCME_PROCEEDINGS_ADDRESS,
        "sponsor_ads": SPONSOR_ADS_PDF,
    }
    dump_yaml(details, Path(out_dir, "conference_details.yml"))


# Unicode characters that pdflatex (T5 fontenc) cannot typeset directly,
# mapped to their LaTeX equivalents. Applied to prefaces (after pandoc) and
# to paper titles, abstracts, and author names (before aclpub2's own escaping,
# which leaves backslash commands untouched).
UNICODE_TO_LATEX = {
    # Greek lowercase
    "α": "$\\alpha$", "β": "$\\beta$", "γ": "$\\gamma$", "δ": "$\\delta$",
    "ε": "$\\epsilon$", "ζ": "$\\zeta$", "η": "$\\eta$", "θ": "$\\theta$",
    "ι": "$\\iota$", "κ": "$\\kappa$", "λ": "$\\lambda$", "μ": "$\\mu$",
    "ν": "$\\nu$", "ξ": "$\\xi$", "ο": "$o$", "π": "$\\pi$",
    "ρ": "$\\rho$", "ς": "$\\varsigma$", "σ": "$\\sigma$", "τ": "$\\tau$",
    "υ": "$\\upsilon$", "φ": "$\\phi$", "χ": "$\\chi$", "ψ": "$\\psi$",
    "ω": "$\\omega$",
    # Greek uppercase
    "Γ": "$\\Gamma$", "Δ": "$\\Delta$", "Θ": "$\\Theta$", "Λ": "$\\Lambda$",
    "Ξ": "$\\Xi$", "Π": "$\\Pi$", "Σ": "$\\Sigma$", "Υ": "$\\Upsilon$",
    "Φ": "$\\Phi$", "Ψ": "$\\Psi$", "Ω": "$\\Omega$",
    # Math and symbols
    "≤": "$\\leq$",
    "≥": "$\\geq$",
    "≠": "$\\neq$",
    "≈": "$\\approx$",
    "±": "$\\pm$",
    "×": "$\\times$",
    "÷": "$\\div$",
    "−": "--",  # minus sign
    "√": "$\\sqrt{}$",
    "∞": "$\\infty$",
    "∝": "$\\propto$",
    "∈": "$\\in$",
    "∂": "$\\partial$",
    "∇": "$\\nabla$",
    "→": "$\\rightarrow$",
    "←": "$\\leftarrow$",
    "↔": "$\\leftrightarrow$",
    "⇒": "$\\Rightarrow$",
    "…": "\\ldots{}",
    "•": "\\textbullet{}",
    "ˆ": "\\^{}",
    "˜": "\\textasciitilde{}",
}

# Non-ASCII characters that are known to typeset fine under the proceedings
# template's T5 font encoding: accented Latin letters, curly quotes, dashes.
KNOWN_SAFE_RE = re.compile(r"[À-ɏḀ-ỿ’‘“”‐‑–—]")


def to_latex(text):
    for char, replacement in UNICODE_TO_LATEX.items():
        text = text.replace(char, replacement)
    return text


def warn_unsafe_chars(text, where, reporter):
    for char in sorted(set(text)):
        if ord(char) > 127 and not KNOWN_SAFE_RE.match(char):
            reporter.warn(f"{where}: character {char!r} (U+{ord(char):04X}) may not "
                          f"typeset correctly; add it to UNICODE_TO_LATEX if pdflatex fails")


def build_preface(volume, input_dir, out_dir):
    markdown_text = Path(input_dir, volume.preface_md).read_text(encoding="utf-8")
    # Control characters from copy-pasting out of PDFs (form feeds in
    # particular) leak through pandoc, and TeX treats a form feed as \par,
    # which breaks paragraphs mid-sentence.
    markdown_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", markdown_text)
    # lists_without_preceding_blankline: the prefaces start bullet lists right
    # after a paragraph, which pandoc's default markdown does not parse.
    latex = pypandoc.convert_text(
        markdown_text, "latex", format="markdown+lists_without_preceding_blankline")
    # Headings in the preface should be unnumbered; the "Preface" title itself
    # is added by the proceedings template.
    latex = re.sub(r"\\(sub*)section\{", r"\\\1section*{", latex)
    # Unnumbered headings don't take labels; drop the ones pandoc adds.
    latex = re.sub(r"(\\sub\*?section\*?\{[^}]*\})\\label\{[^}]*\}", r"\1", latex)
    # \tightlist is defined by pandoc's own template, not by the aclpub2
    # proceedings template; it only tweaks list spacing, so drop it.
    latex = re.sub(r"^\\tightlist\n", "", latex, flags=re.MULTILINE)
    latex = to_latex(latex)

    logo_path = Path(out_dir, LOGO_PNG).resolve()
    preface_tex = (
        # Block paragraphs, no first-line indent. parskip keeps paragraphs
        # distinguishable; both settings are restored at the end so the rest
        # of the front matter is unaffected. \origparindent is defined by the
        # proceedings template.
        "\\setlength{\\parindent}{0pt}\n"
        "\\setlength{\\parskip}{0.6em}\n\n"
        "\\begin{center}\n"
        f"\\includegraphics[width=0.5\\textwidth]{{{logo_path}}}\n"
        "\\end{center}\n\n"
        "\\vspace{1em}\n\n"
        f"{latex}\n"
        "\\setlength{\\parindent}{\\origparindent}\n"
        "\\setlength{\\parskip}{0pt plus 1pt}\n"
    )
    prefaces_dir = Path(out_dir, "prefaces")
    prefaces_dir.mkdir()
    Path(prefaces_dir, "preface.tex").write_text(preface_tex, encoding="utf-8")
    dump_yaml([{"title": "Preface", "file": "preface.tex"}], Path(out_dir, "prefaces.yml"))


def load_committees(input_dir):
    leadership = read_csv_rows(Path(input_dir, LEADERSHIP_CSV))
    organizing = read_csv_rows(Path(input_dir, ORGANIZING_CSV))

    leadership_members = []
    for row in leadership:
        member = committee_member(row, with_institution=False)
        role = collapse_whitespace(row.get("role", ""))
        if role:
            member["last_name"] += f" ({role})"
        leadership_members.append(member)
    organizing_yml = [{"role": "NCME Leadership", "members": leadership_members}]
    roles_seen = []
    for row in organizing:
        role = collapse_whitespace(row["role"])
        if role not in roles_seen:
            roles_seen.append(role)
    for role in roles_seen:
        organizing_yml.append({
            "role": role,
            "members": [committee_member(row) for row in organizing
                        if collapse_whitespace(row["role"]) == role],
        })

    program_chairs = [committee_member(row) for row in organizing
                      if collapse_whitespace(row["role"]) == "Program Chairs"]

    editors = [{
        "first_name": titleize_name(collapse_whitespace(row["firstname"])),
        "last_name": titleize_name(collapse_whitespace(row["lastname"])),
    } for row in organizing if collapse_whitespace(row["role"]) == "Conference Chairs"]

    return organizing_yml, program_chairs, editors


def build_program_committee(volume, input_dir, out_dir, program_chairs, reporter):
    reviewers = []
    for row in read_csv_rows(Path(input_dir, volume.reviewers_csv)):
        first = titleize_name(collapse_whitespace(row.get("firstname")))
        last = titleize_name(collapse_whitespace(row.get("lastname")))
        if last == ".":
            last = ""
        if not first and not last:
            reporter.warn(f"{volume.reviewers_csv}: skipping row with no name")
            continue
        reviewer = {"first_name": first, "last_name": last}
        institution = collapse_whitespace(row.get("institution", ""))
        if institution:
            reviewer["institution"] = institution
        reviewers.append(reviewer)

    program_committee = [
        {"role": "Program Chairs", "entries": program_chairs},
        {"role": "Reviewers", "entries": reviewers},
    ]
    dump_yaml(program_committee, Path(out_dir, "program_committee.yml"))
    return len(reviewers)


def build_sponsors(input_dir, out_dir, reporter):
    sponsors_yml = []
    logos_out_dir = Path(out_dir, "sponsor_logos")
    logos_out_dir.mkdir()
    for tier_dir, tier_name in SPONSOR_TIERS:
        logos = sorted(p.name for p in Path(input_dir, "sponsor_logos", tier_dir).glob("*.png"))
        if not logos:
            reporter.warn(f"no logos found in sponsor_logos/{tier_dir}/, tier will be skipped")
            continue
        for logo in logos:
            shutil.copy2(Path(input_dir, "sponsor_logos", tier_dir, logo),
                         Path(logos_out_dir, logo))
        sponsors_yml.append({"tier": tier_name, "logos": logos})
    dump_yaml(sponsors_yml, Path(out_dir, "sponsors.yml"))


def main():
    parser = argparse.ArgumentParser(
        description="Prepare aclpub2 input directories for the three AIME-Con volumes.")
    parser.add_argument("input_dir", type=Path,
                        help="Directory containing the files provided by the conference organizers.")
    parser.add_argument("--start-date", required=True, help="Conference start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", required=True, help="Conference end date, YYYY-MM-DD.")
    parser.add_argument("--location", required=True, help="Conference location.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing volume<N>/ directories in the input directory.")
    parser.add_argument("--skip-repair-pdfs", action="store_true",
                        help="Skip the default scan that rebuilds, in place, any source "
                             "paper PDF that PAX cannot parse.")
    args = parser.parse_args()

    # Parse to datetime.date so the YAML round trip through safe_load in
    # aclpub2 produces date objects; the templates access start_date.year.
    for date_arg in ("start_date", "end_date"):
        try:
            setattr(args, date_arg.replace("-", "_"),
                    datetime.strptime(getattr(args, date_arg), "%Y-%m-%d").date())
        except ValueError:
            parser.error(f"dates must be YYYY-MM-DD, got '{getattr(args, date_arg)}'")

    input_dir = args.input_dir
    if not input_dir.is_dir():
        parser.error(f"input directory {input_dir} does not exist")

    reporter = Reporter()
    check_expected_inputs(input_dir, reporter)

    out_dirs = {volume: Path(input_dir, f"volume{volume.number}") for volume in VOLUMES}
    for volume, out_dir in out_dirs.items():
        if out_dir.exists() and any(out_dir.iterdir()) and not args.overwrite:
            reporter.error(f"output directory {out_dir} already exists and is not empty; "
                           f"pass --overwrite to regenerate it")
    reporter.print_and_exit_if_errors()

    if not args.skip_repair_pdfs:
        repair_broken_pdfs(input_dir, reporter)

    isbns = parse_isbns(input_dir, reporter)
    organizing_yml, program_chairs, editors = load_committees(input_dir)
    if not editors:
        reporter.error(f"no rows with role 'Conference Chairs' found in {ORGANIZING_CSV}; "
                       f"they are needed for the editors field")
    if not program_chairs:
        reporter.warn(f"no rows with role 'Program Chairs' found in {ORGANIZING_CSV}")
    reporter.print_and_exit_if_errors()

    print(f"Preparing volumes in {input_dir}\n")
    for volume in VOLUMES:
        out_dir = out_dirs[volume]
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        papers = build_papers(volume, input_dir, out_dir, reporter)
        build_conference_details(volume, input_dir, out_dir, isbns, editors, args)
        build_preface(volume, input_dir, out_dir)
        dump_yaml(organizing_yml, Path(out_dir, "organizing_committee.yml"))
        num_reviewers = build_program_committee(volume, input_dir, out_dir,
                                                program_chairs, reporter)
        build_sponsors(input_dir, out_dir, reporter)
        shutil.copy2(Path(input_dir, SPONSOR_ADS_PDF), Path(out_dir, SPONSOR_ADS_PDF))
        shutil.copy2(Path(input_dir, LOGO_PNG), Path(out_dir, LOGO_PNG))

        num_authors = sum(len(paper["authors"]) for paper in papers)
        print(f"volume{volume.number} ({volume.title_suffix}): "
              f"{len(papers)} papers, {num_authors} author slots, {num_reviewers} reviewers")

    reporter.print_and_exit_if_errors()

    print("\nDone. Next steps:")
    for volume in VOLUMES:
        print(f"  python generate {input_dir}/volume{volume.number} "
              f"--proceedings --overwrite --outdir output/volume{volume.number}")


if __name__ == "__main__":
    main()
