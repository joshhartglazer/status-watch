#!/usr/bin/env python3
"""Poll a Banner 9 class search for a list of sections and send a push
notification when a seat or waitlist spot opens. Read-only.

Config comes from the WATCH_CONFIG env var (JSON) or a local config.json:
  {"base": "https://.../StudentRegistrationSsb/ssb",
   "term": "000000",
   "sections": {"12345": "label", ...}}
"""
import http.cookiejar, json, os, re, subprocess, sys, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state.json"
LOG = HERE / "watch.log"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CLOUD = bool(os.environ.get("NTFY_TOPIC"))


def load_config():
    raw = os.environ.get("WATCH_CONFIG")
    if not raw and (HERE / "config.json").exists():
        raw = (HERE / "config.json").read_text()
    if not raw:
        sys.exit("missing WATCH_CONFIG")
    return json.loads(raw)


def log(msg):
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def notify(title, body):
    if CLOUD:
        r = urllib.request.Request(f"https://ntfy.sh/{os.environ['NTFY_TOPIC']}", data=body.encode(),
                                   headers={"Title": title, "Priority": "high", "Tags": "rotating_light"})
        urllib.request.urlopen(r, timeout=30).read()
    else:
        script = f'display notification {json.dumps(body)} with title {json.dumps(title)} sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def num(label, s):
    m = re.search(re.escape(label) + r":\s*(-?\d+)", s)
    return int(m.group(1)) if m else None


class Banner:
    def __init__(self, base, term):
        self.base, self.term = base.rstrip("/"), term
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def req(self, path, data=None, xhr=False):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        r = urllib.request.Request(self.base + path, data=body, headers={"User-Agent": UA})
        if xhr:
            r.add_header("X-Requested-With", "XMLHttpRequest")
        with self.op.open(r, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")

    def start(self):
        self.req("/term/termSelection?mode=search")
        self.req("/term/search?mode=search", {"term": self.term, "studyPath": "", "studyPathText": "",
                                              "startDatepicker": "", "endDatepicker": ""}, xhr=True)

    def section(self, crn):
        d = {"term": self.term, "courseReferenceNumber": crn}
        e = text(self.req("/searchResults/getEnrollmentInfo", d))
        x = text(self.req("/searchResults/getXlstSections", d))
        info = {
            "seats": num("Enrollment Seats Available", e),
            "wl": num("Waitlist Actual", e),
            "wl_cap": num("Waitlist Capacity", e),
            "wl_open": num("Waitlist Seats Available", e),
            "xl_seats": num("Cross List Seats Available", x),
            "xl_partners": re.findall(r"\b(\d{5})\b", x.split("Sections:")[-1]) if "Cross List" in x else [],
        }
        if info["seats"] is None:
            raise ValueError("could not parse enrollment response")
        return info


def main():
    cfg = load_config()
    sections = cfg["sections"]
    b = Banner(cfg["base"], cfg["term"])
    b.start()
    cur = {crn: b.section(crn) for crn in sections}

    for crn, s in cur.items():
        total = s["wl"] or 0
        for p in s["xl_partners"]:
            if p != crn:
                total += (cur[p]["wl"] if p in cur else (b.section(p)["wl"] or 0))
        s["wl_total"] = total
        s["open"] = s["seats"] if s["xl_seats"] is None else min(s["seats"], s["xl_seats"])
        s["can_register"] = s["open"] > total
        # Banner only offers "Waitlist" when the section itself shows 0 seats.
        s["can_waitlist"] = (s["wl_open"] or 0) > 0 and s["seats"] == 0

    prev = json.loads(STATE.read_text()) if STATE.exists() else {}
    alerts = []
    for crn, s in cur.items():
        p, name = prev.get(crn, {}), sections[crn]
        if s["can_register"] and not p.get("can_register"):
            alerts.append(f"SEAT OPEN: {name} ({crn}) {s['open']} open, {s['wl_total']} waiting")
        if s["can_waitlist"] and not p.get("can_waitlist"):
            alerts.append(f"Waitlist spot: {name} ({crn}) {s['wl']}/{s['wl_cap']} on waitlist")
        if not CLOUD:
            log(f"{crn} open={s['open']} wl={s['wl']}/{s['wl_cap']} wl_total={s['wl_total']} "
                f"reg={s['can_register']} wlok={s['can_waitlist']}")
    STATE.write_text(json.dumps(cur))

    if "--test" in sys.argv:
        notify("Watcher test", f"Working. Watching {len(sections)} items.")
    for a in alerts:
        notify("Watcher alert", a)
    # Public logs stay generic: no names, numbers, or URLs.
    print(f"ok: checked {len(sections)} items, {len(alerts)} alert(s)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {type(e).__name__}")
        if not CLOUD:
            log(f"ERROR {type(e).__name__}: {e}")
        sys.exit(1)
