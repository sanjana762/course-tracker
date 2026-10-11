import importlib, subprocess, sys
for _pkg, _mod in [("beautifulsoup4", "bs4"), ("requests", "requests"), ("python-dateutil", "dateutil"), ("gspread", "gspread")]:
    try:
        importlib.import_module(_mod)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "-q", "install", _pkg])

import re, os, csv, json, time, gzip, hashlib
from datetime import date, datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dp

STORE = "sheets"                       # "sheets" | "local" (CSV files in LOCAL_DIR, for testing)
SPREADSHEET_ID = "1pGV-qW83t08BO_FLifSeULpIx9Fn8WVp8SeOCRH9CGQ"
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "")   # empty / missing in Colab -> Colab login popup
LOCAL_DIR = "tracker_data"
ONLY_SITES = [s.strip() for s in os.environ.get("ONLY_SITES", "").split(",") if s.strip()]      # e.g. "Maven,Imarticus"
RUN_DATE = os.environ.get("RUN_DATE", "")                                                       # "YYYY-MM-DD" to override today

RECENT_LAUNCH_DAYS = 30          # explicit-label launch stays on "Recent Launches" this long (about 3 months)
POTENTIAL_WINDOW_DAYS = 28       # "first appeared, no label" stays Potential this long
STALE_START_DAYS = 30           # a batch that started longer ago than this is never a launch
MISSING_RUNS_BEFORE_NOT_LISTED = 2
SITE_DROP_GUARD = 0.30           # site returns < 30% of its previous course count -> scrape treated as failed
BULK_MIN, BULK_FRAC = 6, 0.25    # more brand-new courses than max(6, 25% of old size) at once = discovery drift, not launches
MAX_PAGES_PER_SITE = 150         # course pages opened per site per run (courses with signals first)
MAX_SITEMAP_URLS = 600
WORKERS = 4

# ---------------- HARD EXCLUSIONS (your "links to remove" list) ----------------
BLOCKED_HOST_RE = re.compile(r"(linkedin\.|facebook\.|twitter\.|instagram\.|youtube\.|youtu\.be|wa\.me|koyo)", re.I)     # not a source
NOT_COURSE_PATH_RE = re.compile(                                                                                    # listing / category / tool / banner pages
    r"(/(?:all-)?(?:courses?|programs?|programmes?|categories|category|domains?|our-programs?|free-courses?|free-online-courses?|"
    r"online-courses?|explore|compar[\w-]*|course-compar[\w-]*|counsel[\w-]*|affiliate[\w-]*|hubs?|tags?|topics?|faculty|mentors?)/?$"
    r"|/(?:domains?|categories|category|our-programs?|free-courses?|compar[\w-]*|affiliate[\w-]*|counsel[\w-]*|hubs?)/"
    r"|-courses/?$|-hub/?$|ai-interviewer|interviewer|koyo|/blogs?|/articles?|/resources|/tutorials?|/interview|/login|/signup|/about|/contact|"
    r"/terms|/privacy|/careers?|/press|/webinars?|/events?|/podcast|/news|/help|/support|/refund|/faq|/sitemap|/cart|/checkout|/community|/reviews)", re.I)
CITY_NAMES = ["mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad", "chennai", "pune", "kolkata", "ahmedabad", "gurgaon",
              "gurugram", "noida", "jaipur", "lucknow", "chandigarh", "indore", "kochi", "coimbatore", "nagpur", "surat", "bhopal",
              "dubai", "singapore", "london"]
CITY_TXT = "|".join(c.replace(" ", r"\s+") for c in CITY_NAMES)
CITY_PAGE_RE = re.compile(r"(?:[-/_](?:in|at)[-_]|/)(?:" + "|".join(c.replace(" ", "[-_]") for c in CITY_NAMES) + r")(?:[-_/]|$)", re.I)   # ACCA/CMA/CFA city pages
NOT_COURSE_TITLE_RE = re.compile(
    r"^\s*(?:all|explore|browse)\s+(?:our\s+)?(?:courses?|programs?|programmes?|categories|domains?)\b|\ball\s+categories\b|"
    r"\bcompar(?:e|ison)\s+(?:tool|courses?)\b|\bcounsel+or\b|\baffiliate\b|\binterviewer\b|\bfree\s+courses?\b|\bisb\s+hub\b|"
    r"\bpage\s+not\s+found\b|\b404\b|\bprogram(?:me)?\s+(?:is\s+)?(?:inactive|discontinued)\b", re.I)
JUNK_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".xml", ".zip", ".mp4", ".css", ".js", ".ico")

# ---------------- SITES: one dict per website ----------------
# include/exclude = regex on the URL path | sitemap = optional | render = JS-heavy site | bare_new = a lone "New" badge is a real tag here
# new_labels/batch_labels = this site's own wording | sections = headings above a group of cards | evergreen = names that are never "new"
# first_appearance = may "first seen in a tracked run" count as a Possible launch? | payment_deadline = "Pay by <date>" is a payment date
# batch_means_unclear = batch wording cannot tell new course from new batch | generic_banners = shown on EVERYTHING -> never a signal
# deadline_recent_days = Simplilearn rule: a deadline within +/- N days of today counts as a (Possible) recent launch
SITES = {
    "Simplilearn": dict(start=["https://www.simplilearn.com/"], domains=["simplilearn.com"],
        sitemap="https://www.simplilearn.com/sitemaps/sitemaps_production.xml",
        include=r"(certification|certificate|training|course|bootcamp|program|programme|masters?|post-graduate|postgraduate|pgp|specialization|specialisation|fellowship|diploma|professional-certificate|executive|degree)",
        exclude=r"(/ebooks?|/corporate|/authors|/career-roadmap|/refer-and-earn|/grievance|/search)",
        new_labels=[r"new\s+launch"], weak_labels=[r"most\s+popular", r"trending"], deadline_recent_days=30),
    "Scaler": dict(start=["https://www.scaler.com/", "https://www.scaler.com/courses/"], domains=["scaler.com"], sitemap="https://www.scaler.com/sitemap.xml",
        include=r"(/courses?/|/academy|/data-science|/school-of-technology|/neovarsity|/devops|/program|/masters?)", exclude=r"(/hire)",
        new_labels=[r"new\s+program(?:me)?\s+launched", r"new\s+(?:program(?:me)?|course)\s+is\s+live"],
        batch_labels=[r"new\s+(?:beginner|advanced|weekend|weekday)\s+batch(?:es)?", r"(?:beginner|advanced)\s+batch"]),
    "Great Learning": dict(start=["https://www.mygreatlearning.com/"], domains=["mygreatlearning.com", "greatlearning.in"],
        include=r"(pg-|post-graduate|program|executive|masters?|mba|certificat|degree|bootcamp)", exclude=r"(/academy|/free)",
        generic_banners=[r"applications?\s+closes?\s+today"]),
    "Masai School": dict(start=["https://www.masaischool.com/"], domains=["masaischool.com"], render=True, bare_new=True,
        include=r"(/programs?/|/courses?/|/bootcamp|/mba|/mit|/uai|/iit|/full-stack|/data-|/software|/masters|/pgp|/degree|/advanced)", exclude=r"(/hire|/outcomes?|/stories)",
        batch_labels=[r"applications?\s+closes?"]),
    "Coding Ninjas": dict(start=["https://www.codingninjas.com/courses", "https://www.codingninjas.com/"], domains=["codingninjas.com"],
        include=r"(/courses/[^/]+|/bootcamp|/program|/data-science|/full-stack|/dsa|/placement|/certif)", exclude=r"(/studio|/library)"),
    "Edureka": dict(start=["https://www.edureka.co/all-courses", "https://www.edureka.co/"], domains=["edureka.co"],
        include=r"(training|certification|course|masters-program|bootcamp|post-graduate|/pgp)", exclude=r"(/cat\b)",
        batch_labels=[r"live\s+classes?\s+started\s+on"], batch_means_unclear=True,
        evergreen=[r"data\s+science\s+with\s+python", r"digital\s+business\s+management", r"\(20(?:1\d|2[0-5])\)"]),
    "Newton School": dict(start=["https://www.newtonschool.co/"], domains=["newtonschool.co"],
        include=r"(program|course|bootcamp|degree|masters|full-?stack|data|software|/ai|academy)", exclude=r"(/hire|/stories)",
        batch_labels=[r"\d+\s+seats?\s+(?:available|left|remaining)(?:\s+in\s+this\s+batch)?"]),
    "Eruditus": dict(start=["https://eruditus.com/"], domains=["eruditus.com"],
        include=r"(executive-education|/programs?/|/courses?/|/certificate|/degree|/mba|/masters)", exclude=r"(/insights)",
        payment_deadline=True, batch_means_unclear=True),
    "GrowthSchool": dict(start=["https://growthschool.io/"], domains=["growthschool.io"],
        include=r"(workshop|masterclass|course|program|cohort|bootcamp|/ai|/learn)", exclude=r"(/stories)"),
    "PW Skills": dict(start=["https://pwskills.com/", "https://pwskills.com/courses/"], domains=["pwskills.com"], render=True, bare_new=True,
        include=r"(/courses?/[^/]+|bootcamp|program|/skills?)", exclude=r"(/free)"),
    "AlmaBetter": dict(start=["https://www.almabetter.com/"], domains=["almabetter.com"], bare_new=True,       # "New" tag = course ; "New Batch Starting <date>" = batch
        include=r"(program|course|bootcamp|full-stack|data-science|masters|pgp|cloud|devops|cyber|specialisation|specialization|certificat)", exclude=r"(/bytes|/stories|/hire)"),
    "Intellipaat": dict(start=["https://intellipaat.com/"], domains=["intellipaat.com"], first_appearance=False,   # no tags/deadlines on the site -> only an explicit label could flag it
        include=r"(training|certification|course|masters|program|bootcamp|post-graduate|/pgp)", exclude=r"(/interview-questions|/cheat-sheet|/academy)"),
    "Imarticus": dict(start=["https://imarticus.org/"], domains=["imarticus.org"],
        include=r"(program|course|certification|/pgp|/pg-|executive|masters|bootcamp|/cfa|/cma|/acca|/frm)", exclude=r"(/placements?|/testimonials?)",
        evergreen=[r"\b(?:cfa|cma|acca|cpa|cibop|frm)\b", r"financial\s+analysis", r"banking\s*(?:and|&)\s*finance"]),
    "Maven": dict(start=["https://maven.com/courses"], domains=["maven.com"], render=True,
        include=r"^/[^/]+/[^/]+/?$", exclude=r"^/(login|signup|about|blog|pricing|teach|for-teams|enterprise|explore|courses|search|careers|press|privacy|terms|help|events?|learn|launch|instructors?|partners|community|apply|gift|redeem|tags?|topics?|categories|all-categories|p)(/|$)",
        new_labels=[r"new\s+on\s+maven"], sections=dict(new=r"new\s+on\s+maven", batch=r"starting\s+soon")),
}

