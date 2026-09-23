# AIME-Con proceedings

Tooling and instructions for the AIME-Con proceedings chair. The single
script in this repo, `prepare_aimecon.py`, turns the raw files provided by
the program chairs into three input directories for
[aclpub2](https://github.com/desilinguist/aclpub2) (our fork, `aimecon`
branch), and aclpub2's `generate` command then builds the final proceedings
PDFs:

- Volume 1: Full Papers
- Volume 2: Works in Progress
- Volume 3: Coordinated Session Papers

## What you need from the program chairs

Before anything here works, collect the following files from the program
chairs. Put them all in one folder named after the year, e.g. `aimecon2026`.
The `examples/aimecon2026/` folder in this repo shows a complete, working
layout with real (trimmed) files. Please refer to this folder as a reference
for creating files for future years. 

### Paper metadata and PDFs

- `Vol1.csv`, `Vol2.csv`, `Vol3.csv`: one row per accepted paper in that
  volume. Vol1 is full papers, Vol2 is works in progress, Vol3 is
  coordinated session papers. Columns: `Paper ID`, `Title`, `Abstract`,
  `PDF URL` (optional), then up to ten repeating author groups
  `AuthorN First`, `AuthorN Middle`, `AuthorN Last`, `AuthorN Affiliation`,
  `AuthorN Email`. Fields may contain commas and newlines as long as they
  are properly quoted.
- `Vol1/`, `Vol2/`, `Vol3/`: folders with the final camera-ready PDFs,
  named `Paper-<id>.pdf` where `<id>` matches the `Paper ID` column in the
  CSV. Every CSV row must have a matching PDF, and every PDF should have a
  CSV row. The script checks both directions and reports mismatches.

### Front matter

- `full-papers-preface.md`, `wip-papers-preface.md`,
  `coordinated-session-papers-preface.md`: the preface for each volume in
  markdown. Do not include a top-level "Preface" heading; it is added
  automatically. Section headings (`## ...`), bold, and bullet lists are
  all supported.
- `aimecon-logo.png`: that year's AIME-Con logo, shown at the top of each
  preface.

### Sponsors

- `sponsor_logos/`: a folder with four subfolders named `platinum`,
  `gold`, `silver`, and `supporter`, each containing the tier's logo PNGs
  (convention: `<company>-logo.png`).
- `sponsor_ads.pdf`: all sponsor advertisements in one PDF, in order.

### Committees

- `ncme-leadership.csv`: `firstname,lastname,credentials,role`
  (e.g. `Kadriye,Ercikan,Ph.D.,President`).
- `organizing-committee.csv`:
  `firstname,lastname,credentials,institution,role`. The role values are
  used as-is as section headings (e.g. `Conference Chairs`,
  `Proceedings Chair`, `Program Chairs`). Rows with role
  `Conference Chairs` also become the editors listed in the proceedings
  metadata.
- `full-papers-reviewers.csv`, `wip-papers-reviewers.csv`,
  `coordinated-papers-reviewers.csv`: `firstname,lastname,institution`,
  one reviewer per row, one file per volume.

### ISBNs

- `isbn.csv`: header `volume,isbn`, then exactly three rows:

  ```
  volume,isbn
  full,978-...
  wip,978-...
  coordinated,978-...
  ```

## Prerequisites

