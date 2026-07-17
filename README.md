# ERCOT Meeting Database

## What the hell is this thing?

This is a project I built for Data and Databased with Jon Thirkield at Columbia Gradaute School of Journalism. The main goal was to take all of the meeting data from the ERCOT webite (the Texas energy regulator) and make it easily serachable by scraping all the documents. I used beautifulsoup4 for the scraping, flask & render for the app, and python libraries like spacy for entity recognition. Claude Code was used to help me build the HTML, SQlite, Flask, and some portions of the scraping infrastructure. I have read through all of the code, and won't pretend to perfectly understand everything that happened, but I'm using this as a learning experiance to try and understand/debug what Claude helped create by reviewing it over time. 

The really intersting find here for anybody who uses this is that ERCOT's committee meetings usually have some sort of pptx, either internal or a presenation from a company, which really clearly illistrate what ERCOT is thinking about in terms of energy, infratructure, and AI. Some of the companies I've found in these pptx documents include NVDIA, Meta, and the main Lobbying group for AI datacenters, the Data Center Coalition.

The public database dosen't have the actual documents yet becuase it would ballon in size, so instead I've made them all text-searchable and I'm linking the user to the download of the original document. I'm downloading the documents for posterity's sake, and plan to release a version with everything in it when I get some money and can figure out how the hell that would work. 

**Live site:** [ercot-meetings.onrender.com](https://ercot-meetings.onrender.com)

**Limitations, Challenges, Future Fixes**

The first real issue I ran into was parsing the PPTX files in a way that got the juciest information into the hands of researchers. Each pptx is full to the brim of graphics, photos, and charts showing big corperate plans for Texas power infrastructure. I tried using some huggingface models to slice out each image, then OCR it, but that became unwieldy really fast. so instead I've used python-pptx to grab every piece of available text on each slide, including speaker notes, and put it into a nicely formatted little database entry.

The second issue is the lack of the source document in the public version of this db. I still don't really know how to fix this. 

---

## Methodology

### Data Collection

Documents are scraped directly from the [ERCOT public calendar](https://www.ercot.com/calendar). The pipeline parses the calendar HTML to enumerate meetings, then fetches each meeting page to collect document links and metadata.

```
ercot_enumerate.py   →  meetings.csv       (meeting URL, date, committee, title, status)
ercot_details.py     →  documents.csv      (document URL, title, file type, size)
ercot_download.py    →  docs/              (raw files: PDF, DOCX, PPTX, XLSX, HTML)
```

### Text Extraction

Each downloaded file is parsed to plain text using format-specific libraries:

| Format | Library |
|--------|---------|
| PDF | PyMuPDF (`fitz`) |
| DOCX / DOC | python-docx |
| PPTX | python-pptx + pytesseract (slide image OCR) |
| XLSX / XLS | openpyxl, xlrd |
| HTML | BeautifulSoup4 |

```
extract_text.py   →  document_text table in ercot.db
```

### Named Entity Recognition

Document text is run through [spaCy](https://spacy.io/) (`en_core_web_lg`) to extract:
- **PERSON** — individuals named in meeting materials
- **ORG** — organizations, companies, and working groups
- **NPRR** — custom regex pattern for ERCOT nodal protocol revision requests

**It is important to note that the spacy recognition overfitted significantly so I have an admin portal on the backend to allow me to filter out stupid/bad entites. For example, spacy though words like Batch or Zero were names so I pulled those out.**

```
extract_entities.py   →  entities table in ercot.db
```

### Database

SQLite with [FTS5](https://www.sqlite.org/fts5.html) full-text search. The local database (~900 MB) holds full document text. A slim deployment copy (~240 MB) is built for hosting:

```
build_db.py          →  ercot.db           (full local DB with document_text)
build_public_db.py   →  ercot_public.db    (slim DB: contentless FTS index + 300-char teasers)
```

### Web Application

Flask app with SQLite/FTS5 backend. Features:
- Full-text search across all document content
- Entity browser (people, organizations, NPRR numbers)
- Homepage visualizations: top entities by mention count, 6-month trend charts (Chart.js)
- Meeting and document metadata browsing
- CSV export of document metadata

### Deployment

The app runs on [Render](https://render.com) (free tier). Because Render has no persistent storage, the database is distributed as a [GitHub Release](https://github.com/ccb2195-ux/ercot-meetings/releases) asset and downloaded at startup via `startup.py`.

[UptimeRobot](https://uptimerobot.com) pings the service every 5 minutes to prevent spin-down and avoid repeated cold-start downloads.

---

## Pipeline

### Monthly update (run on the 1st)
```bash
python monthly_update.py
```
Scrapes the current month, rebuilds the DB, builds the public DB, and prompts to publish a new GitHub Release.
---

## Stack

| Layer | Tool |
|-------|------|
| Scraping | Python `requests` + `re` |
| Text extraction | PyMuPDF, python-docx, python-pptx, openpyxl, BeautifulSoup4 |
| NER | spaCy `en_core_web_lg` |
| Database | SQLite 3 + FTS5 |
| Web framework | Flask + Jinja2 |
| Production server | Gunicorn |
| Hosting | Render (free tier) |
| DB distribution | GitHub Releases |
| Visualizations | Chart.js 4 |