# ---------------- generic vocabulary (sites ADD to it) ----------------
BATCH_RX = [r"new\s+(?:batch(?:es)?|cohorts?|intake)", r"next\s+(?:batch|cohort)(?:\s+starts?)?", r"upcoming\s+(?:batch(?:es)?|cohorts?)",
            r"cohorts?\s+start(?:s|ing)", r"batch\s+start(?:s|ing)", r"admissions?\s+open", r"applications?\s+open", r"starting\s+soon",
            r"live\s+classes?\s+start(?:s|ed|ing)", r"\d+\s+seats?\s+(?:available|left|remaining)", r"limited\s+seats?", r"pay\s+by"]
NEW_RX = [r"new\s*launch(?:ed|es)?", r"newly\s+launched", r"just\s+launched", r"recently\s+launched", r"new\s+program(?:me)?\s+launched",
          r"new\s+(?:course|program(?:me)?|certification|offering)s?\b(?!\s+batch)", r"newly\s+(?:added|introduced)"]
GENERIC_WEAK = [r"most\s+popular", r"trending(?:\s+now)?", r"featured", r"upcoming"]
NEW_SECTION_RE = re.compile(r"\b(new\s+(?:launches?|courses?|programs?|programmes?|arrivals|offerings?)|just\s+launched|newly\s+launched|recently\s+launched|latest\s+(?:launches|courses|programs))\b", re.I)
SECTION_STOP_RE = re.compile(r"(all\s+courses|explore|browse|popular|trending|categories|top\s|featured|upcoming|starting\s+soon|new\s+on|testimonials|why\s|faq)", re.I)

MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_RE = rf"(?:\d{{1,2}}(?:st|nd|rd|th)?[\s\-]+{MON}\.?(?:,?\s*\d{{4}})?|{MON}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?)"
KEY_RE = (r"(?:apply\s+(?:by|before)|applications?\s+(?:close|closes|closing|end|ends)(?:\s+on)?|last\s+date(?:\s+to\s+apply)?|deadline|register\s+(?:by|before)"
          r"|registrations?\s+(?:close|closes|closing)(?:\s+on)?|enrol(?:l)?(?:ment)?\s+(?:closes|ends|by|before)|closes?\s+on|closing\s+on|pay\s+(?:by|before)"
          r"|admissions?\s+(?:close|closes|closing|end|ends)(?:\s+on)?)")
DEADLINE_RE = re.compile(rf"(?P<key>{KEY_RE})\s*[:\-–]?\s*(?P<date>{DATE_RE})", re.I)
START_RE = re.compile(rf"(?:start(?:s|ed|ing)?|commenc(?:es|ed|ing)|begin(?:s|ning)?)(?:\s+date)?\s*(?:on|from)?\s*[:\-–]?\s*({DATE_RE})", re.I)
DURATION_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:months?|weeks?|hours?|days?|years?)", re.I)
HEADING_RE = re.compile(r"^h[1-6]$")
IGNORE_RE = re.compile(r"(navbar|nav-|-nav\b|mega|menu|footer|breadcrumb|related|recommend|similar|you-may|also-like|cookie|popup|modal)", re.I)
UA = {"User-Agent": "Mozilla/5.0 (compatible; CourseTracker/5.0)", "Accept-Language": "en-IN,en;q=0.9"}
MISSING = "Not mentioned"


def log(*a):
    print(*a, flush=True)


def squash(s):
    return re.sub(r"\s+", " ", (s or "").replace("\u00a0", " ")).strip()


def http_get(url, tries=3, timeout=30):
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            return r
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


def get_secret(name):
    try:
        from google.colab import userdata
        return userdata.get(name) or ""
    except Exception:
        return os.environ.get(name, "")


def render(url):
    """JS-rendered HTML: Firecrawl (free credits) if a key is set, else a local Playwright browser, else ''."""
    key = get_secret("FIRECRAWL_API_KEY")
    if key:
        try:
            rr = requests.post("https://api.firecrawl.dev/v1/scrape", timeout=90, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                               json={"url": url, "formats": ["html"], "waitFor": 4000})
            html = (rr.json().get("data") or {}).get("html") or ""
            if html:
                return html
        except Exception as e:
            log("  Firecrawl failed:", str(e)[:100])
    try:
        from playwright.sync_api import sync_playwright

        def _go():
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=["--no-sandbox"])
                pg = b.new_page(user_agent=UA["User-Agent"])
                pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(3500)
                pg.evaluate("async()=>{for(let i=0;i<8;i++){window.scrollTo(0,document.body.scrollHeight);await new Promise(r=>setTimeout(r,500));}}")
                h = pg.content()
                b.close()
                return h
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_go).result()
    except Exception:
        return ""


def clean_url(u):
    return u.split("#")[0].split("?")[0].rstrip("/") if u.count("/") > 2 else u.split("#")[0].split("?")[0]


