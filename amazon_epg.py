#!/usr/bin/env python3
"""
Amazon Prime Video — free linear channels EPG scraper (XMLTV).

Ported from the iptv-org/epg primevideo.com site config. Cookie-free:
  * Channel list  : getDataByTransform/v1/dv-ios/linear/v1.js  (paginated)
  * EPG per channel: linearedge/GetAiringsForTimeWindowLRC       (12h windows)
  * Fallback       : linearedge/StationDetailLRC

Everything needs a US IP (Amazon geo-locks the linear catalog). No login,
no token, no browser — plain HTTPS GETs routed through a verified-US proxy.

Output: amazon_epg.xml  (flat XMLTV, IPTVBoss-friendly)
"""

import sys
import time
import random
import requests
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DAAPI_HOST = "https://abfq65byepyg.fe.api.amazonvideo.com/cdp/mobile/getDataByTransform/v1/"
EDGE_HOST  = "https://keho.api.amazonvideo.com/cdp/linearedge/"

DEVICE = {"deviceId": "Web", "deviceTypeId": "AOAGZA014O5RE",
          "enabledFeatures": "zeno.daapi.cleanSlate"}
# linearedge uses slightly different key casing:
EDGE_DEVICE = {"deviceID": "Web", "deviceTypeID": "AOAGZA014O5RE",
               "uxLocale": "en_US", "firmware": "1"}

WINDOW_MS = 43_200_000           # 12h per GetAirings call
SEGMENTS  = 4                    # 4 x 12h = 48h of guide (raise for more days)

# Only keep channels whose name contains one of these (case-insensitive).
# Empty list = ALL channels. Example: ["PBS"] for just the PBS feeds.
CHANNEL_NAME_FILTER: list[str] = []

PROXY_API = ("https://api.proxyscrape.com/v2/?request=displayproxies"
             "&protocol=socks4&timeout=10000&country=US&ssl=all&anonymity=elite")
WANT_PROXIES = 5          # size of the working pool to build
PROBE_LIMIT = 60          # how many raw proxies to probe before giving up
REQ_TIMEOUT = 30          # per-request timeout (free proxies are slow)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept": "application/json"})

WORKING_PROXIES: list[str] = []
_proxy_idx = 0

def _pd(hp):
    return {"http": f"socks4://{hp}", "https": f"socks4://{hp}"}

# ---------------------------------------------------------------------------
# Proxy handling — verify against Amazon's OWN endpoint, keep a rotating pool
# ---------------------------------------------------------------------------

def get_proxies():
    try:
        r = requests.get(PROXY_API, timeout=20)
        if r.status_code == 200:
            return [p.strip() for p in r.text.splitlines() if p.strip()]
    except Exception as e:
        print(f"proxy fetch error: {e}")
    return []

# a small daapi call used to prove a proxy can actually reach Amazon fast enough
_PROBE_URL = (DAAPI_HOST + "dv-ios/linear/v1.js"
              "?deviceId=Web&deviceTypeId=AOAGZA014O5RE&enabledFeatures=zeno.daapi.cleanSlate")

def probe(hp):
    try:
        r = requests.get(_PROBE_URL, headers=session.headers, proxies=_pd(hp), timeout=15)
        return r.status_code == 200 and isinstance(r.json(), dict) and "resource" in r.json()
    except Exception:
        return False

def build_pool():
    proxies = get_proxies()
    random.shuffle(proxies)
    for hp in proxies[:PROBE_LIMIT]:
        if probe(hp):
            WORKING_PROXIES.append(hp)
            print(f"  reaches Amazon: {hp}")
            if len(WORKING_PROXIES) >= WANT_PROXIES:
                break
    return WORKING_PROXIES

# ---------------------------------------------------------------------------
# API helpers — rotate across the pool on any failure
# ---------------------------------------------------------------------------

def _get_json(url, tries=None):
    global _proxy_idx
    if not WORKING_PROXIES:
        raise RuntimeError("no working proxies")
    tries = tries or max(3, len(WORKING_PROXIES))
    last = None
    for _ in range(tries):
        hp = WORKING_PROXIES[_proxy_idx % len(WORKING_PROXIES)]
        try:
            r = requests.get(url, headers=session.headers, proxies=_pd(hp), timeout=REQ_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            _proxy_idx += 1   # rotate to the next proxy and retry
    raise last

def call_api(endpoint, params=None):
    """daapi getDataByTransform -> returns the `resource` object (or {})."""
    q = dict(params or {})
    if "pageSize" in q:
        try:
            q["pageSize"] = int(q["pageSize"]) * 10
        except (TypeError, ValueError):
            pass
    q.update(DEVICE)
    qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in q.items())
    data = _get_json(f"{DAAPI_HOST}{endpoint}?{qs}")
    return data.get("resource", {}) or {}

def get_paged(resource, key):
    """Follow paginationLink, concatenating resource[key] across pages."""
    items = list(resource.get(key, []) or [])
    nxt = resource.get("paginationLink")
    guard = 0
    while nxt and guard < 100:
        guard += 1
        rc = nxt.get("requestContext", {})
        page = call_api(rc.get("transform", ""), rc.get("requestParameters", {}))
        if not page or page.get("error"):
            break
        items.extend(page.get(key, []) or [])
        nxt = page.get("paginationLink")
    return items

