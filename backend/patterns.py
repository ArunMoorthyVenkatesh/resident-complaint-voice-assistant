"""
patterns.py — cross-complaint pattern detection for the admin dashboard.

Individually, complaints look unrelated. Looked at together, they can reveal
things a human skimming a list wouldn't catch:
  - CLUSTER:   several complaints of the same type in the same area within a
               short window -- e.g. 5 aircon leaks in one block in 2 weeks
               might be one shared pipe issue, not 5 separate broken units.
  - RECURRING: the same spot/type keeps coming back -- a sign a past "fix"
               didn't actually hold.
  - SPIKE:     a complaint category suddenly jumps well above its normal
               monthly rate -- e.g. electrical complaints tripling might mean
               aging wiring, not five unrelated incidents.

Deliberately simple (no ML, no new dependencies) -- rule-based grouping over
what's already in DynamoDB. Good enough to surface real signal; not meant to
be a precise clustering algorithm.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SGT = timezone(timedelta(hours=8))

CLUSTER_WINDOW_DAYS   = 14
CLUSTER_MIN_COUNT     = 3
RECURRING_WINDOW_DAYS = 90
RECURRING_MIN_COUNT   = 2
SPIKE_MIN_RATIO       = 2.0
SPIKE_MIN_COUNT       = 3  # ignore spikes like "1 -> 2", too noisy to be meaningful


def _parse_ts(ts: str):
    try:
        return datetime.strptime(ts.replace(" SGT", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SGT)
    except (ValueError, AttributeError):
        return None


def _location_key(location: str) -> str:
    """
    Locations are free text ("Block 204, near the lift", "5th floor toilet").
    Extract a block/unit number if present -- the strongest, most reliable
    grouping signal -- else fall back to the first few words, lowercased.
    """
    if not location:
        return "unspecified"
    loc = location.lower()
    m = re.search(r"\b(?:block|blk|b)\s*#?\s*(\d+)\b", loc)
    if m:
        return f"block {m.group(1)}"
    words = re.findall(r"[a-z0-9]+", loc)
    return " ".join(words[:3]) if words else "unspecified"


def detect_patterns(complaints: list) -> list:
    now = datetime.now(SGT)
    enriched = []
    for c in complaints:
        ts = _parse_ts(c.get("timestamp", ""))
        if ts is None:
            continue
        enriched.append({**c, "_ts": ts, "_loc_key": _location_key(c.get("location", ""))})

    alerts = []
    alerts.extend(_detect_clusters(enriched, now))
    alerts.extend(_detect_recurring(enriched, now))
    alerts.extend(_detect_spikes(enriched, now))
    return alerts


def _detect_clusters(enriched, now):
    cutoff = now - timedelta(days=CLUSTER_WINDOW_DAYS)
    groups = defaultdict(list)
    for c in enriched:
        if c["_ts"] >= cutoff:
            groups[(c["_loc_key"], c.get("complaint_type", ""))].append(c)

    alerts = []
    for (loc_key, ctype), items in groups.items():
        if len(items) >= CLUSTER_MIN_COUNT and loc_key != "unspecified" and ctype:
            alerts.append({
                "type": "cluster",
                "severity": "high",
                "message": f"{len(items)} {ctype} complaints near \"{loc_key}\" in the last {CLUSTER_WINDOW_DAYS} days — possibly one shared issue rather than {len(items)} separate ones.",
                "complaint_type": ctype,
                "location_key": loc_key,
                "count": len(items),
                "complaint_ids": [c["complaint_id"] for c in items],
            })
    return sorted(alerts, key=lambda a: -a["count"])


def _detect_recurring(enriched, now):
    cutoff = now - timedelta(days=RECURRING_WINDOW_DAYS)
    groups = defaultdict(list)
    for c in enriched:
        if c["_ts"] >= cutoff:
            groups[(c["_loc_key"], c.get("complaint_type", ""))].append(c)

    alerts = []
    for (loc_key, ctype), items in groups.items():
        if len(items) >= RECURRING_MIN_COUNT and loc_key != "unspecified" and ctype:
            items.sort(key=lambda c: c["_ts"])
            # A cluster within the short window is "one incident, many callers" --
            # recurring is specifically about the SAME spot coming back over time,
            # so skip groups already flagged as a tight cluster.
            span_days = (items[-1]["_ts"] - items[0]["_ts"]).days
            if span_days < CLUSTER_WINDOW_DAYS:
                continue
            alerts.append({
                "type": "recurring",
                "severity": "medium",
                "message": f"\"{loc_key}\" has had {len(items)} {ctype} complaints over {span_days} days — may not have been properly fixed the first time.",
                "complaint_type": ctype,
                "location_key": loc_key,
                "count": len(items),
                "complaint_ids": [c["complaint_id"] for c in items],
            })
    return sorted(alerts, key=lambda a: -a["count"])


def _detect_spikes(enriched, now):
    this_month_key = (now.year, now.month)
    monthly_counts = defaultdict(lambda: defaultdict(int))  # {complaint_type: {(y,m): count}}
    for c in enriched:
        ctype = c.get("complaint_type", "")
        if ctype:
            key = (c["_ts"].year, c["_ts"].month)
            monthly_counts[ctype][key] += 1

    alerts = []
    for ctype, months in monthly_counts.items():
        this_count = months.get(this_month_key, 0)
        prior = {k: v for k, v in months.items() if k != this_month_key}
        if this_count < SPIKE_MIN_COUNT or not prior:
            continue
        avg_prior = sum(prior.values()) / len(prior)
        if avg_prior > 0 and this_count / avg_prior >= SPIKE_MIN_RATIO:
            alerts.append({
                "type": "spike",
                "severity": "medium",
                "message": f"{ctype} complaints this month ({this_count}) are {this_count / avg_prior:.1f}x the recent average ({avg_prior:.1f}/month) — worth investigating as a building-wide issue.",
                "complaint_type": ctype,
                "count": this_count,
                "avg_prior_months": round(avg_prior, 1),
            })
    return sorted(alerts, key=lambda a: -a["count"])