def url_key(u):
    p = urlparse(u)
    return f"{p.netloc.lower().removeprefix('www.')}{p.path.rstrip('/')}".lower()


def host_ok(host, cfg):
    host = host.lower().removeprefix("www.").split(":")[0]
    return any(host == d or host.endswith("." + d) for d in cfg["domains"])


def reject_reason(u, cfg):
    """'' if the URL is a course candidate, else why it was rejected (used by the gate and by diagnose())."""
    if not u.lower().startswith(("http://", "https://")):
        return "not http"
    p = urlparse(u)
    if BLOCKED_HOST_RE.search(p.netloc):
        return "blocked host (social / Koyo)"
    if not host_ok(p.netloc, cfg):
        return "off-site"
    if p.path in ("", "/") or p.path.lower().endswith(JUNK_EXT):
        return "homepage / file"
    if NOT_COURSE_PATH_RE.search(p.path):
        return "listing / category / tool page"
    if CITY_PAGE_RE.search(p.path):
        return "city page"
    if re.search(cfg.get("exclude", r"(?!x)x"), p.path, re.I):
        return "site exclude pattern"
    if not re.search(cfg["include"], p.path, re.I):
        return "path does not match include pattern"
    return ""


def course_url_ok(u, cfg):
    """THE GATE. A URL is a course candidate only if it is on the site's own domain and is not a listing / category / city / tool / banner page."""
    return not reject_reason(u, cfg)


def diagnose(site):
    """Run for ONE site to see exactly why it returns few/no courses: HTTP status, anchors found, why anchors were rejected."""
    cfg = SITES[site]
    for url in cfg["start"]:
        r = http_get(url)
        html = r.text if r is not None and r.status_code == 200 else ""
        log(f"\n[{site}] {url} -> HTTP {r.status_code if r is not None else 'no response'} | html length {len(html)}")
        if len(html) < 2000 or cfg.get("render"):
            h2 = render(url)
            log(f"   rendered html length: {len(h2)}" + ("" if h2 else "  (no Firecrawl key / Playwright -> add FIRECRAWL_API_KEY as a Colab secret)"))
            html = h2 or html
        soup = BeautifulSoup(html, "html.parser")
        why, ok = {}, []
        for a in soup.find_all("a", href=True):
            h = a["href"].strip()
            if h.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            u = clean_url(urljoin(url, h))
            w = reject_reason(u, cfg)
            if w:
                why.setdefault(w, []).append(u)
            else:
                ok.append(u)
        log(f"   course-candidate links: {len(set(ok))}")
        for w, us in sorted(why.items(), key=lambda x: -len(x[1])):
            log(f"   rejected - {w}: {len(us)}   e.g. {', '.join(list(dict.fromkeys(us))[:3])}")
        for u in list(dict.fromkeys(ok))[:5]:
            log("   ok:", u)