def get_channels():
    linear = call_api("dv-ios/linear/v1.js")
    out, seen = [], set()
    for col in get_paged(linear, "collections"):
        if col.get("type") != "epgGroup":
            continue
        for it in get_paged(col, "items"):
            tid = it.get("titleId")
            name = (it.get("title") or "").strip()
            if not tid or not name or tid in seen:
                continue
            if CHANNEL_NAME_FILTER and not any(
                    f.lower() in name.lower() for f in CHANNEL_NAME_FILTER):
                continue
            seen.add(tid)
            out.append({"id": tid, "name": name, "logo": it.get("imageURL", "")})
    return out

# ---------------------------------------------------------------------------
# EPG
# ---------------------------------------------------------------------------

def edge_url(endpoint, extra):
    q = dict(extra)
    q.update(EDGE_DEVICE)
    qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in q.items())
    return f"{EDGE_HOST}{endpoint}?{qs}"

def parse_item(item):
    try:
        start = int(item["startTime"]); stop = int(item["endTime"])
    except (KeyError, TypeError, ValueError):
        return None
    if stop <= start:
        return None
    hc = item.get("hierarchyContext") or {}
    return {
        "start": start, "stop": stop,
        "title": item.get("title") or "Unknown",
        "sub": hc.get("episodeTitle") or "",
        "desc": item.get("synopsis") or "",
        "image": item.get("heroImage") or "",
        "airingId": item.get("airingId"),
    }

def station_detail_fallback(site_id):
    """Used when GetAirings reports the station in failedStationIds."""
    progs, seen = [], set()
    try:
        res = _get_json(edge_url("StationDetailLRC",
                                 {"presentationScheme": "living-room-react",
                                  "stationId": site_id})).get("resource", {})
    except Exception:
        return progs
    for cont in res.get("containerList", []) or []:
        epg = cont.get("stationDetailEpg", {}) or {}
        for st in epg.get("items", []) or []:
            for item in st.get("schedule", []) or []:
                p = parse_item(item)
                if p and p["airingId"] not in seen:
                    seen.add(p["airingId"]); progs.append(p)
    return progs

def get_airings(site_id, base_ms):
    progs, seen = [], set()
    for seg in range(SEGMENTS):
        url = edge_url("GetAiringsForTimeWindowLRC",
                       {"stationIds": site_id, "durationInMs": WINDOW_MS,
                        "startTimeEpochInMs": base_ms + seg * WINDOW_MS})
        try:
            res = _get_json(url).get("resource", {})
        except Exception:
            continue
        if res.get("failedStationIds"):
            # station not served by GetAirings -> one detail call covers the day
            return station_detail_fallback(site_id)
        for schedule in res.get("schedule", []) or []:
            for item in schedule:
                p = parse_item(item)
                if p and p["airingId"] not in seen:
                    seen.add(p["airingId"]); progs.append(p)
    return progs

# ---------------------------------------------------------------------------
# XMLTV output
# ---------------------------------------------------------------------------

def xt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

def build_xmltv(channels, epg):
    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
    for ch in channels:
        out.append(
            f'<channel id="{esc(ch["id"])}"><display-name>{esc(ch["name"])}</display-name>'
            + (f'<icon src="{esc(ch["logo"])}" />' if ch["logo"] else "")
            + "</channel>")
    for cid, progs in epg.items():
        for p in progs:
            line = (f'<programme start="{xt(p["start"])}" stop="{xt(p["stop"])}" '
                    f'channel="{esc(cid)}"><title>{esc(p["title"])}</title>')
            if p["sub"]:
                line += f'<sub-title>{esc(p["sub"])}</sub-title>'
            if p["desc"]:
                line += f'<desc>{esc(p["desc"])}</desc>'
            if p["image"]:
                line += f'<icon src="{esc(p["image"])}" />'
            line += "</programme>"
            out.append(line)
    out.append("</tv>")
    return "\n".join(out) + "\n"

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once():
    channels = get_channels()
    print(f"Channels found: {len(channels)}")
    for ch in channels:
        print(f"  [{ch['id']}] {ch['name']}")
    if not channels:
        return None

    # start of current UTC day
    now = datetime.now(timezone.utc)
    base_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    epg, total = {}, 0
    for i, ch in enumerate(channels, 1):
        progs = get_airings(ch["id"], base_ms)
        epg[ch["id"]] = progs
        total += len(progs)
        if i % 20 == 0:
            print(f"  ...{i}/{len(channels)} channels, {total} programmes so far")
        time.sleep(0.1)  # be polite

    print(f"Total programmes: {total}")
    return build_xmltv(channels, epg)

def main():
    print("Building proxy pool (verifying against Amazon)...")
    if not build_pool():
        print("No proxy could reach Amazon this run; re-run (free list churns). Aborting.")
        sys.exit(1)
    print(f"Pool: {len(WORKING_PROXIES)} proxies\n")

    try:
        xml = run_once()
    except Exception as e:
        print(f"ERROR: {e}")
        xml = None

    if not xml:
        print("ERROR: scrape failed; leaving previous file untouched.")
        sys.exit(1)

    with open("amazon_epg.xml", "w", encoding="utf-8") as f:
        f.write(xml)
    print("Wrote amazon_epg.xml")

if __name__ == "__main__":
    main()
