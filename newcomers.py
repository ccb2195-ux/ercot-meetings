"""Find organizations and rule changes appearing in the archive for the first time.

The name tagger is noisy, so this is deliberately strict: it would rather
show five real newcomers than twenty fragments of slide text.
"""
import os
import re
import sqlite3
from datetime import date, timedelta

# An organization name has to END like a company / project / body to qualify.
_ORG_END = re.compile(
    r"\b(Inc|LLC|L\.L\.C|Corp|Corporation|Company|Co|Ltd|LP|L\.P|PLC|"
    r"Energy|Power|Electric|Solar|Wind|Storage|Battery|BESS|Cooperative|Co-op|"
    r"Coalition|Association|Alliance|Holdings|Partners|Capital|Technologies|"
    r"Authority|Utilities|Renewables|Data\s+Centers?|Labs|University|Institute|Council)\.?$"
)
# Or contain a name worth flagging on sight.
_ORG_BRAND = re.compile(
    r"\b(Microsoft|Google|Alphabet|Amazon|AWS|Meta|NVIDIA|Nvidia|Tesla|OpenAI|Oracle|"
    r"Crusoe|CoreWeave|Riot|Marathon|Bitdeer|Core\s+Scientific|Stargate|xAI|Anthropic)\b"
)
# Words that mean the tagger grabbed a slide heading, not an organization.
_ORG_JUNK = {
    "board", "resolution", "resolutions", "confirmation", "except", "backup",
    "designated", "program", "project", "projects", "process", "requirements",
    "guide", "section", "protocol", "protocols", "update", "status", "review",
    "summary", "overview", "item", "agenda", "appendix", "draft", "final",
    "non", "proposed", "total", "existing", "new", "planned", "study",
    "lacks", "statutory", "combined", "independent", "generator", "load", "loads",
    "resource", "resources", "resource’s", "resource's", "ercot", "ercot’s", "ercot's",
    "complete", "completed", "parallel", "attestation", "attestations", "form", "forms",
}
# Common first words that don't identify a company on their own. For names
# starting with these, the first TWO words are used to match earlier mentions.
_COMMON_FIRST = {
    "blue", "red", "green", "white", "black", "golden", "silver", "sun", "solar",
    "wind", "south", "north", "east", "west", "southern", "northern", "eastern",
    "western", "central", "texas", "lone", "star", "gulf", "high", "big", "great",
    "american", "united", "national", "new", "first", "panhandle", "parallel",
    "complete", "power", "energy", "electric", "grid", "clear", "bright", "rock",
    "river", "lake", "oak", "pine", "cedar", "eagle", "hawk", "prairie", "desert",
}
_LOWER_OK = {"of", "and", "&", "de", "for", "the", "on", "at"}
_CORP_SUFFIX = re.compile(
    r"\b(inc|llc|l l c|lp|l p|plc|corp|corporation|company|co|ltd)\b", re.I)


def _strip_article(t):
    return re.sub(r"^(the|an|a)\s+", "", t.strip(), flags=re.I)


def org_key(t):
    """Loose identity for an org, so 'NRG Energy, Inc.' matches 'NRG Energy'."""
    t = _strip_article(t).lower()
    t = re.sub(r"[^\w\s&]", " ", t)
    t = _CORP_SUFFIX.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


def brand_key(t):
    """The part of a name that identifies the company: 'NRG Texas Power LLC' -> 'nrg',
    'Blue Bird Solar' -> 'blue bird'. Used to spot renamed versions of old orgs."""
    words = org_key(t).split()
    if not words:
        return ""
    if words[0] in _COMMON_FIRST or len(words[0]) < 3:
        return " ".join(words[:2])
    return words[0]


def plausible_org(t):
    t = _strip_article(t)
    if re.search(r"[•:;()\[\]{}/\\|@#=+*<>\d]", t):
        return False
    words = t.replace(",", " ").split()
    if not 1 <= len(words) <= 6:
        return False
    if re.fullmatch(r"[A-Z]\.?", words[0]):          # outline markers like "E."
        return False
    if any(w.lower().strip(".") in _ORG_JUNK for w in words):
        return False
    if any(not (w[0].isupper() or w.lower() in _LOWER_OK) for w in words):
        return False
    if len(words) == 1 and len(t) <= 5 and t.isupper():   # bare acronyms
        return False
    return bool(_ORG_END.search(t) or _ORG_BRAND.search(t))


_cache = {}