def collect_cards(html, page_url, cfg):
    """One item per distinct course URL: the card text around its link(s), plus any 'New on X / Starting soon' section it sits under."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    akey = {}
    for a in soup.find_all("a", href=True):
        h = a["href"].strip()
        if h.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        u = clean_url(urljoin(page_url, h))
        if course_url_ok(u, cfg):
            akey[id(a)] = (url_key(u), u)
    sec_rx = [("new", re.compile(NEW_SECTION_RE.pattern + (("|" + cfg["sections"]["new"]) if cfg.get("sections", {}).get("new") else ""), re.I))]
    if cfg.get("sections", {}).get("batch"):
        sec_rx.append(("batch", re.compile(cfg["sections"]["batch"], re.I)))
    items = {}
    for a in soup.find_all("a", href=True):
        if id(a) not in akey:
            continue
        key, u = akey[id(a)]
        card = a
        while card.parent is not None and card.parent.name not in ("body", "html", "[document]"):
            p = card.parent
            others = {akey[id(x)][0] for x in p.find_all("a", href=True) if id(x) in akey} - {key}
            if others or len(p.get_text(" ", strip=True)) > 1200:
                break
            card = p
        strings = [squash(s) for s in card.stripped_strings if squash(s)]
        h = card.find(HEADING_RE)
        title = squash(h.get_text(" ", strip=True)) if h else ""
        if not title:
            cand = [s for s in strings if len(s) > 8 and not DURATION_RE.fullmatch(s)]
            title = max(cand, key=len) if cand else squash(a.get_text(" ", strip=True))
        section, node = None, card
        for _ in range(6):                                           # walk back over headings to find the section this card sits under
            node = node.find_previous(HEADING_RE)
            if node is None:
                break
            t = squash(node.get_text(" ", strip=True))
            if len(t) > 80:
                continue
            hit = next((k for k, rx in sec_rx if rx.search(t)), None)
            if hit:
                section = (hit, t)
                break
            if SECTION_STOP_RE.search(t):
                break
        it = items.setdefault(key, dict(url=u, name="", duration="", strings=[], sections=[], sources={"listing"}))
        it["strings"] += [s for s in strings if s not in it["strings"]]
        it["name"] = it["name"] if len(it["name"]) >= len(title) or len(title) > 120 else title
        it["duration"] = it["duration"] or next((s for s in strings if DURATION_RE.fullmatch(s)), "")
        if section and section not in it["sections"]:
            it["sections"].append(section)
    return items


def sitemap_urls(sm, cfg, depth=0, found=None):
    found = [] if found is None else found
    if not sm or depth > 2 or len(found) >= MAX_SITEMAP_URLS:
        return found
    r = http_get(sm)
    if r is None or r.status_code != 200:
        return found
    try:
        body = gzip.decompress(r.content).decode("utf-8", "ignore") if sm.endswith(".gz") else r.text
    except Exception:
        return found
    for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", body, re.I | re.S):
        if len(found) >= MAX_SITEMAP_URLS:
            break
        if loc.endswith((".xml", ".xml.gz")):
            sitemap_urls(loc, cfg, depth + 1, found)
        elif course_url_ok(clean_url(loc), cfg):
            found.append(clean_url(loc))
    return found


def scan(strings, cfg, kind):
    """Return dict(new=[(label, kind, strong)], batch=[labels], weak=[labels]). Batch wording is masked first so 'New Batch' never counts as 'New'."""
    out = dict(new=[], batch=[], weak=[])
    batch_rx = BATCH_RX + cfg.get("batch_labels", [])
    new_rx = NEW_RX + cfg.get("new_labels", [])
    weak_rx = GENERIC_WEAK + cfg.get("weak_labels", [])
    banners = cfg.get("generic_banners", [])
    for raw in strings:
        s = squash(raw)
        if not s or len(s) > 120 or any(re.search(b, s, re.I) for b in banners):
            continue
        masked = s
        for p in batch_rx:
            for m in re.finditer(p, masked, re.I):
                out["batch"].append(m.group(0))
                masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
        if len(s) <= 45:                                                  # explicit labels are SHORT; long sentences are never evidence
            for p in new_rx:
                m = re.search(p, masked, re.I)
                if m:
                    out["new"].append((m.group(0).strip(), kind, True))
                    masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
            if re.fullmatch(r"\W*new\W*", s, re.I):
                out["new"].append(("New", kind, bool(cfg.get("bare_new"))))     # bare badge: strong only where the site really uses it
        for p in weak_rx:
            if re.search(p, masked, re.I):
                w = re.search(p, masked, re.I).group(0)
                if w not in out["weak"]:
                    out["weak"].append(w)
    return out


def read_course_page(url, cfg):
    """Open the course page. Returns alive?/reason + title + top-of-page strings. Dead = 404, redirected away, inactive, not a course."""
    res = dict(ok=False, dead=False, reason="", title="", strings=[], text="")
    time.sleep(0.15)
    r = http_get(url)
    if r is None:
        res["reason"] = "could not fetch"
        return res
    if r.status_code in (404, 410):
        res.update(dead=True, reason=f"HTTP {r.status_code}")
        return res
    if r.status_code != 200:
        res["reason"] = f"HTTP {r.status_code}"
        return res
    fin = urlparse(r.url)
    hops = " ".join([h.headers.get("Location", "") for h in r.history] + [r.url]).lower()
    seg = lambda p: [x for x in p.split("/") if x]
    if "inactive" in hops or "discontinued" in hops:
        why = "redirects to an inactive/discontinued page"
    elif BLOCKED_HOST_RE.search(fin.netloc) or not host_ok(fin.netloc, cfg):
        why = "redirects off-site"
    elif fin.path in ("", "/"):
        why = "redirects to the homepage"
    elif url_key(r.url) != url_key(url) and len(seg(fin.path)) < len(seg(urlparse(url).path)):
        why = "redirects to a higher-level page"
    else:
        why = ""
    if why:
        res.update(dead=True, reason=why)
        return res
    parsed = parse_course_html(r.text)
    if not parsed["ok"] and not parsed["junk"] and (cfg.get("render") or get_secret("FIRECRAWL_API_KEY")):
        h2 = render(url)                                      # JS site: the plain HTML is an empty shell -> read the rendered page
        if h2:
            parsed = parse_course_html(h2)
    if parsed["junk"]:
        res.update(dead=True, reason=parsed["reason"])
    elif parsed["ok"]:
        res.update(ok=True, title=parsed["title"], strings=parsed["strings"], text=parsed["text"])
    else:
        res["reason"] = "could not verify (no readable course content - JavaScript site?)"      # kept as 'course page not verified', NOT dropped
    return res


def parse_course_html(html):
    out = dict(ok=False, junk=False, reason="", title="", strings=[], text="")
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    h1 = soup.find("h1")
    title = squash(h1.get_text(" ", strip=True)) if h1 else ""
    if not title and soup.title:
        title = squash(re.split(r"\s[|\-–—]\s", soup.title.get_text())[0])
    page_title = soup.title.get_text() if soup.title else ""
    if title and (NOT_COURSE_TITLE_RE.search(title) or NOT_COURSE_TITLE_RE.search(page_title)):
        out.update(junk=True, reason=f"not a course page (title: {title[:50]})")
        return out
    kill = [t for t in soup.find_all(["nav", "footer", "aside"])]
    kill += [t for t in soup.find_all(True) if t.name not in ("html", "body") and IGNORE_RE.search(" ".join(t.get("class") or []) + " " + str(t.get("id") or ""))]
    for t in kill:
        if t.decomposed if hasattr(t, "decomposed") else False:
            continue
        if h1 is None or (t is not h1 and t not in h1.parents):
            t.decompose()
    strings = [squash(x) for x in soup.stripped_strings if squash(x)]
    text = " ".join(strings)
    words = ("curriculum", "syllabus", "apply", "enrol", "fees", "duration", "eligibility", "certificate", "program", "cohort", "batch", "instructor", "learn", "mentor", "workshop")
    if not title or sum(w in text.lower() for w in words) < 2:
        out["reason"] = "no readable course content"
        return out
    out.update(ok=True, title=title[:150], strings=strings[:150], text=text[:30000])
    return out


MASTER_COLS = ["Unique Course ID", "Website", "Course Name", "Course URL", "Course Category", "Course Type", "Duration", "New Course?", "New Batch?",
               "New Course / Batch Label", "Classification", "Confidence", "Evidence", "Application Status", "Application Deadline", "Deadline Type",
               "Course/Batch Start Date", "Cohort/Batch Information", "Current Website Label", "Launch Basis", "Launch Detected Date", "Date First Seen",
               "Run Type", "Last Checked Date", "Last Updated Date", "Course Status", "Missed Runs", "Discovery Source", "Data Notes"]
RECENT_COLS = ["Launch Detected Date", "Website", "Course Name", "Course URL", "Launch Basis", "New Course / Batch Label", "Evidence", "Confidence",
               "Application Status", "Application Deadline", "Course/Batch Start Date", "New Batch?", "Still Listed?", "Unique Course ID"]
CHANGE_COLS = ["Change Date", "Website", "Course", "Course URL", "Field Changed", "Previous Value", "New Value", "Unique Course ID", "Change Key"]
TRACKED = ["Course Name", "Course URL", "New Course?", "New Batch?", "New Course / Batch Label", "Application Status", "Application Deadline",
           "Course/Batch Start Date", "Cohort/Batch Information", "Course Status"]
CATEGORY_RULES = [("Data Science / AI / ML", r"(data\s*science|machine\s*learning|\bai\b|artificial|deep\s*learning|generative|gen\s*ai|llm|prompt|analytics|data\s*analy|big\s*data|nlp)"),
                  ("Cyber Security", r"(cyber|security|ethical\s*hack)"), ("Cloud / DevOps", r"(cloud|\baws\b|azure|devops|kubernetes|docker)"),
                  ("Software Development", r"(software|full[\s\-]*stack|web\s*dev|java|python|\bdsa\b|coding|programming|backend|frontend|android)"),
                  ("Finance / Banking", r"(financ|banking|investment|\bcfa\b|\bfrm\b|\bacca\b|\bcma\b|accounting|fintech|trading)"),
                  ("Product / Management", r"(product\s*manage|project\s*manage|\bpmp\b|scrum|agile|management|leadership|\bmba\b|strategy|operations)"),
                  ("Marketing / Sales", r"(marketing|sales|\bseo\b|growth|brand)"), ("Design / UX", r"(design|\bux\b|\bui\b|figma)")]


def norm_name(s):
    s = re.sub(r"\(.*?\)", " ", (s or "").lower().replace("&", " and "))
    s = re.sub(rf"\b(?:in|at)\s+(?:{CITY_TXT})\b", " ", s)                      # "ACCA in Mumbai" == "ACCA in Delhi" == "ACCA"
    stop = {"course", "courses", "program", "programme", "programs", "certification", "certificate", "online", "training", "with", "the", "and", "of", "in", "for", "a", "live", "batch", "cohort", "new", "by", "to"}
    return " ".join(w for w in re.findall(r"[a-z0-9]+", s) if w not in stop and not re.fullmatch(r"20\d\d", w))


def course_type(name, duration, url):
    s = f"{name} {url}".lower()
    hrs = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*hours?\s*", duration or "", re.I)
    if re.search(r"workshop|masterclass|webinar", s) or (hrs and float(hrs.group(1)) <= 12):
        return "Workshop / short course"
    if re.search(r"\b(mba|msc|m\.?tech|bsc|b\.?tech|degree|pgdm|pg\s*diploma)\b", s):
        return "Degree / Diploma"
    if re.search(r"pgp|post[\s\-]*graduate|executive|advanced[\s\-]*(certificate|program)|masters", s):
        return "Executive / PG program"
    if "bootcamp" in s:
        return "Bootcamp"
    return "Certification" if re.search(r"certification|certificate", s) else "Course / Program"


def norm_date(text, today):
    if not text:
        return "", ""
    t = re.sub(r"(?<=\d)(st|nd|rd|th)", "", text, flags=re.I).strip()
    try:
        if re.search(r"\d{4}", t):
            return dp.parse(t, dayfirst=True).date().isoformat(), ""
        c = [dp.parse(t, dayfirst=True, default=datetime(y, 1, 1)).date() for y in (today.year - 1, today.year, today.year + 1)]
        return min(c, key=lambda d: abs((d - today).days)).isoformat(), "year inferred (not shown on site)"
    except (ValueError, OverflowError):
        return text, "date could not be parsed"


def app_status(blob, cfg, deadline_iso, today):
    b = blob.lower()
    generic = any(re.search(p, b, re.I) for p in cfg.get("generic_banners", []))
    if re.search(r"(?:applications?|admissions?|registrations?)\s+(?:are\s+)?closed", b):
        return "Closed"
    if deadline_iso:
        try:
            if date.fromisoformat(deadline_iso) < today:
                return "Closed (deadline passed)"
        except ValueError:
            pass
    if generic and re.search(r"clos\w*\s+today", b):
        return "Open - generic 'closes today' banner (shown on every course; not a real deadline)"
    if re.search(r"clos(?:es|ing)\s+today", b):
        return "Open - closes today"
    if re.search(r"(?:applications?|admissions?)\s+clos(?:es|ing)", b):
        return "Open" + ("" if deadline_iso else " - closing date not specified")
    if re.search(r"(?:applications?|admissions?|registrations?)\s+open|seats?\s+(?:available|left)|limited\s+seats?", b) or deadline_iso:
        return "Open"
    return MISSING


def is_evergreen(name, cfg):
    return any(re.search(p, name, re.I) for p in cfg.get("evergreen", []))


def build_record(site, cfg, it, page, today):
    """Item + (optional) verified page -> one Master record. New COURSE only from explicit labels; batch info stays separate."""
    card_sig = scan(it["strings"], cfg, "card")
    sig = dict(new=list(card_sig["new"]), batch=list(card_sig["batch"]), weak=list(card_sig["weak"]))
    for hit, head in it["sections"]:                                        # courses listed under 'New on Maven' / 'New launches' vs 'Starting soon'
        (sig["new"] if hit == "new" else sig["batch"]).append((head, "section", True) if hit == "new" else head)
    if page:
        ps = scan(page["strings"][:150], cfg, "page_top")
        for lab, k, strong in ps["new"]:
            sig["new"].append((lab, k, strong and (lab.lower() != "new" or cfg.get("bare_new"))))
        sig["batch"] += ps["batch"]
        sig["weak"] += ps["weak"]
    name = (page or {}).get("title") or it["name"]
    blob = " | ".join(it["strings"][:40]) + " | " + ((page or {}).get("text", "")[:6000])
    d = DEADLINE_RE.search(blob)
    s = START_RE.search(blob)
    dl_iso, dl_note = norm_date(d.group("date") if d else "", today)
    st_iso, st_note = norm_date(s.group(1) if s else "", today)
    pay = bool(d and re.match(r"pay", d.group("key"), re.I))
    strong = [x for x in sig["new"] if x[2]]
    weak_new = [x for x in sig["new"] if not x[2]]
    batch = list(dict.fromkeys(sig["batch"]))
    evergreen_block = is_evergreen(name, cfg) and not any(k in ("card", "section") for _, k, _ in strong)
    if strong and not evergreen_block:
        cls, newc = "NEW_COURSE", "Yes"
        conf = "HIGH" if len({k for _, k, _ in strong}) >= 2 else "MEDIUM"
        best = strong[0]
        evid = f'"{best[0]}" - {dict(card="badge/label on the course card", section="listed under a New/Launch section", page_top="top of the course page")[best[1]]}'
        if batch:
            evid += f'; batch wording also present ("{batch[0]}") - kept separate'
    elif weak_new or (strong and evergreen_block):
        cls, newc, conf = "AMBIGUOUS", "Unclear", "LOW"
        evid = ('Bare "New" wording only' if not strong else "Label only on the course page of a long-running programme") + "; not enough to call it a new course"
    elif batch:
        cls, conf = "NEW_BATCH", "MEDIUM"
        newc = "Unclear" if cfg.get("batch_means_unclear") else "No"
        evid = f'Batch/cohort wording "{batch[0]}" - a new intake, not proof the course is new'
    else:
        cls, newc, conf, evid = "NOT_NEW", "No", ("MEDIUM" if page else "LOW"), "No new-course or batch signal"
    labels = list(dict.fromkeys([x[0] for x in strong + weak_new] + batch + sig["weak"]))
    notes = [f"deadline {dl_note}" for _ in [0] if dl_note] + [f"start {st_note}" for _ in [0] if st_note]
    if not page:
        notes.append("course page not verified")
    if cls == "NEW_BATCH" and cfg.get("batch_means_unclear"):
        notes.append("batch label could mean new batch or new course - not determined")
    cohort = " | ".join(batch[:4]) + ((" | Starts: " + st_iso) if st_iso else "")
    url = it["url"]
    return {"Website": site, "Course Name": squash(name), "Course URL": url,
            "Course Category": next((lab for lab, pat in CATEGORY_RULES if re.search(pat, f"{name} {urlparse(url).path.replace('-', ' ')}".lower())), "Other"),
            "Course Type": course_type(name, it["duration"], url), "Duration": it["duration"] or MISSING, "New Course?": newc,
            "New Batch?": "Yes" if batch else ("Unclear" if (st_iso or dl_iso) else "No"), "New Course / Batch Label": " | ".join(labels[:4]) or "None",
            "Classification": cls, "Confidence": conf, "Evidence": evid, "Application Status": app_status(blob, cfg, dl_iso, today),
            "Application Deadline": dl_iso or MISSING, "Deadline Type": ("Payment deadline" if pay else "Application deadline") if dl_iso else MISSING,
            "Course/Batch Start Date": st_iso or MISSING, "Cohort/Batch Information": cohort.strip(" |") or MISSING,
            "Current Website Label": " | ".join(labels[:6]) or "None", "Discovery Source": "+".join(sorted(it["sources"])), "Data Notes": "; ".join(notes)}


def scrape_site(site, cfg, today):
    stats = dict(listing_rows=0, dead=0, duplicate=0, blocked_name=0, unverified_dropped=0)
    items = {}
    for url in cfg["start"]:
        log(f"[{site}] fetching {url}")
        r = http_get(url)                                   # always try the plain page first (render sites too: rendering may be unavailable)
        html = r.text if r is not None and r.status_code == 200 else ""
        if r is None or r.status_code != 200:
            log(f"  plain fetch failed: {'no response' if r is None else 'HTTP ' + str(r.status_code)} (site may block bots -> a Firecrawl key helps)")
        found = collect_cards(html, url, cfg) if html else {}
        if cfg.get("render") or len(found) < 3:
            h2 = render(url)
            if h2:
                found = {**found, **collect_cards(h2, url, cfg)}
            elif cfg.get("render") or not found:
                log("  could not render this page (no FIRECRAWL_API_KEY secret / Playwright) - using plain HTML only")
        for k, it in found.items():
            if k in items:
                items[k]["strings"] += [s for s in it["strings"] if s not in items[k]["strings"]]
                items[k]["sections"] += [s for s in it["sections"] if s not in items[k]["sections"]]
            else:
                items[k] = it
    n_cards = len(items)
    for u in sorted(sitemap_urls(cfg.get("sitemap", ""), cfg)):
        items.setdefault(url_key(u), dict(url=u, name="", duration="", strings=[], sections=[], sources={"sitemap"}))
    log(f"[{site}] discovered {n_cards} from pages + {len(items) - n_cards} from sitemap")
    order = sorted(items.values(), key=lambda it: (0 if (it["sections"] or scan(it["strings"], cfg, "card")["new"]) else (1 if it["strings"] else 2), it["url"]))
    todo, rest = order[:MAX_PAGES_PER_SITE], order[MAX_PAGES_PER_SITE:]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pages = list(pool.map(lambda it: read_course_page(it["url"], cfg), todo))
    recs, dead_log = [], []
    for it, pg in zip(todo, pages):
        if pg["dead"]:
            stats["dead"] += 1
            dead_log.append(f"{(it['name'] or it['url'])[:40]} ({pg['reason']})")
            continue
        if not pg["ok"] and "sitemap" in it["sources"] and len(it["sources"]) == 1:
            stats["unverified_dropped"] += 1                                    # sitemap-only + could not verify -> never trust it
            continue
        recs.append(build_record(site, cfg, it, pg if pg["ok"] else None, today))
    for it in rest:
        if "sitemap" not in it["sources"] or len(it["sources"]) > 1:
            recs.append(build_record(site, cfg, it, None, today))
    if dead_log:
        log(f"[{site}] dropped {len(dead_log)} dead / non-course pages: " + "; ".join(dead_log[:6]))
    out, by_key = [], {}                                                         # de-duplicate: same URL or same normalised name (any domain / city page)
    for rec in recs:
        if not rec["Course Name"] or len(rec["Course Name"]) < 4 or re.search(r"(ai\s+interviewer|all\s+categories|counsel+or|affiliate|course\s+comparison|isb\s+hub)", rec["Course Name"], re.I):
            stats["blocked_name"] += 1
            continue
        keys = [k for k in (("u", url_key(rec["Course URL"])), ("n", norm_name(rec["Course Name"]))) if k[1]]
        hit = next((by_key[k] for k in keys if k in by_key), None)
        if hit is None:
            out.append(rec)
            hit = rec
        else:
            stats["duplicate"] += 1
            if len(rec["Evidence"]) + (rec["Classification"] == "NEW_COURSE") * 100 > len(hit["Evidence"]) + (hit["Classification"] == "NEW_COURSE") * 100:
                out[out.index(hit)] = rec
                hit = rec
        for k in keys:
            by_key[k] = hit
    n_unv = sum("course page not verified" in r["Data Notes"] for r in out)
    if n_unv:
        log(f"[{site}] note: {n_unv} courses kept but their page could not be read (JavaScript site? add FIRECRAWL_API_KEY) - labels from the listing page still count")
    if not out:
        log(f"[{site}] 0 courses. Run diagnose('{site}') to see why (blocked? JavaScript? include pattern too strict?)")
    log(f"[{site}] kept {len(out)} courses | dropped: dead={stats['dead']} duplicates={stats['duplicate']} blocked={stats['blocked_name']} unverified-sitemap={stats['unverified_dropped']}")
    return out


def make_id(site, rec):
    return f"{re.sub(r'[^a-z0-9]+', '-', site.lower()).strip('-')}-{hashlib.sha1((url_key(rec['Course URL']) or norm_name(rec['Course Name'])).encode()).hexdigest()[:10]}"


def match_prev(prev_rows, recs):
    pairs, used = {}, set()
    by_url = {}
    for p in prev_rows:
        by_url.setdefault(url_key(p["Course URL"]), p)
    for i, r in enumerate(recs):
        p = by_url.get(url_key(r["Course URL"]))
        if p is not None and p["Unique Course ID"] not in used:
            pairs[i] = p
            used.add(p["Unique Course ID"])
    by_name = {}
    for p in prev_rows:
        by_name.setdefault(norm_name(p["Course Name"]), p)
    for i, r in enumerate(recs):
        if i in pairs:
            continue
        p = by_name.get(norm_name(r["Course Name"]))
        if p is not None and p["Unique Course ID"] not in used:
            pairs[i] = p
            used.add(p["Unique Course ID"])
    cands = [(SequenceMatcher(None, norm_name(r["Course Name"]), norm_name(p["Course Name"])).ratio(), i, p) for i, r in enumerate(recs) if i not in pairs
             for p in prev_rows if p["Unique Course ID"] not in used and norm_name(p["Course Name"]) and norm_name(r["Course Name"])]
    for sc, i, p in sorted(cands, key=lambda x: -x[0]):
        if sc >= 0.88 and i not in pairs and p["Unique Course ID"] not in used:
            pairs[i] = p
            used.add(p["Unique Course ID"])
    return pairs


def is_stale(rec, today):
    m = re.search(r"\b(20\d\d)\b", rec["Course Name"])
    if m and int(m.group(1)) < today.year:
        return True
    try:
        st = rec["Course/Batch Start Date"]
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", st)) and (today - date.fromisoformat(st)).days > STALE_START_DAYS
    except ValueError:
        return False


def assess_launch(rec, cfg, first_seen, run_type, prev_detected, today):
    """Launch basis: (1) explicit site label  (2) Possible = first seen in a tracked run, no label, passes every guard  (3) site deadline rule."""
    basis = ""
    verified = "course page not verified" not in rec["Data Notes"]
    guards = verified and not is_evergreen(rec["Course Name"], cfg) and not is_stale(rec, today)
    if rec["New Course?"] == "Yes":
        basis = "Explicit website label"
    elif guards and cfg.get("first_appearance", True) and run_type == "Tracked run" and (today - first_seen).days <= POTENTIAL_WINDOW_DAYS and rec["New Course?"] == "No":
        rec["New Course?"], basis = "Potential", "First appearance (no label)"
    elif guards and cfg.get("deadline_recent_days") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec["Application Deadline"]) and rec["New Course?"] == "No" \
            and abs((date.fromisoformat(rec["Application Deadline"]) - today).days) <= cfg["deadline_recent_days"]:
        rec["New Course?"], basis = "Potential", "Deadline within window (site rule)"
    rec["Launch Basis"] = basis
    rec["Launch Detected Date"] = (prev_detected or today.isoformat()) if basis else ""


def cmp_val(f, v):
    s = squash(str(v or "")).lower()
    if s in ("", "not mentioned", "none"):
        return ""
    return re.sub(r"\d+\s+seats?", "N seats", s) if f == "Cohort/Batch Information" else s


def reconcile(site, cfg, prev_rows, recs, today):
    t = today.isoformat()
    baseline = not prev_rows
    pairs = match_prev(prev_rows, recs)
    n_new = len(recs) - len(pairs)
    bulk = (not baseline) and n_new > max(BULK_MIN, BULK_FRAC * len(prev_rows))
    if bulk:
        log(f"[{site}] {n_new} courses appeared at once (was {len(prev_rows)}) -> treated as discovery drift: no 'Potential' launches (explicit labels still count)")
    rows, changes, used = [], [], set()
    for i, rec in enumerate(recs):
        p = pairs.get(i)
        if p is None:
            rec["Unique Course ID"], first, run_type, prev_det = make_id(site, rec), today, ("Baseline run" if (baseline or bulk) else "Tracked run"), ""
            rec["Date First Seen"], rec["Last Updated Date"] = t, t
        else:
            rec["Unique Course ID"], prev_det = p["Unique Course ID"], p.get("Launch Detected Date", "")
            rec["Date First Seen"] = p.get("Date First Seen") or t
            first = date.fromisoformat(rec["Date First Seen"]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", rec["Date First Seen"]) else today
            run_type = p.get("Run Type") or "Baseline run"                       # rows from older versions have no Run Type -> never "Potential"
        if rec["Unique Course ID"] in used:
            rec["Unique Course ID"] += "-" + hashlib.sha1(rec["Course Name"].encode()).hexdigest()[:4]
        used.add(rec["Unique Course ID"])
        rec.update({"Run Type": run_type, "Last Checked Date": t, "Course Status": "Active", "Missed Runs": "0"})
        assess_launch(rec, cfg, first, run_type, prev_det, today)
        if p is not None:
            diffs = [(f, p.get(f, ""), rec[f]) for f in TRACKED if cmp_val(f, p.get(f)) != cmp_val(f, rec[f])
                     and not (f == "New Course?" and p.get(f) == "Potential" and rec[f] == "No")]
            rec["Last Updated Date"] = t if diffs else (p.get("Last Updated Date") or t)
            for f, a, b in diffs:
                changes.append({"Change Date": t, "Website": site, "Course": rec["Course Name"], "Course URL": rec["Course URL"], "Field Changed": f,
                                "Previous Value": a or MISSING, "New Value": b or MISSING, "Unique Course ID": rec["Unique Course ID"],
                                "Change Key": hashlib.sha1(f"{rec['Unique Course ID']}|{f}|{a}|{b}|{t}".encode()).hexdigest()[:16]})
        elif not baseline:
            changes.append({"Change Date": t, "Website": site, "Course": rec["Course Name"], "Course URL": rec["Course URL"], "Field Changed": "Course Added",
                            "Previous Value": "", "New Value": rec["Course URL"], "Unique Course ID": rec["Unique Course ID"],
                            "Change Key": hashlib.sha1(f"{rec['Unique Course ID']}|added|{t}".encode()).hexdigest()[:16]})
        rows.append(rec)
    for p in prev_rows:                                                           # known courses missing this run
        if p["Unique Course ID"] in {r["Unique Course ID"] for r in rows} or any(p is q for q in pairs.values()):
            continue
        p = dict(p)
        if p.get("Last Checked Date") != t:
            missed = int(p.get("Missed Runs") or 0) + 1
            p["Missed Runs"] = str(missed)
            if missed >= MISSING_RUNS_BEFORE_NOT_LISTED and p.get("Course Status") != "Not Listed":
                old, p["Course Status"], p["Last Updated Date"], p["Last Checked Date"] = p.get("Course Status") or "Active", "Not Listed", t, t
                changes.append({"Change Date": t, "Website": site, "Course": p["Course Name"], "Course URL": p["Course URL"], "Field Changed": "Course Status",
                                "Previous Value": old, "New Value": "Not Listed", "Unique Course ID": p["Unique Course ID"],
                                "Change Key": hashlib.sha1(f"{p['Unique Course ID']}|status|{t}".encode()).hexdigest()[:16]})
        p["Launch Basis"], p["Launch Detected Date"] = "", ""                    # a course that is gone is not a launch
        rows.append(p)
    return rows, changes


def junk_row(p):
    """Old rows from earlier versions that violate the exclusion rules (off-site, listing, city page, banner without URL)."""
    u = (p.get("Course URL") or "").strip()
    cfg = SITES.get(p.get("Website"))
    if not u or cfg is None:
        return not u
    pu = urlparse(u)
    return bool(BLOCKED_HOST_RE.search(pu.netloc)) or not host_ok(pu.netloc, cfg) or bool(NOT_COURSE_PATH_RE.search(pu.path) or CITY_PAGE_RE.search(pu.path)) or pu.path in ("", "/")


def recent_launches(master, today):
    """Stateless: built from Master every run. Explicit label = 90 days from detection; Potential = 28 days."""
    out = []
    for r in master:
        if r.get("Course Status") != "Active" or not r.get("Launch Basis") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.get("Launch Detected Date", "")):
            continue
        age = (today - date.fromisoformat(r["Launch Detected Date"])).days
        if age <= (RECENT_LAUNCH_DAYS if r["Launch Basis"] == "Explicit website label" else POTENTIAL_WINDOW_DAYS):
            out.append({**r, "Still Listed?": "Yes"})
    return sorted(out, key=lambda r: (r["Launch Detected Date"], r["Website"]), reverse=True)


TABS = ["Master", "Recent Launches", "Change Log"]


def with_retry(fn, tries=4):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"  Sheets call failed ({type(e).__name__}); retrying ...")
            time.sleep(3 * 2 ** i)


class LocalStore:
    def __init__(self, folder):
        self.dir = folder
        os.makedirs(folder, exist_ok=True)

    def _p(self, tab):
        return os.path.join(self.dir, tab.replace(" ", "_") + ".csv")

    def read(self, tab):
        if not os.path.exists(self._p(tab)):
            return []
        with open(self._p(tab), newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def write(self, tab, cols, rows):
        with open(self._p(tab), "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerows([[str(r.get(c, "")) for c in cols] for r in rows])

    def append(self, tab, cols, rows):
        if not rows:
            return
        new = not os.path.exists(self._p(tab))
        with open(self._p(tab), "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if new:
                w.writerow(cols)
            w.writerows([[str(r.get(c, "")) for c in cols] for r in rows])


class SheetsStore:
    def __init__(self):
        import gspread
        self.g = gspread
        if SERVICE_ACCOUNT_FILE and os.path.exists(SERVICE_ACCOUNT_FILE):
            gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
        elif os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
            gc = gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
        else:
            from google.colab import auth
            auth.authenticate_user()
            from google.auth import default
            gc = gspread.authorize(default()[0])
        self.sh = with_retry(lambda: gc.open_by_key(SPREADSHEET_ID))
        self.ws = {}
        for t in TABS:
            try:
                self.ws[t] = self.sh.worksheet(t)
            except gspread.WorksheetNotFound:
                self.ws[t] = with_retry(lambda t=t: self.sh.add_worksheet(title=t, rows=200, cols=40))
        log("Google Sheet:", self.sh.url)

    def read(self, tab):
        v = with_retry(lambda: self.ws[tab].get_all_values())
        if len(v) < 2:
            return []
        head = v[0]
        return [dict(zip(head, row + [""] * (len(head) - len(row)))) for row in v[1:] if any(row)]

    def write(self, tab, cols, rows):
        data = [cols] + [[str(r.get(c, ""))[:2000] for c in cols] for r in rows]
        ws = self.ws[tab]
        with_retry(lambda: ws.clear())
        with_retry(lambda: ws.resize(rows=max(len(data) + 20, 100), cols=max(len(cols), 10)))
        for i in range(0, len(data), 2000):
            with_retry(lambda i=i: ws.update(range_name=f"A{i + 1}", values=data[i:i + 2000]))
        with_retry(lambda: ws.freeze(rows=1))

    def append(self, tab, cols, rows):
        if not rows:
            return
        ws = self.ws[tab]
        if not with_retry(lambda: ws.get_all_values()):
            with_retry(lambda: ws.update(range_name="A1", values=[cols]))
        with_retry(lambda: ws.append_rows([[str(r.get(c, ""))[:2000] for c in cols] for r in rows], value_input_option="RAW"))


# ---------------- EMAIL SUMMARY (sent at the end of every run) ----------------
# Secrets (GitHub Actions secrets / environment variables):
#   EMAIL_USER          sender address (Gmail)           e.g. you@gmail.com
#   EMAIL_APP_PASSWORD  Gmail *App Password* (16 chars, NOT your normal password)
#   EMAIL_TO            recipient(s), comma-separated    (defaults to EMAIL_USER)
# If EMAIL_USER / EMAIL_APP_PASSWORD are not set, the email is simply skipped.
EMAIL_MAX_ROWS = 40      # rows shown per section in the mail (the full data is always in the sheet)


def build_summary_html(t, total, launches, added, removed, failed, recent_total, sheet_url):
    from html import escape as e

    def stat(num, label, color):
        return (f'<td align="center" style="padding:18px 6px;border-right:1px solid #e8eaf0;">'
                f'<div style="font:700 30px Consolas,Menlo,monospace;color:{color};">{num}</div>'
                f'<div style="font:600 11px Arial,sans-serif;letter-spacing:1px;color:#6b7390;margin-top:6px;text-transform:uppercase;">{label}</div></td>')

    def section(title, color, rows, line2):
        """rows = list of (website, name, url, extra); grouped by website."""
        if not rows:
            return ""
        out = [f'<div style="padding:22px 32px 0;"><div style="font:700 16px Arial,sans-serif;color:{color};padding-bottom:8px;border-bottom:2px solid {color};">'
               f'{e(title)} <span style="font-weight:400;color:#8a90a8;">({len(rows)})</span></div></div>']
        shown = rows[:EMAIL_MAX_ROWS]
        last = None
        for site, name, url, extra in shown:
            if site != last:
                out.append(f'<div style="background:#f4f6fb;padding:8px 32px;font:700 11px Arial,sans-serif;letter-spacing:1px;color:#6b7390;text-transform:uppercase;">{e(site)}</div>')
                last = site
            out.append(f'<div style="padding:9px 32px;border-bottom:1px solid #f0f1f6;font:15px Arial,sans-serif;color:#1f2540;">'
                       f'<a href="{e(url)}" style="color:#1f2540;text-decoration:none;font-weight:600;">{e(name)}</a>'
                       + (f'<div style="font:12px Arial,sans-serif;color:#6b7390;margin-top:2px;">{e(extra)}</div>' if extra else "")
                       + f'<div style="font:12px Consolas,Menlo,monospace;color:#2b4eea;margin-top:3px;word-break:break-all;">{e(url)}</div></div>')
        if len(rows) > len(shown):
            out.append(f'<div style="padding:10px 32px;font:13px Arial,sans-serif;color:#6b7390;">+ {len(rows) - len(shown)} more - open the spreadsheet for the full list.</div>')
        return "".join(out)

    body = []
    body.append(section("New Launches Detected", "#15683f", launches, ""))
    body.append(section("Courses Added", "#2b4eea", added, ""))
    body.append(section("Removed / Not Listed", "#b42318", removed, ""))
    if failed:
        body.append('<div style="margin:22px 32px 0;padding:12px 14px;background:#fff4e5;border:1px solid #f5c26b;border-radius:6px;font:13px Arial,sans-serif;color:#7a4b00;">'
                    '<b>Sites that failed / were skipped this run (previous data kept):</b> ' + e(", ".join(failed)) + '</div>')
    if not (launches or added or removed or failed):
        body.append('<div style="padding:26px 32px;font:14px Arial,sans-serif;color:#6b7390;text-align:center;">No new launches, additions or removals this week.</div>')
    return f"""<!DOCTYPE html><html><body style="margin:0;padding:0;background:#eef0f6;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#eef0f6;padding:24px 0;"><tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;">
