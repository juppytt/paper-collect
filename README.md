# paper-collect

Reusable paper collection utilities for large-scale literature corpora.

The first supported workflow builds a seed manifest from a local DBLP XML dump.
Downstream projects can then enrich those seed rows with venue pages, abstracts,
PDF links, downloaded PDFs, and extracted full text.

## Current Scope

* Stream-parse `dblp.xml.gz` without expanding the full file on disk.
* Select main-conference papers by exact DBLP `crossref`.
* Support the first security corpus venues:
	* `sp`: IEEE Symposium on Security and Privacy
	* `ccs`: ACM CCS
	* `security`: USENIX Security Symposium
	* `ndss`: Network and Distributed System Security Symposium
* Store a normalized seed manifest in SQLite.
* Download abstracts and PDFs from manifest rows using venue/DOI links where
  direct crawling is allowed.
* Keep raw downloads and generated databases out of git.

## Data Layout

```text
data/
  raw/
    dblp/
      dblp.xml.gz
      dblp.dtd
  paper_collect.sqlite
```

DBLP's XML export is metadata only. It includes titles, authors, years,
booktitles, pages, crossrefs, DBLP page URLs, and electronic-edition links.
It does not include abstracts or paper full text.

## Commands

Run commands from the repository root:

```bash
PYTHONPATH=src python3 -m paper_collect.cli dblp-summary \
  --xml data/raw/dblp/dblp.xml.gz \
  --max-year 2022
```

```bash
PYTHONPATH=src python3 -m paper_collect.cli dblp-sample \
  --xml data/raw/dblp/dblp.xml.gz \
  --max-year 2022 \
  --limit 5
```

```bash
PYTHONPATH=src python3 -m paper_collect.cli dblp-import \
  --xml data/raw/dblp/dblp.xml.gz \
  --db data/paper_collect.sqlite \
  --max-year 2022
```

After importing, inspect the manifest with:

```bash
sqlite3 data/paper_collect.sqlite \
  'select venue, count(*) from papers group by venue order by venue;'
```

Download abstracts, PDFs, or both from selected manifest rows:

```bash
PYTHONPATH=src python3 -m paper_collect.cli download \
  --db data/paper_collect.sqlite \
  --target abstract \
  --venues security ndss \
  --year-from 2020 \
  --year-to 2022 \
  --limit 20
```

```bash
PYTHONPATH=src python3 -m paper_collect.cli download \
  --db data/paper_collect.sqlite \
  --target pdf \
  --venues ndss \
  --year 2022 \
  --output-dir data/raw
```

The downloader stores PDFs under `data/raw/pdf/<venue>/<year>/` and updates
`abstract`, `pdf_url`, and `pdf_path` in SQLite. Use `--dry-run` before larger
crawls and `--sleep` for polite venue crawling.

CCS PDF downloads use a real Chrome/Chromium browser because plain HTTP clients
receive ACM's Cloudflare challenge at `dl.acm.org`. Install the browser extra
and run CCS slowly:

```bash
python3 -m pip install -e '.[browser]'
PYTHONPATH=src python3 -m paper_collect.cli download \
  --db data/paper_collect.sqlite \
  --target pdf \
  --venues ccs \
  --year-from 2010 \
  --year-to 2022 \
  --output-dir data/raw \
  --sleep 20 \
  --timeout 90
```

Use `--chrome-path /path/to/chrome` if Chrome is not on a standard path. On a
server without a display, try `--browser-headless`, but headed Chrome or Xvfb may
be more reliable with ACM. At 20 seconds per paper, the 2010-2022 CCS DOI set
takes at least about 11 hours before browser and download overhead.

NDSS pages do not expose abstracts consistently across years. The NDSS downloader
uses explicit year policies: 2010-2015 and 2017 parse HTML abstracts; 2016 and
2018-2022 extract abstracts from the first pages of the paper PDF. PDF text
extraction requires `pdftotext` from Poppler.

S&P uses IEEE Computer Society CSDL instead of DOI landing pages. The S&P
downloader queries the CSDL proceedings group, maps the target year to a
proceeding ID, matches CSDL article rows against DBLP rows by DOI or normalized
title, and resolves abstracts and PDS PDF endpoints.

## Next Downloaders

* Venue pages:
	* USENIX Security proceedings pages usually expose paper pages, abstracts,
	  and PDF links directly.
	* NDSS paper pages expose direct PDF links; sampled 2022 pages did not expose
	  HTML abstracts.
	* IEEE S&P uses CSDL GraphQL/PDS endpoints instead of DOI landing pages.
	* CCS browser PDF download works from ACM DOI links; abstracts and
	  full-paper filtering still need SIGSAC/OpenAlex enrichment.
* Open metadata APIs:
	* Semantic Scholar, Unpaywall, and OpenAlex can enrich DOI, abstract,
	  open-access URL, and license fields when official venue pages are
	  incomplete.
	* CCS PDF collection can use ACM browser download, and should still prefer
	  non-ACM OA repository PDFs when available.
* Text extraction:
	* GROBID or S2ORC doc2json should be used for full-text extraction instead
	  of hand-written PDF parsing.
