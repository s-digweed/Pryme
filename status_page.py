"""
status_page.py - shared by the PlexTV and SamsungTVPlus repos.

Writes <out_json> for status.html: every channel per region with its VPN status,
plus a rolling log of channels Added / Removed / Renamed per region per day.

    from status_page import update_status
    update_status("Plex", regions, "status.json", "status_state.json")

regions = { "us": {"name": "United States",
                   "channels": [ {"id":..., "name":..., "logo":..., "group":...,
                                  "status": "free" | "blocked" | "unknown",
                                  "detail": "short text"} , ... ] }, ... }

The state file keeps the previous channel list per region (id -> name) and the
change log; commit it every run or the history is lost.
"""
import gzip
import json
import os
import re
from datetime import datetime, timedelta, timezone

CHANGE_DAYS = 7          # keep this many days of Added/Removed/Renamed history


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


_PROG = re.compile(r'<programme\b([^>]*)>')
_ATTR_STOP = re.compile(r'stop="(\d{14})\s*([+-]\d{4})?"')
_ATTR_CH = re.compile(r'channel="([^"]*)"')


def epg_stats(path):
    """Guide reach of an XMLTV file (.xml or .xml.gz), read line by line:
    {"epg_until": latest stop anywhere, "epg_median_until": the median channel's last
     stop (what a typical channel shows), "epg_channels": channels with any programme}.
    Times are ISO UTC. Returns {} if the file can't be read."""
    for p in (path, path + ".gz" if not path.endswith(".gz") else path[:-3]):
        if not p or not os.path.exists(p):
            continue
        opener = gzip.open if p.endswith(".gz") else open
        last = {}
        try:
            with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    for attrs in _PROG.findall(line):
                        ms, mc = _ATTR_STOP.search(attrs), _ATTR_CH.search(attrs)
                        if not ms or not mc:
                            continue
                        dt = datetime.strptime(ms.group(1), "%Y%m%d%H%M%S")
                        off = ms.group(2)
                        if off:
                            sign = 1 if off[0] == "+" else -1
                            dt -= sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
                        if dt > last.get(mc.group(1), datetime.min):
                            last[mc.group(1)] = dt
        except Exception:
            continue
        if last:
            ends = sorted(last.values())
            iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
            return {"epg_until": iso(ends[-1]), "epg_median_until": iso(ends[len(ends) // 2]),
                    "epg_channels": len(ends)}
    return {}


def update_status(service, regions, out_json, state_file, now=None, health=None):
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state = _load(state_file, {"regions": {}, "changes": []})
    prev = state.get("regions", {})
    changes = state.get("changes", [])
    first_run = not prev

    for r, info in regions.items():
        cur = {c["id"]: c["name"] for c in info.get("channels", [])}
        if not cur:                       # region failed this run: keep old snapshot, no diff
            continue
        old = prev.get(r)
        if old is None:                   # region seen for the first time: baseline only
            prev[r] = cur
            continue
        for cid, name in cur.items():
            if cid not in old:
                changes.append({"d": today, "r": r, "t": "added", "id": cid, "n": name})
            elif old[cid] != name:
                changes.append({"d": today, "r": r, "t": "renamed", "id": cid, "n": name, "o": old[cid]})
        for cid, name in old.items():
            if cid not in cur:
                changes.append({"d": today, "r": r, "t": "removed", "id": cid, "n": name})
        prev[r] = cur

    cutoff = (now - timedelta(days=CHANGE_DAYS - 1)).strftime("%Y-%m-%d")
    seen, kept = set(), []
    for c in changes:                     # prune old days, drop exact repeats
        key = (c["d"], c["r"], c["t"], c["id"], c.get("n"), c.get("o"))
        if c["d"] >= cutoff and key not in seen:
            seen.add(key); kept.append(c)
    state = {"regions": prev, "changes": kept,
             "since": state.get("since") or today}
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))

    # health: freshness of the M3U / EPG plus notes. Keys not supplied this run (e.g.
    # the EPG on an M3U-only run) carry over from the previous status.json.
    prev_health = _load(out_json, {}).get("health", {})
    h = {**prev_health, **{k: v for k, v in (health or {}).items() if v is not None}}
    h["notes"] = (health or {}).get("notes", [])

    out = {
        "service": service,
        "health": h,
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tracking_since": state["since"],
        "first_run": first_run,
        "regions": {
            r: {"name": info.get("name", r.upper()),
                # compact rows: [id, name, logo, group, status, detail]
                "channels": [[c["id"], c["name"], c.get("logo", ""), c.get("group", ""),
                              c.get("status", "unknown"), c.get("detail", "")]
                             for c in sorted(info.get("channels", []), key=lambda c: c["name"].lower())]}
            for r, info in regions.items()
        },
        "changes": sorted(kept, key=lambda c: (c["d"], c["r"], c["t"], c["n"].lower()), reverse=True),
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(i["channels"]) for i in out["regions"].values())
    print(f"Status page data: {out_json} ({n} channel entries, {len(kept)} changes in last {CHANGE_DAYS} days)")