<tr><td style="background:#1f35c9;background-image:linear-gradient(135deg,#1a2266,#2b4eea);padding:30px 32px;">
  <div style="font:700 26px Arial,sans-serif;color:#ffffff;">Competitor Course Tracker - Weekly Report</div>
  <div style="font:14px Arial,sans-serif;color:#c7d0ff;margin-top:8px;">{e(t)} &nbsp;&middot;&nbsp; {len(SITES)} sites &nbsp;&middot;&nbsp; <a href="{e(sheet_url)}" style="color:#c7d0ff;">Open spreadsheet &#8599;</a></div>
</td></tr>
<tr><td><table width="100%" cellpadding="0" cellspacing="0" style="border-bottom:1px solid #e8eaf0;"><tr>
  {stat(total, "Courses tracked", "#2b4eea")}
  {stat("+" + str(len(launches)), "New launches", "#15683f" if launches else "#9aa1b8")}
  {stat("+" + str(len(added)), "Added", "#15683f" if added else "#9aa1b8")}
  {stat("-" + str(len(removed)), "Removed", "#b42318" if removed else "#9aa1b8")}
</tr></table></td></tr>
<tr><td>{"".join(body)}</td></tr>
<tr><td style="background:#f7f8fc;padding:18px 32px;margin-top:24px;font:12px Arial,sans-serif;color:#9aa1b8;text-align:center;">
  Sent automatically by the Competitor Course Tracker. {recent_total} courses are currently on the "Recent Launches" tab. View the spreadsheet for details on all tabs.</td></tr>
