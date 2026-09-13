#!/usr/bin/env python3
"""
A Brilliant Mind — University Prospect Crawler + LinkedIn Finder
================================================================
Modes:
  python crawler.py                  # fill missing contacts
  python crawler.py --all            # re-crawl all rows
  python crawler.py --discover       # find and add new universities
  python crawler.py --linkedin       # find LinkedIn profiles for named contacts
  python crawler.py --linkedin --all # re-check LinkedIn for everyone
  python crawler.py --country DE     # filter by country code
  python crawler.py --push           # push CSV + index.html to GitHub

Requirements: pip install requests beautifulsoup4 lxml
GitHub push:  set env var GITHUB_TOKEN=ghp_...
"""

import csv, re, sys, os, time, json, base64, argparse, logging
from datetime import datetime
from urllib.parse import urljoin, urlparse, quote_plus

import requests
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────────────────
GITHUB_USER   = "YOUR-GITHUB-USERNAME"
GITHUB_REPO   = "YOUR-REPO-NAME"
GITHUB_BRANCH = "main"
CSV_FILE      = "prospects.csv"
DELAY         = 2.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8",
}

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("crawler")

FIELDNAMES = ["university","country","country_code","unit_type","unit_name",
              "contact_name","role","email","url","priority","status","notes","linkedin"]

ROLE_KEYWORDS = [
    "managing director","geschaeftsfuehr","head of","leitung","director",
    "coordinator","koordinator","researcher development","doctoral",
    "postdoc","nachwuchs","early career","transferable skills",
    "qualification","academic staff development","personalentwicklung",
]

EMAIL_RE    = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
LINKEDIN_RE = re.compile(r"https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9\-_%]+/?")

