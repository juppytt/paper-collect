# paper-collect

`paper-collect` is a CLI for collecting papers from publication metadata. It
starts from a DBLP XML file, creates a SQLite manifest, downloads abstracts and
PDFs when available, and extracts text from downloaded PDFs.

Workflow:

1. Download the DBLP XML file.
2. Import papers from the venues and years you want into `data/paper_collect.sqlite`.
3. Download abstracts and/or PDFs for those papers.
4. Extract `.txt` files from the PDFs.

Supported venue codes:

| Code | Venue |
| --- | --- |
| `sp` | IEEE Symposium on Security and Privacy |
| `ccs` | ACM CCS |
| `security` | USENIX Security Symposium |
| `ndss` | Network and Distributed System Security Symposium |

## Setup

Install the CLI from the repository root:

```bash
python3 -m pip install -e .
```

To collect CCS papers from ACM, install the browser dependency too:

```bash
python3 -m pip install -e '.[browser]'
```

Text extraction requires Poppler's `pdftotext` command:

```bash
# Ubuntu/Debian
sudo apt-get install poppler-utils

# macOS
brew install poppler
```

CCS PDF downloads also require Chrome or Chromium. Use `--chrome-path` if the
browser is not on a standard path.

## Prepare DBLP Input

Download `dblp.xml.gz` and `dblp.dtd` from DBLP:

```text
https://dblp.org/xml/
```

Place both files here:

```text
data/raw/dblp/
  dblp.xml.gz
  dblp.dtd
```

DBLP provides metadata only: title, authors, year, venue, pages, DOI links, and
DBLP page links. It does not provide abstracts, PDFs, or paper text.

You do not need to unpack `dblp.xml.gz`. The CLI reads the compressed file
directly.

## Data Layout

The default local layout is:

```text
data/
  raw/
    dblp/
      dblp.xml.gz
      dblp.dtd
    pdf/
      <venue>/<year>/*.pdf
    text/
      <venue>/<year>/*.txt
  paper_collect.sqlite
```

`data/` is ignored by git.

## Build The Manifest

First, check how many DBLP entries match the venues and years you want:

```bash
paper-collect dblp-summary \
  --xml data/raw/dblp/dblp.xml.gz \
  --year-from 2013 \
  --year-to 2022
```

Inspect a few matched entries before importing:

```bash
paper-collect dblp-sample \
  --xml data/raw/dblp/dblp.xml.gz \
  --year-from 2013 \
  --year-to 2022 \
  --limit 5
```

Import the matched entries into SQLite:

```bash
paper-collect dblp-import \
  --xml data/raw/dblp/dblp.xml.gz \
  --db data/paper_collect.sqlite \
  --year-from 2013 \
  --year-to 2022
```

The import stores the matched papers in the `papers` table. It identifies
venues by DBLP proceedings IDs such as `conf/ccs/2022` and `conf/uss/2022`, not
by title keywords.

Check the imported counts:

```bash
sqlite3 data/paper_collect.sqlite \
  'select venue, count(*) from papers group by venue order by venue;'
```

## Download Abstracts And PDFs

Use `download` after the manifest exists. Start with `--dry-run` or a small
`--limit` when trying a new venue/year range.

Download abstracts for papers that match your filters:

```bash
paper-collect download \
  --db data/paper_collect.sqlite \
  --target abstract \
  --venues security ndss \
  --year-from 2020 \
  --year-to 2022 \
  --limit 20
```

Download PDFs:

```bash
paper-collect download \
  --db data/paper_collect.sqlite \
  --target pdf \
  --venues ndss \
  --year 2022 \
  --output-dir data/raw
```

Download both abstracts and PDFs:

```bash
paper-collect download \
  --db data/paper_collect.sqlite \
  --target both \
  --venues sp security ndss \
  --year-from 2013 \
  --year-to 2022 \
  --output-dir data/raw \
  --sleep 2
```

The command updates these SQLite columns when it succeeds:

| Column | Meaning |
| --- | --- |
| `abstract` | Extracted abstract text, when available |
| `pdf_url` | Remote PDF URL used by the downloader |
| `pdf_path` | Local path under `data/raw/pdf/<venue>/<year>/` |

Common filters:

| Option | Use |
| --- | --- |
| `--venues ccs ndss` | Include only selected venues |
| `--year 2022` | Include one year |
| `--year-from 2013 --year-to 2022` | Include a year range |
| `--paper-id 123` | Include one paper by its manifest ID |
| `--dblp-key conf/ccs/Example22` | Include one DBLP key |
| `--title-contains keyword` | Include titles containing a string |
| `--force` | Refresh existing artifacts |
| `--dry-run` | Preview matched papers and URLs without writing files |
| `--sleep 2` | Wait between papers |

### CCS Downloads

ACM often blocks plain HTTP clients at `dl.acm.org`, so CCS PDF downloads use a
real Chrome/Chromium browser.

```bash
paper-collect download \
  --db data/paper_collect.sqlite \
  --target pdf \
  --venues ccs \
  --year-from 2013 \
  --year-to 2022 \
  --output-dir data/raw \
  --sleep 20 \
  --timeout 90
```

Headed Chrome is usually more reliable for ACM. If you are running without a
display, you can try `--browser-headless`, but be prepared to rerun failures.

### Venue Notes

| Venue | Downloader behavior |
| --- | --- |
| `sp` | Uses IEEE Computer Society CSDL APIs to resolve abstracts and PDF endpoints. |
| `ccs` | Uses ACM DOI links and browser-based PDF download. |
| `security` | Uses DBLP electronic-edition links and generic HTML/PDF discovery. |
| `ndss` | Uses NDSS pages and year-specific fallbacks when abstracts are only available inside PDFs. |

## Extract Text From PDFs

After PDFs are downloaded, extract text:

```bash
paper-collect extract-text \
  --db data/paper_collect.sqlite \
  --venues ccs \
  --year-from 2013 \
  --year-to 2022 \
  --output-dir data/raw
```

The command reads `pdf_path`, writes `.txt` files under
`data/raw/text/<venue>/<year>/`, and updates `text_path` in SQLite.

Useful options:

| Option | Use |
| --- | --- |
| `--force` | Regenerate text even when `text_path` already exists |
| `--pdftotext /path/to/pdftotext` | Use a specific Poppler binary |
| `--timeout 120` | Set the per-PDF extraction timeout |
| `--min-chars 100` | Treat very short extraction output as failure |
| `--delete-pdfs` | Delete each PDF after text extraction succeeds |

Only use `--delete-pdfs` if you are sure you will not need the PDFs for
debugging or re-extraction:

```bash
paper-collect extract-text \
  --db data/paper_collect.sqlite \
  --venues ccs \
  --year-from 2013 \
  --year-to 2022 \
  --output-dir data/raw \
  --delete-pdfs
```

## TODO

* Add safer handling for PDF line-break hyphenation during text extraction or
  lookup/indexing.
* Add structured PDF parsing with GROBID or S2ORC doc2json.
* Improve CCS metadata filtering so workshop and poster entries can be separated
  from main-conference papers when needed.
* Prefer open-access PDFs when an official venue page does not expose a direct
  PDF URL.
* Add a config file only if repeated runs need many stable options. The config
  should map directly to the existing CLI flags.
