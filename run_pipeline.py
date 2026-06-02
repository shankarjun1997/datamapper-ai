#!/usr/bin/env python3
"""
DataMapper pipeline automation — Frontier → Verizon BQ
Starts a session, loads source + 4 target schemas, runs mapping to Gate 2,
approves, and generates BigQuery SQL.

Usage:
    python3 run_pipeline.py
(Server must already be running on port 7788 — use start_and_map.sh to do both)
"""

import json, os, sys, time, requests

BASE       = "http://localhost:7788"
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_files")


def p(msg):
    print(msg, flush=True)


def check_server():
    try:
        r = requests.get(f"{BASE}/api/health", timeout=5)
        r.raise_for_status()
        p(f"✓ Server healthy: {r.json()}")
        return True
    except Exception as e:
        p(f"✗ Server not reachable: {e}")
        return False


def create_session():
    r = requests.post(f"{BASE}/api/sessions", json={"name": "Frontier→Verizon BQ Mapping"})
    r.raise_for_status()
    d = r.json()
    sid = d.get("session_id") or d.get("id")
    p(f"✓ Created session: {sid}")
    return sid


def set_bq_config(sid):
    r = requests.post(f"{BASE}/api/sessions/{sid}/bq-config", json={
        "project":       "gcpproject-438715",
        "dataset":       "sqlgen_mockup",
        "region":        "us-central1",
        "gcp_creds":     "",
        "target_tables": "verizon_customer_profile,verizon_service_ops,verizon_network_metrics,verizon_billing_usage",
    })
    r.raise_for_status()
    p("✓ BQ config set: gcpproject-438715.sqlgen_mockup")


def upload_source(sid):
    path = os.path.join(SCHEMA_DIR, "frontier_schema.csv")
    with open(path, "rb") as f:
        r = requests.post(
            f"{BASE}/api/sessions/{sid}/upload",
            files={"file": ("frontier_schema.csv", f, "text/csv")},
        )
    r.raise_for_status()
    d = r.json()
    p(f"✓ Source uploaded: {d.get('tables',1)} table(s), {d.get('columns','?')} columns")


def upload_targets(sid):
    target_files = [
        "verizon_customer_profile.csv",
        "verizon_service_ops.csv",
        "verizon_network_metrics.csv",
        "verizon_billing_usage.csv",
    ]
    handles, files = [], []
    for fname in target_files:
        fh = open(os.path.join(SCHEMA_DIR, fname), "rb")
        handles.append(fh)
        files.append(("files", (fname, fh, "text/csv")))
    try:
        r = requests.post(f"{BASE}/api/sessions/{sid}/target-files", files=files)
        r.raise_for_status()
        d = r.json()
        p(f"✓ Targets uploaded: {d.get('tables',4)} table(s) — {d.get('table_names', target_files)}")
    finally:
        for fh in handles:
            fh.close()


def run_pipeline(sid):
    r = requests.post(f"{BASE}/api/sessions/{sid}/run")
    r.raise_for_status()
    p(f"✓ Pipeline started")


def poll_until_gate2(sid, timeout=300):
    p("⏳ Waiting for pipeline to reach Gate 2 review…")
    start, last_stage = time.time(), None
    while time.time() - start < timeout:
        r  = requests.get(f"{BASE}/api/sessions/{sid}")
        r.raise_for_status()
        s  = r.json()
        stage, status = s.get("stage"), s.get("status")
        if stage != last_stage:
            p(f"  Stage: {stage} | Status: {status}")
            last_stage = stage
        if status == "review":
            p(f"✓ Reached Gate 2 after {int(time.time()-start)}s")
            return True
        if status == "error":
            p(f"✗ Pipeline error: {s.get('error')}")
            return False
        time.sleep(3)
    p("✗ Timed out waiting for Gate 2")
    return False