def find_newcomers(db, db_path, window_days=90, n_orgs=5, n_rules=3):
    """Orgs and NPRRs whose first mention falls in the last `window_days` of data."""
    try:
        stamp = (db_path, os.path.getmtime(db_path))
    except OSError:
        stamp = None
    blocked = tuple(sorted(tuple(r) for r in db.execute(
        "SELECT entity_text, entity_type FROM entity_blocklist")))
    key = (stamp, blocked, window_days, n_orgs, n_rules)
    if stamp and key in _cache:
        return _cache[key]

    latest = db.execute("SELECT MAX(date) FROM meetings").fetchone()[0]
    first = db.execute("SELECT MIN(date) FROM meetings").fetchone()[0]
    if not latest or not first:
        return None
    end = date.fromisoformat(latest[:10])
    since = (end - timedelta(days=window_days)).isoformat()
    # Need real history before the window, or everything looks "new".
    if first[:10] > (end - timedelta(days=window_days + 180)).isoformat():
        return None

    blocked_set = set(blocked)
    rows = db.execute(
        """SELECT e.entity_text, e.entity_type,
                  MIN(d.meeting_date)            AS first_seen,
                  COUNT(DISTINCT e.document_id)  AS docs,
                  COUNT(DISTINCT d.meeting_url)  AS meetings
           FROM entities e JOIN documents d ON d.id = e.document_id
           WHERE e.entity_type IN ('ORG', 'NPRR')
           GROUP BY e.entity_text, e.entity_type"""
    ).fetchall()

    prior_org_keys = set()
    prior_nprr_max = 0
    fresh_orgs, fresh_rules = [], []
    for text, etype, first_seen, docs, meetings in rows:
        if (text, etype) in blocked_set or not first_seen:
            continue
        if etype == "NPRR":
            m = re.fullmatch(r"NPRR(\d{3,4})", text)
            if not m:
                continue                      # typos like NPRR13908
            num = int(m.group(1))
            if first_seen < since:
                prior_nprr_max = max(prior_nprr_max, num)
            else:
                fresh_rules.append({"text": text, "num": num, "first_seen": first_seen,
                                    "docs": docs, "meetings": meetings})
        else:
            if first_seen < since:
                prior_org_keys.add(brand_key(text))
            elif docs >= 2 and plausible_org(text):
                fresh_orgs.append({"text": text, "label": _strip_article(text),
                                   "first_seen": first_seen, "docs": docs,
                                   "meetings": meetings})

    # Drop renamed versions of orgs we've already seen ('NRG Energy, Inc.')
    orgs, seen = [], set()
    for o in sorted(fresh_orgs, key=lambda o: (-o["meetings"], -o["docs"], o["first_seen"])):
        k = brand_key(o["text"])
        if not k or k in prior_org_keys or k in seen:
            continue
        seen.add(k)
        orgs.append(o)

    # A "new" rule change is a number higher than anything filed before the window.
    rules = sorted((r for r in fresh_rules if r["num"] > prior_nprr_max),
                   key=lambda r: (-r["docs"], -r["num"]))
    rules = rules[:n_rules]
    for r in rules:
        r["title"] = nprr_title(db, r["num"])

    out = {"since": since, "through": latest[:10],
           "orgs": orgs[:n_orgs], "rules": rules,
           "n_orgs": len(orgs), "n_rules": len([r for r in fresh_rules if r["num"] > prior_nprr_max])}
    if stamp:
        _cache.clear()
        _cache[key] = out
    return out


_TITLE_NOISE = re.compile(
    r"\s+(?:(?:PRS|TAC|WMS|ROS|RMS|WMWG|CFSG|TWG|PLWG|SAWG|DWG|LLWG|Board)\b.*|\d{6,8}.*)$")


def _tidy_title(t):
    t = re.split(r"[\x00-\x1f]|\s{2,}", t)[0]          # stop at control chars / gaps
    t = _TITLE_NOISE.sub("", t).strip(" ,.-–_")
    if not t or re.match(r"(?i)(urgency|ballot|comments?|implementation|and\b|&|as submitted|was\b|is\b)", t):
        return None
    if not 6 <= len(t) <= 70 or not t[0].isupper():
        return None
    return t


def nprr_title(db, num):
    """Best-effort short title for an NPRR: 'NPRR1337, ERS Enhancements' -> 'ERS Enhancements'."""
    candidates = []
    for (title,) in db.execute(
            "SELECT doc_title FROM documents WHERE doc_title LIKE ?", (f"%NPRR{num}%",)):
        m = re.search(rf"NPRR\s?{num}\b[\s,:\-–]*(.+)", title or "")
        if m:
            candidates.append(m.group(1))

    # Then the document text (full DB) or opening lines (public DB), where
    # ERCOT writes the formal "NPRR1337, ERS Enhancements" form.
    tables = [t for t in ("document_text", "document_summary")
              if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()]
    if tables:
        table = tables[0]
        col = "text" if table == "document_text" else "teaser"
        try:
            ids = [r[0] for r in db.execute(
                "SELECT rowid FROM document_fts WHERE document_fts MATCH ? LIMIT 40", (f"NPRR{num}",))]
        except sqlite3.Error:
            ids = []
        for i in ids:
            row = db.execute(f"SELECT {col} FROM {table} WHERE document_id=?", (i,)).fetchone()
            for m in re.finditer(rf"NPRR\s?{num}\s*[,:\-–]\s*([^\n]+)", (row[0] if row else "") or ""):
                candidates.append(m.group(1))

    tidy = [t for t in (_tidy_title(c) for c in candidates) if t]
    if not tidy:
        return None
    # Most common wins; ties go to the shorter (cleaner) one.
    counts = {}
    for t in tidy:
        counts[t] = counts.get(t, 0) + 1
    return sorted(counts, key=lambda t: (-counts[t], len(t)))[0]
