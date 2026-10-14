#!/usr/bin/env python3
"""Conservative duplicate-guest finder and archiver for Zeevou.

Commands:
  fetch    Download guests, contacts and bookings to a data directory (read only).
  analyse  Build a merge plan (plan.json + review.csv). Never writes to Zeevou.
  apply    Archive duplicates that nothing references. Dry run unless --execute.
  rollback Restore guests from the backups taken by apply.

Safety model:
  * Only strong keys match: normalised email or phone. Names are never enough.
  * A shared key (relay emails, agents, families) is skipped, not merged.
  * Names must be compatible and passport/date of birth must not conflict.
  * Duplicates are archived, never deleted. A backup is written first and the
    change is verified afterwards, with automatic restore on any surprise.
  * Only duplicates with no bookings, no balance, no documents and no staff,
    investor, team or brand link are touched. Everything else goes to review.csv.
"""
import argparse
import csv
import json
import os
import re
import sys
import unicodedata
import urllib.request

BASE = os.environ.get("ZEEVOU_API_BASE", "https://hub.zeevou.com/apis").rstrip("/")
RELAY_DOMAINS = (
    "guest.booking.com", "m.airbnb.com", "guest.airbnb.com", "airbnb.com",
    "expediapartnercentral.com", "staysitu.com",
)
RELAY_LOCALS = ("noreply", "no-reply", "donotreply", "guestcare", "bookings", "info", "admin")
MAX_GROUP = 5  # a key shared by more records than this is treated as shared


# ---------- normalisation ----------