</table></td></tr></table></body></html>"""


def send_summary_email(t, master, recent, changes, summary):
    user, pwd = get_secret("EMAIL_USER").strip(), re.sub(r"\s+", "", get_secret("EMAIL_APP_PASSWORD"))      # App Password is shown with spaces; remove them
    if not user or not pwd:
        log("Email skipped (EMAIL_USER / EMAIL_APP_PASSWORD not set).")
        return
    try:
        import smtplib, ssl
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        to = [x.strip() for x in (get_secret("EMAIL_TO") or user).split(",") if x.strip()]
        launches = [(r["Website"], r["Course Name"], r["Course URL"], f'{r["Launch Basis"]} | {r["New Course / Batch Label"]}')
                    for r in recent if r.get("Launch Detected Date") == t]
        added = [(c["Website"], c["Course"], c["Course URL"], "") for c in changes if c["Field Changed"] == "Course Added"]
        removed = [(c["Website"], c["Course"], c["Course URL"], "") for c in changes if c["Field Changed"] == "Course Status" and c["New Value"] == "Not Listed"]
        failed = [s for s, v in summary.items() if v[0] != "OK"]
        total = sum(r.get("Course Status") == "Active" for r in master)
        sheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
        html_body = build_summary_html(t, total, sorted(launches), sorted(added), sorted(removed), failed, len(recent), sheet_url)
        text_body = (f"Competitor Course Tracker - {t}\nCourses tracked: {total} | New launches: {len(launches)} | Added: {len(added)} | Removed: {len(removed)}\n"
                     + ("Failed sites: " + ", ".join(failed) + "\n" if failed else "") + f"Spreadsheet: {sheet_url}\n")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Course Tracker {t}: {len(launches)} new launch(es), {len(added)} added, {len(removed)} removed"
        msg["From"], msg["To"] = user, ", ".join(to)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465")), context=ssl.create_default_context()) as s:
            s.login(user, pwd)
            s.sendmail(user, to, msg.as_string())
        log(f"Summary email sent to {', '.join(to)}")
    except Exception as ex:                                   # an email problem must never fail the tracker run
        log(f"Email FAILED ({type(ex).__name__}: {ex}) - tracker data is already saved")


def main():
    today = date.fromisoformat(RUN_DATE) if RUN_DATE else date.today()
    t = today.isoformat()
    store = LocalStore(LOCAL_DIR) if STORE == "local" else SheetsStore()
    prev = [p for p in store.read("Master") if not junk_row(p)]                  # old junk rows are purged here
    seen_keys = {r.get("Change Key") for r in store.read("Change Log")}
    log(f"\nRun date {t} | previous Master rows (after junk purge): {len(prev)}")
    prev_by_site = {}
    for p in prev:
        prev_by_site.setdefault(p["Website"], []).append(p)
    master, changes, summary = [], [], {}
    sites = {k: v for k, v in SITES.items() if not ONLY_SITES or k in ONLY_SITES}
    for site, cfg in sites.items():
        prev_rows = prev_by_site.get(site, [])
        try:
            recs = scrape_site(site, cfg, today)
        except Exception as e:
            log(f"[{site}] SCRAPE FAILED ({type(e).__name__}: {e}) - previous data kept")
            recs = []
        active_prev = [p for p in prev_rows if p.get("Course Status") != "Not Listed"]
        if not recs or (len(active_prev) >= 5 and len(recs) < SITE_DROP_GUARD * len(active_prev)):
            log(f"[{site}] WARNING: {len(recs)} courses vs {len(active_prev)} before -> treated as failed scrape; previous rows kept unchanged")
            master += prev_rows
            summary[site] = ("FAILED/SKIPPED", len(prev_rows), 0, 0)
            continue
        rows, ch = reconcile(site, cfg, prev_rows, recs, today)
        master += rows
        changes += ch
        summary[site] = ("OK", len(recs), sum(r["Launch Basis"] == "Explicit website label" for r in rows), sum(r["Launch Basis"] not in ("", "Explicit website label") for r in rows))
    for site, rows in prev_by_site.items():
        if site not in sites:
            master += rows
    changes = [c for c in changes if c["Change Key"] not in seen_keys]
    master.sort(key=lambda r: (r["Website"], r["Course Name"].lower()))
    recent = recent_launches(master, today)
    store.append("Change Log", CHANGE_COLS, changes)                              # logs first, Master LAST (safe to re-run)
    store.write("Recent Launches", RECENT_COLS, recent)
    store.write("Master", MASTER_COLS, master)
    log("\n" + "=" * 78)
    log(f"DONE {t} | Master {len(master)} | Recent Launches {len(recent)} | Change Log +{len(changes)}")
    for s, (st, n, ex, po) in summary.items():
        log(f"  {s:<15} {st:<15} courses={n:<4} confirmed-launch={ex:<3} possible-launch={po}")
    send_summary_email(t, master, recent, changes, summary)
    return master, recent


if __name__ == "__main__":
    main()