# ── HTTP ──────────────────────────────────────────────────────────────────────
def fetch(url, timeout=12):
    try:
        r = requests.get(url.strip(), headers=HEADERS, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        log.warning(f"  fetch failed {url[:60]}: {e}")
        return None

def clean(s): return re.sub(r"\s+", " ", s or "").strip()

# ── CONTACT EXTRACTION ────────────────────────────────────────────────────────
def score_role(text):
    t = text.lower()
    return sum(len(ROLE_KEYWORDS)-i for i,kw in enumerate(ROLE_KEYWORDS) if kw in t)

def get_linkedin_from_block(block):
    for a in block.find_all("a", href=True):
        m = LINKEDIN_RE.search(a["href"])
        if m and "/in/" in m.group(0):
            return m.group(0).rstrip("/")
    return ""

def extract_contacts(soup, url):
    candidates = []
    for sel in ["[class*='person']","[class*='staff']","[class*='team']",
                "[class*='contact']","[class*='mitarbeit']","[class*='ansprechpartner']",
                "[class*='member']","[class*='people']","[class*='profile']","[class*='card']"]:
        blocks = soup.select(sel)
        if not blocks: continue
        for block in blocks[:40]:
            text  = block.get_text(" ", strip=True)
            mailto = next((a["href"][7:].split("?")[0] for a in block.find_all("a",href=True)
                           if a["href"].startswith("mailto:")), None)
            emails  = EMAIL_RE.findall(text)
            linkedin = get_linkedin_from_block(block)
            name_el = (block.find(["h2","h3","h4","strong","b"]) or
                       block.find(class_=re.compile(r"name|title|heading",re.I)))
            name = name_el.get_text(strip=True) if name_el else ""
            role_text = text.replace(name,"").strip()
            role = ([l.strip() for l in role_text.splitlines() if l.strip()] or [""])[0]
            if not name and not emails and not mailto: continue
            candidates.append({"name":name,"role":role[:120],
                "email": mailto or (emails[0] if emails else ""),
                "linkedin": linkedin,
                "score": score_role(name+" "+role)})
        if candidates: break

    if not candidates:
        for table in soup.find_all("table")[:5]:
            for row in table.find_all("tr"):
                cells = row.find_all(["td","th"])
                if len(cells)<2: continue
                text  = " ".join(c.get_text(strip=True) for c in cells)
                mailto = next((a["href"][7:].split("?")[0] for a in row.find_all("a",href=True)
                               if a["href"].startswith("mailto:")), None)
                emails = EMAIL_RE.findall(text)
                linkedin = get_linkedin_from_block(row)
                if not emails and not mailto: continue
                name,role = cells[0].get_text(strip=True), cells[1].get_text(strip=True)
                candidates.append({"name":name,"role":role[:120],
                    "email": mailto or emails[0], "linkedin": linkedin,
                    "score": score_role(name+" "+role)})

    if not candidates:
        for a in soup.find_all("a", href=True):
            if not a["href"].startswith("mailto:"): continue
            email = a["href"][7:].split("?")[0].strip()
            name, parent = "", a.parent
            for _ in range(4):
                if parent is None: break
                h = parent.find(["h2","h3","h4","strong"])
                if h: name=h.get_text(strip=True); break
                parent = parent.parent
            candidates.append({"name":name,"role":"","email":email,"linkedin":"","score":score_role(name)})

    candidates.sort(key=lambda x:(-x["score"],-bool(x["name"])))
    return candidates

def find_team_url(base_url, soup):
    kws = ["team","contact","kontakt","ansprechpartner","staff","people","mitarbeiter","personen"]
    for a in soup.find_all("a", href=True):
        href,text = a["href"].lower(), a.get_text(strip=True).lower()
        if any(kw in href or kw in text for kw in kws):
            full = urljoin(base_url, a["href"])
            if urlparse(full).netloc == urlparse(base_url).netloc:
                return full
    return None

# ── LINKEDIN SEARCH ───────────────────────────────────────────────────────────
def search_linkedin(name, university, role=""):
    """Search DuckDuckGo for a person's LinkedIn /in/ profile URL."""
    if not name or len(name.strip()) < 4:
        return ""

    clean_name = re.sub(r'^(Dr\.|Prof\.|Mag\.|Dipl\.|Ing\.)\s*', '', name, flags=re.I).strip()

    for query in [
        f'{clean_name} {university} site:linkedin.com/in',
        f'{clean_name} "{university}" linkedin',
    ]:
        try:
            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            resp = requests.get(url, headers={**HEADERS,"Accept":"text/html"},
                                timeout=10, allow_redirects=True)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            for a in soup.find_all("a", href=True):
                m = LINKEDIN_RE.search(a["href"])
                if m:
                    li = m.group(0).rstrip("/")
                    slug = li.split("/in/")[-1].lower()
                    if len(slug) > 3 and not any(x in slug for x in ["company","school","showcase"]):
                        log.info(f"  → LinkedIn: {li}")
                        return li
            time.sleep(1.5)
        except Exception as e:
            log.warning(f"  LinkedIn search error: {e}")
            break

    return ""

def find_linkedin_on_page(url):
    """Look for a LinkedIn link directly on a team page."""
    soup = fetch(url)
    if not soup: return ""
    for a in soup.find_all("a", href=True):
        m = LINKEDIN_RE.search(a["href"])
        if m and "/in/" in m.group(0):
            return m.group(0).rstrip("/")
    return ""

# ── CRAWL ONE ROW ─────────────────────────────────────────────────────────────
def crawl_row(row):
    url = row.get("url","").strip()
    uni = row.get("university","")
    log.info(f"▶ {uni}")
    if not url or url=="#": return row

    soup = fetch(url)
    if soup is None: return row

    candidates = extract_contacts(soup, url)
    if not candidates or not candidates[0].get("name"):
        team_url = find_team_url(url, soup)
        if team_url and team_url!=url:
            log.info(f"  → team page: {team_url}")
            time.sleep(DELAY)
            soup2 = fetch(team_url)
            if soup2:
                c2 = extract_contacts(soup2, team_url)
                if c2: candidates=c2; row["url"]=team_url

    if candidates:
        c = candidates[0]; updated = False
        for field,key in [("contact_name","name"),("role","role"),("email","email")]:
            val = clean(c.get(key,""))
            if val and not row.get(field,"").strip():
                row[field]=val; updated=True; log.info(f"  ✓ {field}: {val}")
        if c.get("linkedin") and not row.get("linkedin","").strip():
            row["linkedin"]=c["linkedin"]; updated=True
            log.info(f"  ✓ linkedin (page): {c['linkedin']}")
        if updated:
            row["status"] = f"Crawler updated {datetime.now().strftime('%d %b %Y')}"
    else:
        log.info("  – no contacts found")
    return row

def crawl_linkedin_row(row):
    """Find LinkedIn for a single named contact."""
    name = row.get("contact_name","").strip()
    uni  = row.get("university","").strip()
    url  = row.get("url","").strip()
    log.info(f"▶ LinkedIn: {name} @ {uni}")

    # 1. Check team page first
    if url and url!="#":
        li = find_linkedin_on_page(url)
        if li:
            row["linkedin"] = li
            row["status"] = f"Crawler updated {datetime.now().strftime('%d %b %Y')}"
            return row
        time.sleep(DELAY)

    # 2. Search DuckDuckGo
    li = search_linkedin(name, uni, row.get("role",""))
    if li:
        row["linkedin"] = li
        row["status"] = f"Crawler updated {datetime.now().strftime('%d %b %Y')}"
    else:
        log.info(f"  – no LinkedIn found")
    return row

# ── DISCOVERY ─────────────────────────────────────────────────────────────────
DISCOVERY_SOURCES = [
    ("DE","Germany",      "https://www.uniwind.org/mitglieder"),
    ("AT","Austria",      "https://www.uniko.ac.at/mitglieder"),
    ("CH","Switzerland",  "https://www.swissuniversities.ch/en/higher-education-area/recognised-swiss-higher-education-institutions"),
    ("UK","United Kingdom","https://www.vitae.ac.uk/higher-education/member-organisations"),
    ("NL","Netherlands",  "https://www.universiteitenvannederland.nl/en_GB/universities.html"),
]

def discover_from_page(url, country_code, country, existing_unis):
    log.info(f"Discovering from: {url}")
    soup = fetch(url)
    if not soup: return []
    new_rows=[]; seen={e.lower() for e in existing_unis}
    for a in soup.find_all("a", href=True):
        text=a.get_text(strip=True); href=a["href"]
        if not text or len(text)<4 or len(text)>80: continue
        if not any(s in href.lower() for s in [".ac.uk",".edu","/uni-","univ","university","hochschule","tu-"]): continue
        if text.lower() in seen: continue
        if len(new_rows)>50: break
        blank={f:'' for f in FIELDNAMES}
        blank.update({"university":text,"country":country,"country_code":country_code,
            "unit_type":"Doctoral School","url":href if href.startswith("http") else urljoin(url,href),
            "priority":"C","status":f"Discovered {datetime.now().strftime('%d %b %Y')}",
            "notes":f"Auto-discovered from {url}"})
        new_rows.append(blank); seen.add(text.lower()); log.info(f"  + {text}")
    return new_rows

def run_discovery(rows):
    existing=[r["university"] for r in rows]; new_rows=[]
    for cc,country,url in DISCOVERY_SOURCES:
        found=discover_from_page(url,cc,country,existing)
        new_rows.extend(found); existing.extend([r["university"] for r in found])
        time.sleep(DELAY*2)
    log.info(f"Discovery: {len(new_rows)} new universities")
    crawled=[]
    for i,row in enumerate(new_rows):
        log.info(f"  crawl {i+1}/{len(new_rows)}: {row['university']}")
        crawled.append(crawl_row(row)); time.sleep(DELAY)
    return crawled

# ── CSV / STATUS / INJECT ─────────────────────────────────────────────────────
def read_csv(path):
    with open(path,newline="",encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    for r in rows: r.setdefault("linkedin","")
    return rows

def write_csv(path, rows):
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDNAMES,extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    log.info(f"Saved {len(rows)} rows -> {path}")

def write_status(rows):
    has_name  = sum(1 for r in rows if r.get("contact_name","").strip())
    has_email = sum(1 for r in rows if r.get("email","").strip())
    has_li    = sum(1 for r in rows if r.get("linkedin","").strip())
    s={"last_run":datetime.utcnow().isoformat()+"Z","total_rows":len(rows),
       "rows_with_name":has_name,"rows_with_email":has_email,"rows_with_linkedin":has_li,
       "coverage_pct":round(has_name/len(rows)*100,1) if rows else 0,
       "linkedin_pct":round(has_li/has_name*100,1) if has_name else 0}
    with open("crawl_status.json","w") as f: json.dump(s,f,indent=2)
    log.info(f"Status: {has_name} named | {has_email} email | {has_li} LinkedIn")

def inject_into_html(csv_path, html_path="index.html"):
    if not os.path.exists(html_path): return
    with open(csv_path,encoding="utf-8") as f: rows=list(csv.DictReader(f))
    data_js="window.PROSPECT_DATA = "+json.dumps(rows,ensure_ascii=False)+";"
    with open(html_path,encoding="utf-8") as f: html=f.read()
    html=re.sub(r'/\* PROSPECT_DATA_START \*/.*?/\* PROSPECT_DATA_END \*/',
        f"/* PROSPECT_DATA_START */\n{data_js}\n/* PROSPECT_DATA_END */",
        html,flags=re.DOTALL)
    with open(html_path,"w",encoding="utf-8") as f: f.write(html)
    log.info(f"Injected {len(rows)} rows into {html_path}")

def push_file(path, message):
    token=os.environ.get("GITHUB_TOKEN")
    if not token: log.error("GITHUB_TOKEN not set"); return
    filename=os.path.basename(path)
    api=f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/contents/{filename}"
    hdrs={"Authorization":f"token {token}","Accept":"application/vnd.github.v3+json"}
    r=requests.get(api,headers=hdrs,params={"ref":GITHUB_BRANCH})
    sha=r.json().get("sha") if r.ok else None
    with open(path,"rb") as f: content=base64.b64encode(f.read()).decode()
    payload={"message":message,"content":content,"branch":GITHUB_BRANCH}
    if sha: payload["sha"]=sha
    r=requests.put(api,headers=hdrs,json=payload)
    if r.ok: log.info(f"Pushed {filename}")
    else: log.error(f"Push failed: {r.status_code} {r.text[:200]}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    p=argparse.ArgumentParser(description="ABM University Crawler")
    p.add_argument("--all",      action="store_true")
    p.add_argument("--discover", action="store_true")
    p.add_argument("--linkedin", action="store_true", help="Find LinkedIn profiles")
    p.add_argument("--row",      type=int)
    p.add_argument("--country",  type=str)
    p.add_argument("--push",     action="store_true")
    p.add_argument("--csv",      type=str, default=CSV_FILE)
    args=p.parse_args()

    if not os.path.exists(args.csv):
        log.error(f"CSV not found: {args.csv}"); sys.exit(1)

    rows=read_csv(args.csv)
    log.info(f"Loaded {len(rows)} rows")

    if args.discover:
        new_rows=run_discovery(rows); rows.extend(new_rows)
        log.info(f"Added {len(new_rows)} new universities")

    # Contact crawl (skip if --linkedin only)
    if not args.linkedin or args.all:
        updated=0
        for i,row in enumerate(rows):
            if args.row is not None and i!=args.row: continue
            if args.country and row.get("country_code","").upper()!=args.country.upper(): continue
            if not args.all and row.get("contact_name","").strip(): continue
            if not row.get("url","").strip(): continue
            rows[i]=crawl_row(row); updated+=1; time.sleep(DELAY)
        log.info(f"Contact crawl: updated {updated} rows")

    # LinkedIn search
    if args.linkedin:
        li_updated=0
        for i,row in enumerate(rows):
            if args.row is not None and i!=args.row: continue
            if args.country and row.get("country_code","").upper()!=args.country.upper(): continue
            if not args.all and row.get("linkedin","").strip(): continue  # skip if already found
            if not row.get("contact_name","").strip(): continue           # need a name
            rows[i]=crawl_linkedin_row(row); li_updated+=1
            time.sleep(DELAY*1.5)
        log.info(f"LinkedIn: updated {li_updated} rows")

    write_csv(args.csv,rows)
    write_status(rows)
    inject_into_html(args.csv)

    if args.push:
        msg=f"crawler: update {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC"
        push_file(args.csv,msg)
        push_file("crawl_status.json",msg)
        push_file("index.html",msg)

if __name__=="__main__":
    main()