1. [`uv`](https://docs.astral.sh/uv/). On macOS:

   ```
   brew install uv
   ```

   or see the uv docs for other platforms.
2. A TeX distribution with `pdflatex`, e.g.
   [MacTeX](https://www.tug.org/mactex/) or BasicTeX on macOS, TeX Live on
   Linux.
3. A JDK (`java` on the PATH). aclpub2 uses a Java tool called PAX to
   preserve hyperlinks from the paper PDFs.

## Setup

1. Clone the aclpub2 fork and switch to the `aimecon` branch:

   ```
   git clone git@github.com:desilinguist/aclpub2.git
   cd aclpub2
   git checkout aimecon
   ```

   The `aimecon` branch contains AIME-Con-specific additions that have not
   landed upstream yet (the `sponsor_ads` and `proceedings_address` fields
   in `conference_details.yml`). Building with upstream aclpub2 will not
   include the sponsor ads.

2. Create the Python environment for aclpub2:

   ```
   uv venv
   source .venv/bin/activate
   uv pip install -r requirements.txt
   ```

3. Copy `prepare_aimecon.py` from this repo into the root of the aclpub2
   clone:

   ```
   curl -O https://raw.githubusercontent.com/desilinguist/aimecon-proceedings/main/prepare_aimecon.py
   ```

   The script must live in the aclpub2 root because it locates the PAX jars
   in the `aclpub2` package next to it.

4. Create the input folder (e.g. `aimecon2026`) inside the aclpub2 clone
   and fill it with the files from the program chairs, laid out as
   described above. See `examples/aimecon2026/` in this repo for a complete
   example.

## Test your setup with the bundled example

`examples/aimecon2026/` contains a small but complete input set: two papers
per volume instead of the usual dozens, trimmed reviewer lists, and the
real sponsor material. Run it through the whole pipeline first to confirm
your setup works. From the root of the aclpub2 clone:

```
uv run prepare_aimecon.py /path/to/aimecon-proceedings/examples/aimecon2026 \
    --start-date 2026-10-26 --end-date 2026-10-28 \
    --location "Wyndham Grand Pittsburgh, Downtown, Pittsburgh, Pennsylvania, United States" \
    --overwrite
```

Then build one volume:

```
python bin/generate /path/to/aimecon-proceedings/examples/aimecon2026/volume1 \
    --proceedings --overwrite --outdir output/example-volume1
```

If that produces `output/example-volume1/proceedings.pdf` with a cover,
sponsors, sponsor ads, copyright page, preface, committees, table of
contents, and the two papers, your setup is good.

## Preparing the real inputs

From the root of the aclpub2 clone:

```
uv run prepare_aimecon.py aimecon2026 \
    --start-date 2026-10-26 --end-date 2026-10-28 \
    --location "Wyndham Grand Pittsburgh, Downtown, Pittsburgh, Pennsylvania, United States" \
    --overwrite
```

`uv run` reads the dependencies from the script header, so there is nothing
to install for the script itself.

What the script does:

- Validates that every required input file exists and reports all missing
  ones at once.
- Scans every source paper PDF with PAX and rebuilds, in place, any PDF
  that PAX's old PDFBox cannot parse (a known issue with PDFs produced by
  some versions of Word). The rebuild preserves the content exactly; page
  count and text are verified before the original is replaced. Skip this
  scan with `--skip-repair-pdfs`.
- Cross-checks the CSVs against the PDF folders. A CSV row without a PDF is
  a hard error; a PDF without a CSV row is excluded with a warning.
- Converts the markdown prefaces to LaTeX (via pandoc, bundled), prepends
  the conference logo, and maps Unicode characters that pdflatex cannot
  typeset (Greek letters, math symbols like `≤`) to LaTeX equivalents, in
  both prefaces and paper titles/abstracts.
- Writes `aimecon2026/volume1`, `volume2`, and `volume3`, each a complete
  aclpub2 input directory: `conference_details.yml`, `papers.yml`,
  `papers/`, `prefaces/`, `organizing_committee.yml`,
  `program_committee.yml`, `sponsors.yml`, `sponsor_logos/`,
  `sponsor_ads.pdf`, and `aimecon-logo.png`.

## Building the proceedings

Still from the aclpub2 root, build each volume:

```
python bin/generate aimecon2026/volume1 --proceedings --overwrite --outdir output/volume1
python bin/generate aimecon2026/volume2 --proceedings --overwrite --outdir output/volume2
python bin/generate aimecon2026/volume3 --proceedings --overwrite --outdir output/volume3
```

Each `output/volumeN/` contains:

- `proceedings.pdf`: the full volume, ready to publish.
- `watermarked_pdfs/`: each paper as a separate watermarked PDF (`0.pdf` is
  the front matter).
- `inputs/`: the YAML files the ACL Anthology ingestion reads.

Build each volume twice in a row if internal links or the table of
contents look stale; aclpub2 already runs pdflatex twice for the full
proceedings, but the front matter is compiled once.

## Troubleshooting

- **`Unicode character ... not set up for use with LaTeX`**: a paper title
  or preface contains a character the template cannot typeset. Add it to
  `UNICODE_TO_LATEX` in `prepare_aimecon.py` and rerun the prepare step.
  The script warns about suspicious characters at prepare time.
- **`Annotation on page N not recognized` warnings from PAX**: a few links
  in that paper use features PAX does not support (e.g. links that launch
  external files). Those links are skipped; everything else is preserved.
  A small number of these is normal.
- **PAX `NullPointerException` while generating**: a paper PDF is
  structurally incompatible with the old PDFBox bundled with aclpub2. Run
  the prepare step again without `--skip-repair-pdfs`; it rebuilds such
  PDFs automatically.