def norm_text(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", s.casefold()).strip()


def norm_email(e):
    e = (e or "").strip().casefold()
    if "@" not in e:
        return None
    local, domain = e.rsplit("@", 1)
    if domain in RELAY_DOMAINS or any(local.startswith(p) for p in RELAY_LOCALS):
        return None
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+")[0].replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def norm_phone(p):
    d = re.sub(r"[^\d+]", "", p or "")
    if d.startswith("00"):
        d = "+" + d[2:]
    if d.startswith("+"):
        d = d[1:]
    elif d.startswith("0"):
        d = "44" + d[1:]
    elif d.startswith("44"):
        pass
    else:
        return None  # ambiguous country, do not guess
    d = d.replace("440", "44", 1) if d.startswith("440") else d
    return d if len(d) >= 10 else None


def names_compatible(a, b):
    la, lb = norm_text(a.get("last_name")), norm_text(b.get("last_name"))
    fa, fb = norm_text(a.get("first_name")), norm_text(b.get("first_name"))
    if not la or la != lb or not fa or not fb:
        return False
    return fa == fb or (len(fa) == 1 and fb.startswith(fa)) or (len(fb) == 1 and fa.startswith(fb))


def hard_conflict(a, b):
    for f in ("passport_id", "date_of_birth"):
        if a.get(f) and b.get(f) and str(a[f]).strip() != str(b[f]).strip():
            return True
    return bool(a.get("child")) != bool(b.get("child"))


# ---------- analysis ----------

def keys_for(g):
    ks = set()
    for e in (g.get("email"), g.get("second_email")):
        k = norm_email(e)
        if k:
            ks.add("e:" + k)
    for p in (g.get("mobile_number"), g.get("tel_number")):
        k = norm_phone(p)
        if k:
            ks.add("p:" + k)
    return ks


def usage_counts(bookings):
    use = {}
    for b in bookings:
        for f in ("guest", "person_booking"):
            gid = (b.get(f) or {}).get("id")
            if gid:
                use[gid] = use.get(gid, 0) + 1
    return use


def protected_ids(contacts):
    out = set()
    for c in contacts:
        if any(c.get(k) for k in ("staff", "investor", "team", "brand")) and c.get("guest"):
            out.add(int(c["guest"].rstrip("/").split("/")[-1]))
    return out


def completeness(g):
    return sum(1 for v in g.values() if v not in (None, "", [], {}))


def analyse(guests, contacts, bookings):
    use, prot = usage_counts(bookings), protected_ids(contacts)
    live = {g["id"]: g for g in guests if not g.get("archive")}
    groups, skipped = {}, []
    for gid, g in live.items():
        for k in keys_for(g):
            groups.setdefault(k, []).append(gid)

    parent = {i: i for i in live}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    conflicts = []
    for k, ids in groups.items():
        if len(ids) < 2:
            continue
        if len(ids) > MAX_GROUP:
            skipped.append({"key": k, "reason": "shared key", "guest_ids": ids})
            continue
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ga, gb = live[a], live[b]
                if hard_conflict(ga, gb):
                    conflicts.append({"key": k, "guest_ids": [a, b], "reason": "passport/dob/child conflict"})
                elif names_compatible(ga, gb):
                    parent[find(a)] = find(b)
                else:
                    conflicts.append({"key": k, "guest_ids": [a, b], "reason": "same key, different names"})

    clusters = {}
    for i in live:
        clusters.setdefault(find(i), []).append(i)

    plan = {"safe_archive": [], "manual_review": [], "skipped_shared_keys": skipped, "key_conflicts": conflicts}
    for members in (m for m in clusters.values() if len(m) > 1):
        def rank(i):
            g = live[i]
            return (i in prot, use.get(i, 0), bool(g.get("id_verified")), completeness(g), -i)
        members.sort(key=rank, reverse=True)
        keep, losers = members[0], members[1:]
        pairwise_ok = all(names_compatible(live[a], live[b]) and not hard_conflict(live[a], live[b])
                          for n, a in enumerate(members) for b in members[n + 1:])
        for lid in losers:
            g = live[lid]
            blockers = []
            if not pairwise_ok:
                blockers.append("cluster has members that do not match each other")
            if lid in prot:
                blockers.append("linked to staff/investor/team/brand")
            if use.get(lid):
                blockers.append(f"{use[lid]} booking reference(s)")
            if (g.get("balance") or {}).get("value"):
                blockers.append("non-zero balance")
            if any(g.get(f) for f in ("guest_documents", "signable", "corporations", "guest_notes", "notes")):
                blockers.append("has documents/notes/corporations")
            item = {"keep": keep, "duplicate": lid,
                    "keep_name": f"{live[keep].get('first_name')} {live[keep].get('last_name')}",
                    "duplicate_name": f"{g.get('first_name')} {g.get('last_name')}",
                    "matched_on": sorted(keys_for(g) & keys_for(live[keep])),
                    "blockers": blockers}
            plan["safe_archive" if not blockers else "manual_review"].append(item)
    return plan


def write_plan(plan, outdir):
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "plan.json"), "w") as f:
        json.dump(plan, f, indent=2)
    with open(os.path.join(outdir, "review.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "keep_id", "keep_name", "duplicate_id", "duplicate_name", "matched_on", "why"])
        for s in ("safe_archive", "manual_review"):
            for i in plan[s]:
                w.writerow([s, i["keep"], i["keep_name"], i["duplicate"], i["duplicate_name"],
                            ";".join(i["matched_on"]), "; ".join(i["blockers"])])
        for c in plan["key_conflicts"] + plan["skipped_shared_keys"]:
            w.writerow(["not_merged", "", "", ";".join(map(str, c["guest_ids"])), "", c["key"], c["reason"]])


# ---------- API ----------

def api(method, path, body=None):
    tok = os.environ.get("ZEEVOU_TOKEN")
    if not tok:
        sys.exit("Set ZEEVOU_TOKEN (and optionally ZEEVOU_API_BASE).")
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def members(resp):
    return resp if isinstance(resp, list) else resp.get("hydra:member") or resp.get("member") or []


def fetch_all(path, limit=100):
    out, page = [], 1
    while True:
        batch = members(api("GET", f"{path}?limit={limit}&page={page}"))
        out += batch
        if len(batch) < limit:
            return out
        page += 1


STABLE = {"updated_at"}


def strip(g):
    return {k: v for k, v in g.items() if k not in STABLE}


def apply_plan(plan, outdir, execute):
    bdir = os.path.join(outdir, "backups")
    os.makedirs(bdir, exist_ok=True)
    for item in plan["safe_archive"]:
        gid = item["duplicate"]
        print(f"{'ARCHIVE' if execute else 'would archive'} #{gid} {item['duplicate_name']} (keep #{item['keep']})")
        if not execute:
            continue
        before = api("GET", f"/guests/{gid}")
        if before.get("archive"):
            continue
        with open(os.path.join(bdir, f"guest_{gid}.json"), "w") as f:
            json.dump(before, f)
        api("PUT", f"/guests/{gid}", {**before, "archive": True})
        after = api("GET", f"/guests/{gid}")
        if not after.get("archive") or strip({**after, "archive": False}) != strip({**before, "archive": False}):
            api("PUT", f"/guests/{gid}", before)
            sys.exit(f"Guest {gid}: unexpected change after archive. Restored and stopped.")


def rollback(outdir):
    bdir = os.path.join(outdir, "backups")
    for fn in sorted(os.listdir(bdir)):
        rec = json.load(open(os.path.join(bdir, fn)))
        api("PUT", f"/guests/{rec['id']}", rec)
        print("restored", rec["id"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "analyse", "apply", "rollback"])
    ap.add_argument("--dir", default="dedupe_data")
    ap.add_argument("--execute", action="store_true", help="apply: actually write (default is dry run)")
    a = ap.parse_args()
    d = a.dir
    if a.command == "fetch":
        os.makedirs(d, exist_ok=True)
        for name, path in (("guests", "/guests"), ("contacts", "/contacts"), ("bookings", "/bookings")):
            data = fetch_all(path)
            json.dump(data, open(os.path.join(d, name + ".json"), "w"))
            print(name, len(data))
    elif a.command == "analyse":
        load = lambda n: json.load(open(os.path.join(d, n + ".json")))
        plan = analyse(load("guests"), load("contacts"), load("bookings"))
        write_plan(plan, d)
        print({k: len(v) for k, v in plan.items()}, "-> see", os.path.join(d, "review.csv"))
    elif a.command == "apply":
        apply_plan(json.load(open(os.path.join(d, "plan.json"))), d, a.execute)
    else:
        rollback(d)


if __name__ == "__main__":
    main()