def show_mappings(sid):
    r = requests.get(f"{BASE}/api/sessions/{sid}/mappings")
    r.raise_for_status()
    data     = r.json()
    mappings = data.get("mappings", [])
    stats    = data.get("stats", {})

    p(f"\n{'='*65}")
    p(f"MAPPING RESULTS — {len(mappings)} columns")
    p(f"  Auto-mapped : {stats.get('mapped',0)}")
    p(f"  Needs review: {stats.get('review',0)}")
    p(f"  Unmapped    : {stats.get('unmapped',0)}")
    p(f"  Avg confidence: {stats.get('avg_confidence',0):.1%}")
    p(f"{'='*65}")

    by_tgt = {}
    for m in mappings:
        key = m.get("tgt_table") or "UNMAPPED"
        by_tgt.setdefault(key, []).append(m)

    for tgt_table, rows in sorted(by_tgt.items()):
        p(f"\n  ▸ {tgt_table}  ({len(rows)} mappings)")
        for m in rows:
            src    = m.get("src_field", "")
            tgt    = m.get("tgt_column", "")
            mtype  = m.get("mapping_type", "")
            conf   = m.get("confidence", 0)
            logic  = m.get("business_logic", "") or ""
            arrow  = "→" if tgt else "✕"
            cfmt   = f"{conf:.0%}" if tgt else "  -"
            lstr   = f"  [{logic}]" if logic and logic.lower() != "direct" else ""
            p(f"    {src:<42} {arrow} {tgt:<35} {mtype:<12} {cfmt}{lstr}")
    return mappings


def approve_gate2(sid):
    r = requests.post(f"{BASE}/api/sessions/{sid}/approve-gate2")
    r.raise_for_status()
    p(f"\n✓ Gate 2 approved — SQL generation started")


def poll_until_done(sid, timeout=120):
    p("⏳ Waiting for SQL generation…")
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/api/sessions/{sid}")
        r.raise_for_status()
        s = r.json()
        if s.get("status") == "done":
            p("✓ SQL generation complete!")
            return True
        if s.get("status") == "error":
            p(f"✗ SQL gen error: {s.get('error')}")
            return False
        time.sleep(3)
    p("✗ Timed out waiting for SQL")
    return False


def save_outputs(sid):
    out_dir = os.path.dirname(os.path.abspath(__file__))

    # SQL
    r = requests.get(f"{BASE}/api/sessions/{sid}/sql")
    r.raise_for_status()
    d = r.json()
    if d.get("ready") or d.get("sql"):
        sql = d.get("sql", "")
        sql_path = os.path.join(out_dir, "generated_mapping.sql")
        with open(sql_path, "w") as f:
            f.write(sql)
        p(f"✓ SQL saved  → {sql_path}")

    # CSV export
    r2 = requests.get(f"{BASE}/api/sessions/{sid}/export/csv")
    r2.raise_for_status()
    csv_path = os.path.join(out_dir, "mapping_export.csv")
    with open(csv_path, "wb") as f:
        f.write(r2.content)
    p(f"✓ CSV saved  → {csv_path}")

    # Table mapping CSV
    try:
        r3 = requests.get(f"{BASE}/api/sessions/{sid}/export/table-mappings")
        r3.raise_for_status()
        tbl_path = os.path.join(out_dir, "table_mapping_summary.csv")
        with open(tbl_path, "wb") as f:
            f.write(r3.content)
        p(f"✓ Table map  → {tbl_path}")
    except Exception:
        pass


def main():
    p("\n" + "━"*65)
    p("  DataMapper — Frontier → Verizon BQ  (mockup pipeline)")
    p("━"*65 + "\n")

    if not check_server():
        p("\nPlease start the server first:\n  cd ~/Desktop/Projects/dmapper && bash run.sh")
        sys.exit(1)

    sid = create_session()
    set_bq_config(sid)
    upload_source(sid)
    upload_targets(sid)
    run_pipeline(sid)

    if not poll_until_gate2(sid):
        p("Pipeline did not reach Gate 2. Check server logs.")
        sys.exit(1)

    show_mappings(sid)
    approve_gate2(sid)

    if poll_until_done(sid):
        save_outputs(sid)

    p(f"\n✓ Done! Session: {sid}")
    p(f"  Live UI: {BASE}?session={sid}\n")


if __name__ == "__main__":
    main()
