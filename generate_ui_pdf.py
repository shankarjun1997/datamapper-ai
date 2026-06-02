from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors
import io

W, H = A4  # 595 x 842 pt

# ── Palette (xREF dark theme) ────────────────────────────────
BG0    = HexColor("#080b14")
BG1    = HexColor("#0d1117")
BG2    = HexColor("#111827")
BG3    = HexColor("#1a2235")
BORDER = HexColor("#1f2d42")
BORDER2= HexColor("#2a3d55")
TEXT1  = HexColor("#e2e8f0")
TEXT2  = HexColor("#94a3b8")
TEXT3  = HexColor("#64748b")
CYAN   = HexColor("#00d4ff")
CYAN_D = HexColor("#0099cc")
GREEN  = HexColor("#10b981")
ORANGE = HexColor("#f97316")
YELLOW = HexColor("#fbbf24")
PURPLE = HexColor("#a78bfa")
RED    = HexColor("#f87171")

OUT = "/sessions/great-affectionate-bohr/mnt/outputs/xref_datamapper_ui.pdf"

c = canvas.Canvas(OUT, pagesize=A4)
c.setTitle("xREF Agent — DataMapper AI")
c.setAuthor("xREF Agent v2.0")
c.setSubject("Frontier → Verizon BQ Mapping Session")

def bg(cv):
    cv.setFillColor(BG0)
    cv.rect(0, 0, W, H, fill=1, stroke=0)

def card(cv, x, y, w, h, fill=BG2, stroke=BORDER, radius=6):
    cv.setFillColor(fill)
    cv.setStrokeColor(stroke)
    cv.setLineWidth(0.5)
    cv.roundRect(x, y, w, h, radius, fill=1, stroke=1)

def label(cv, x, y, text, size=9, color=TEXT3, font="Helvetica"):
    cv.setFont(font, size)
    cv.setFillColor(color)
    cv.drawString(x, y, text)

def label_r(cv, x, y, text, size=9, color=TEXT3, font="Helvetica"):
    cv.setFont(font, size)
    cv.setFillColor(color)
    cv.drawRightString(x, y, text)

def pill(cv, x, y, text, bg_col, text_col, size=8, w=None):
    cv.setFont("Helvetica-Bold", size)
    tw = cv.stringWidth(text, "Helvetica-Bold", size)
    pw = (w or tw) + 10
    cv.setFillColor(bg_col)
    cv.roundRect(x, y-2, pw, 13, 4, fill=1, stroke=0)
    cv.setFillColor(text_col)
    cv.drawCentredString(x + pw/2, y+2, text)
    return pw

def dot(cv, x, y, col, r=4):
    cv.setFillColor(col)
    cv.circle(x, y, r, fill=1, stroke=0)

def hline(cv, x1, x2, y, col=BORDER, lw=0.5):
    cv.setStrokeColor(col)
    cv.setLineWidth(lw)
    cv.line(x1, y, x2, y)

def conf_color(pct):
    if pct >= 85: return GREEN
    if pct >= 65: return YELLOW
    return RED

# ══════════════════════════════════════════════════════════════
# PAGE 1 — COVER
# ══════════════════════════════════════════════════════════════
bg(c)

# Triangle logo
c.setFillColor(HexColor("#1a34a8"))
c.setStrokeColor(HexColor("#0fd4b0"))
c.setLineWidth(1.5)
tri_cx, tri_cy = W/2, H - 160
pts = [(tri_cx, tri_cy+55), (tri_cx+48, tri_cy-27), (tri_cx-48, tri_cy-27)]
p = c.beginPath()
p.moveTo(*pts[0]); p.lineTo(*pts[1]); p.lineTo(*pts[2]); p.close()
c.drawPath(p, fill=1, stroke=0)

# Diagonal lines inside triangle (clip manually)
c.setStrokeColor(HexColor("#0fd4b0"))
c.setLineWidth(2.5)
for dx in [-18, -6, 6, 18]:
    c.line(tri_cx+dx-18, tri_cy-27, tri_cx+dx+18, tri_cy+55)

# Ellipse ring
c.setStrokeColor(HexColor("#0fd4b0"))
c.setLineWidth(2)
c.saveState()
c.transform(1, 0, -0.3, 0.42, tri_cx, tri_cy+12)
c.ellipse(-46, -18, 46, 18, fill=0, stroke=1)
c.restoreState()

# Title
c.setFont("Helvetica-Bold", 26)
c.setFillColor(TEXT1)
c.drawCentredString(W/2, H - 250, "xREF Agent")
c.setFont("Helvetica", 14)
c.setFillColor(CYAN)
c.drawCentredString(W/2, H - 270, "DataMapper AI  v2.0")

# Subtitle
c.setFont("Helvetica", 10)
c.setFillColor(TEXT3)
c.drawCentredString(W/2, H - 295, "Agentic Source-to-Target Column Mapping")

hline(c, W/2-80, W/2+80, H-308, BORDER2, 0.5)

# Session info card
card(c, W/2-130, H-390, 260, 70, BG2, BORDER)
label(c, W/2-115, H-335, "ACTIVE SESSION", 7, TEXT3, "Helvetica-Bold")
label(c, W/2-115, H-350, "Frontier → Verizon BQ Mapping", 11, TEXT1, "Helvetica-Bold")
label(c, W/2-115, H-365, "60 source cols  ·  4 target tables  ·  45 mapped", 9, TEXT2)
pill(c, W/2-115, H-383, "GATE 2 — AWAITING REVIEW", HexColor("#001f2e"), CYAN, 8)

# Stats row
stats = [("60", "source cols"), ("4", "target tables"), ("45", "auto-mapped"), ("75%", "avg confidence")]
sx = 40
for val, lbl in stats:
    card(c, sx, H-480, 115, 60, BG2, BORDER)
    c.setFont("Helvetica-Bold", 20)
    c.setFillColor(CYAN)
    c.drawCentredString(sx+57, H-454, val)
    label(c, sx+57-30, H-468, lbl, 8, TEXT3)
    sx += 128

# Pipeline overview
label(c, 40, H-510, "PIPELINE STAGES", 7, TEXT3, "Helvetica-Bold")
stages = [
    ("L1", "Parse source schema", "60 cols · 1 table", GREEN, "done"),
    ("L2", "Load target schemas", "4 Verizon BQ tables · 45 cols", GREEN, "done"),
    ("L3", "LLM semantic mapping", "45 mapped · 11 review · 4 unmapped", GREEN, "done"),
    ("G2", "Gate 2 — Human review", "Awaiting approval", CYAN, "waiting"),
    ("L4", "Generate BigQuery SQL", "Pending gate approval", TEXT3, "pending"),
]
sy = H - 530
for tag, name, detail, col, status in stages:
    card(c, 40, sy-22, W-80, 28, BG2, BORDER, 5)
    dot(c, 62, sy-8, col)
    label(c, 75, sy-5, f"{tag}  {name}", 9, TEXT1, "Helvetica-Bold")
    label_r(c, W-48, sy-5, detail, 8, TEXT2)
    sy -= 34

# Footer
label(c, 40, 30, "xREF Agent  ·  DataMapper AI v2.0  ·  gcpproject-438715.sqlgen_mockup", 8, TEXT3)
label_r(c, W-40, 30, "1 / 5", 8, TEXT3)

c.showPage()

# ══════════════════════════════════════════════════════════════
# PAGE 2 — DASHBOARD
# ══════════════════════════════════════════════════════════════
bg(c)

# Topbar
card(c, 0, H-44, W, 44, BG1, BG1, 0)
hline(c, 0, W, H-44, BORDER)
label(c, 16, H-28, "☰", 13, TEXT3)
label(c, 36, H-28, "xREF Agent  /", 10, TEXT3)
label(c, 110, H-28, "Dashboard", 10, TEXT1, "Helvetica-Bold")
pill(c, W-160, H-34, "Frontier→Verizon · REVIEW", HexColor("#001520"), CYAN, 7)

# Page title
label(c, 30, H-70, "Dashboard", 16, TEXT1, "Helvetica-Bold")
label(c, 30, H-85, "Current session overview", 9, TEXT2)

# Stat cards 2x2
stats2 = [
    ("4", "Target tables", CYAN),
    ("60", "Source columns", TEXT1),
    ("45", "Auto-mapped", GREEN),
    ("11", "Needs review", YELLOW),
]
cols_x = [30, W/2+10]
rows_y = [H-165, H-230]
for i, (val, lbl, col) in enumerate(stats2):
    cx = cols_x[i % 2]
    cy = rows_y[i // 2]
    card(c, cx, cy, W/2-50, 52, BG2, BORDER)
    c.setFont("Helvetica-Bold", 22)
    c.setFillColor(col)
    c.drawCentredString(cx + (W/2-50)/2, cy+28, val)
    label(c, cx + (W/2-50)/2 - 25, cy+12, lbl, 8, TEXT3)

# Gate 2 banner
card(c, 30, H-300, W-60, 60, HexColor("#00111a"), HexColor("#003d5c"), 8)
c.setStrokeColor(CYAN)
c.setLineWidth(2)
c.line(30, H-300, 30, H-240)
label(c, 46, H-252, "⏸  GATE 2 — AWAITING REVIEW", 8, CYAN, "Helvetica-Bold")
label(c, 46, H-266, "Frontier → Verizon BQ  ·  60 source cols mapped across 4 target tables", 9, TEXT2)
label(c, 46, H-280, "Review mapping table and approve to generate BigQuery SQL statements.", 8, TEXT3)
pill(c, 46, H-297, "Review & Approve Mappings →", CYAN, BG0, 9)

# Recent sessions
label(c, 30, H-325, "RECENT SESSIONS", 7, TEXT3, "Helvetica-Bold")
sessions = [
    ("Frontier → Verizon BQ", "60 cols  ·  4 tables  ·  75% confidence", "REVIEW", HexColor("#001520"), CYAN),
    ("Legacy CRM → BQ Warehouse", "128 cols  ·  8 tables  ·  91% confidence", "DONE", HexColor("#001a0f"), GREEN),
    ("Oracle ERP → Snowflake", "84 cols  ·  5 tables  ·  88% confidence", "DONE", HexColor("#001a0f"), GREEN),
]
sy = H - 345
for name, meta, status, sbg, scol in sessions:
    card(c, 30, sy-30, W-60, 38, BG2, BORDER, 6)
    label(c, 46, sy-10, name, 10, TEXT1, "Helvetica-Bold")
    label(c, 46, sy-24, meta, 8, TEXT2)
    pill(c, W-100, sy-22, status, sbg, scol, 7)
    sy -= 46

label(c, 40, 30, "xREF Agent  ·  DataMapper AI v2.0", 8, TEXT3)
label_r(c, W-40, 30, "2 / 5", 8, TEXT3)
c.showPage()

# ══════════════════════════════════════════════════════════════
# PAGE 3 — PIPELINE VIEW
# ══════════════════════════════════════════════════════════════
bg(c)
card(c, 0, H-44, W, 44, BG1, BG1, 0)
hline(c, 0, W, H-44, BORDER)
label(c, 36, H-28, "xREF Agent  /", 10, TEXT3)
label(c, 110, H-28, "Run Pipeline", 10, TEXT1, "Helvetica-Bold")

label(c, 30, H-70, "Pipeline Run", 16, TEXT1, "Helvetica-Bold")
label(c, 30, H-85, "Frontier → Verizon BQ  ·  Session #f8a3c2", 9, TEXT2)

# Progress bar
label(c, 30, H-105, "PROGRESS", 7, TEXT3, "Helvetica-Bold")
card(c, 30, H-122, W-60, 10, BG3, BORDER, 5)
c.setFillColor(CYAN)
c.roundRect(30, H-122, (W-60)*0.75, 10, 5, fill=1, stroke=0)
label_r(c, W-30, H-113, "75% complete", 8, CYAN)

# Stage details
label(c, 30, H-148, "PIPELINE STAGES", 7, TEXT3, "Helvetica-Bold")

stage_detail = [
    ("L1", "Parse Source Schema", "Parsed frontier_schema.csv", "60 columns detected across 1 table", GREEN, True),
    ("L2", "Load Target Schemas", "Loaded 4 custom target files", "45 total target columns across 4 BQ tables", GREEN, True),
    ("L3", "LLM Semantic Mapping", "Claude claude-sonnet-4-6 · 4 batches", "45 auto-mapped  ·  11 need review  ·  4 unmapped", GREEN, True),
    ("G2", "Gate 2 — Human Review", "Awaiting your approval", "Review mappings below and approve to proceed", CYAN, False),
    ("L4", "Generate BigQuery SQL", "Pending gate approval", "Will generate CREATE OR REPLACE TABLE ×4", TEXT3, False),
]

sy = H - 165
for tag, name, sub1, sub2, col, done in stage_detail:
    alpha = 1.0 if done or tag in ("G2",) else 0.4
    fill = BG2 if done or tag == "G2" else BG2
    border = CYAN if tag == "G2" else BORDER
    card(c, 30, sy-42, W-60, 50, fill, border, 6)
    # dot
    dot(c, 52, sy-17, col, 5)
    if not done and tag != "G2":
        c.setFillColor(BG3)
        c.circle(52, sy-17, 5, fill=1, stroke=0)
        c.setStrokeColor(col)
        c.setLineWidth(1)
        c.circle(52, sy-17, 5, fill=0, stroke=1)
    # tag pill
    pill(c, 62, sy-12, tag, BG3, col, 7)
    # name
    label(c, 98, sy-10, name, 10, TEXT1 if (done or tag=="G2") else TEXT3, "Helvetica-Bold")
    label(c, 62, sy-24, sub1, 8, TEXT2 if done else TEXT3)
    label(c, 62, sy-36, sub2, 8, TEXT3)
    sy -= 58

# Approve button
c.setFillColor(CYAN)
c.roundRect(30, sy+10, W-60, 32, 6, fill=1, stroke=0)
c.setFont("Helvetica-Bold", 11)
c.setFillColor(BG0)
c.drawCentredString(W/2, sy+28, "✓  Approve Gate 2 & Generate SQL")

label(c, 40, 30, "xREF Agent  ·  DataMapper AI v2.0", 8, TEXT3)
label_r(c, W-40, 30, "3 / 5", 8, TEXT3)
c.showPage()

# ══════════════════════════════════════════════════════════════
# PAGE 4 — MAPPING TABLE
# ══════════════════════════════════════════════════════════════
bg(c)
card(c, 0, H-44, W, 44, BG1, BG1, 0)
hline(c, 0, W, H-44, BORDER)
label(c, 36, H-28, "xREF Agent  /", 10, TEXT3)
label(c, 110, H-28, "Mapping Table", 10, TEXT1, "Helvetica-Bold")

label(c, 30, H-70, "Column Mappings", 16, TEXT1, "Helvetica-Bold")
label(c, 30, H-85, "Frontier → 4 Verizon BQ tables  ·  60 source columns", 9, TEXT2)

# Stats strip
stats3 = [("45", "Mapped", GREEN), ("11", "Review", YELLOW), ("4", "Unmapped", RED)]
sx = 30
for val, lbl, col in stats3:
    card(c, sx, H-120, 100, 28, BG2, BORDER, 5)
    c.setFont("Helvetica-Bold", 13)
    c.setFillColor(col)
    c.drawString(sx+8, H-103, val)
    label(c, sx+34, H-103, lbl, 9, TEXT2)
    sx += 112

# Table header
label(c, 30, H-145, "SOURCE COLUMN", 7, TEXT3, "Helvetica-Bold")
label(c, 220, H-145, "TARGET TABLE", 7, TEXT3, "Helvetica-Bold")
label(c, 375, H-145, "TARGET COLUMN", 7, TEXT3, "Helvetica-Bold")
label_r(c, W-30, H-145, "CONF", 7, TEXT3, "Helvetica-Bold")
hline(c, 30, W-30, H-150, BORDER2)

mappings = [
    ("frontier_customer_id",    "verizon_customer_profile", "vz_customer_key",        98, "Direct"),
    ("frontier_account_num",    "verizon_customer_profile", "vz_account_id",           97, "Direct"),
    ("cust_first_name",         "verizon_customer_profile", "first_nm",                95, "Direct"),
    ("cust_last_name",          "verizon_customer_profile", "last_nm",                 95, "Direct"),
    ("customer_email_addr",     "verizon_customer_profile", "email_id",                99, "Direct"),
    ("customer_phone_no",       "verizon_customer_profile", "mobile_num",              96, "Direct"),
    ("customer_segment_type",   "verizon_customer_profile", "cust_segment",            94, "Direct"),
    ("customer_tenure_months",  "verizon_customer_profile", "tenure_mths",             98, "Direct"),
    ("payment_method_type",     "verizon_customer_profile", "payment_mode",            93, "Direct"),
    ("auto_pay_enabled_flag",   "verizon_customer_profile", "autopay_ind",             91, "Direct"),
    ("nps_score",               "verizon_customer_profile", "nps_rating",              93, "Direct"),
    ("churn_risk_score",        "verizon_customer_profile", "risk_of_churn_pct",       72, "Derived"),
    ("support_ticket_count",    "verizon_service_ops",      "open_ticket_count",       91, "Direct"),
    ("last_support_ticket_dt",  "verizon_service_ops",      "recent_ticket_dt",        89, "Direct"),
    ("service_outage_count",    "verizon_service_ops",      "service_outage_events",   87, "Direct"),
    ("last_outage_dt",          "verizon_service_ops",      "last_outage_date",        92, "Direct"),
    ("technician_assigned_nm",  "verizon_service_ops",      "assigned_technician",     98, "Direct"),
    ("installation_dt",         "verizon_service_ops",      "installation_date",       99, "Direct"),
    ("router_model_name",       "verizon_service_ops",      "router_name",             95, "Direct"),
    ("vpn_enabled_flag",        "verizon_service_ops",      "vpn_ind",                 96, "Direct"),
    ("cloud_backup_enabled_flag","verizon_service_ops",     "backup_service_ind",      94, "Direct"),
    ("iot_devices_connected_cnt","verizon_service_ops",     "iot_connected_devices",   97, "Direct"),
    ("device_identifier",       "verizon_network_metrics",  "vz_device_id",            98, "Direct"),
    ("fiber_node_id",           "verizon_network_metrics",  "network_node",            93, "Direct"),
    ("avg_download_speed_mbps", "verizon_network_metrics",  "download_speed",          99, "Direct"),
    ("avg_upload_speed_mbps",   "verizon_network_metrics",  "upload_speed",            99, "Direct"),
    ("network_latency_ms",      "verizon_network_metrics",  "latency_ms",              99, "Direct"),
    ("packet_loss_percent",     "verizon_network_metrics",  "pkt_loss_pct",            97, "Direct"),
    ("network_uptime_pct",      "verizon_network_metrics",  "uptime_percentage",       98, "Direct"),
    ("wifi_connected_devices",  "verizon_network_metrics",  "wifi_device_count",       99, "Direct"),
    ("signal_strength_dbm",     "verizon_network_metrics",  "signal_dbm",              97, "Direct"),
    ("avg_bandwidth_utilization_pct","verizon_network_metrics","bandwidth_util_pct",   88, "Direct"),
    ("security_incident_count", "verizon_network_metrics",  "security_alerts",         90, "Direct"),
    ("firewall_blocked_attempts","verizon_network_metrics", "fw_block_attempts",       95, "Direct"),
    ("frontier_account_num",    "verizon_billing_usage",    "account_reference",       96, "Direct"),
    ("monthly_subscription_amt","verizon_billing_usage",    "monthly_bill_amt",        96, "Direct"),
    ("promotion_applied_cd",    "verizon_billing_usage",    "promo_code",              94, "Direct"),
    ("discount_amount",         "verizon_billing_usage",    "discount_amt_usd",        92, "Direct"),
    ("billing_overdue_amt",     "verizon_billing_usage",    "overdue_balance_amt",     91, "Direct"),
    ("termination_fee_amt",     "verizon_billing_usage",    "termination_charge",      89, "Direct"),
    ("data_consumption_gb",     "verizon_billing_usage",    "consumed_data_gb",        98, "Direct"),
    ("streaming_usage_hours",   "verizon_billing_usage",    "streaming_hrs",           97, "Direct"),
    ("gaming_usage_hours",      "verizon_billing_usage",    "gaming_hrs",              97, "Direct"),
    ("voip_call_minutes",       "verizon_billing_usage",    "voip_minutes_used",       96, "Direct"),
]

table_short = {
    "verizon_customer_profile": "customer_profile",
    "verizon_service_ops":      "service_ops",
    "verizon_network_metrics":  "network_metrics",
    "verizon_billing_usage":    "billing_usage",
}

sy = H - 162
row_h = 14
for src, tbl, tgt, conf, mtype in mappings:
    if sy < 55:
        break
    alt = mappings.index((src, tbl, tgt, conf, mtype)) % 2 == 1
    if alt:
        c.setFillColor(HexColor("#0a0f1a"))
        c.rect(30, sy-row_h+2, W-60, row_h, fill=1, stroke=0)

    c.setFont("Helvetica", 8)
    c.setFillColor(TEXT1)
    c.drawString(32, sy-10, src[:32])

    tbl_short = table_short.get(tbl, tbl)
    tbl_colors = {
        "customer_profile": (HexColor("#1a0a2e"), PURPLE),
        "service_ops":      (HexColor("#001520"), CYAN),
        "network_metrics":  (HexColor("#001a0f"), GREEN),
        "billing_usage":    (HexColor("#1a0800"), ORANGE),
    }
    tbg, tcol = tbl_colors.get(tbl_short, (BG3, TEXT2))
    pill(c, 220, sy-11, tbl_short, tbg, tcol, 7)

    c.setFont("Helvetica", 8)
    c.setFillColor(CYAN)
    c.drawString(375, sy-10, tgt[:28])

    cc = conf_color(conf)
    label_r(c, W-30, sy-10, f"{conf}%", 8, cc, "Helvetica-Bold")

    hline(c, 30, W-30, sy-row_h+2, BORDER, 0.3)
    sy -= row_h

label(c, 40, 30, "xREF Agent  ·  DataMapper AI v2.0", 8, TEXT3)
label_r(c, W-40, 30, "4 / 5", 8, TEXT3)
c.showPage()

# ══════════════════════════════════════════════════════════════
# PAGE 5 — EXPORT & SQL PREVIEW
# ══════════════════════════════════════════════════════════════
bg(c)
card(c, 0, H-44, W, 44, BG1, BG1, 0)
hline(c, 0, W, H-44, BORDER)
label(c, 36, H-28, "xREF Agent  /", 10, TEXT3)
label(c, 110, H-28, "Export", 10, TEXT1, "Helvetica-Bold")

label(c, 30, H-70, "Export Artifacts", 16, TEXT1, "Helvetica-Bold")
label(c, 30, H-85, "All outputs ready after Gate 2 approval", 9, TEXT2)

exports = [
    ("mapping_export.csv", "All column mappings with confidence scores & rationale", CYAN,    "ti-file-spreadsheet"),
    ("table_mapping_summary.csv", "Per table-pair: coverage %, mapped/review/unmapped counts", GREEN,  "ti-table"),
    ("generated_mapping.sql", "BigQuery CREATE OR REPLACE TABLE × 4 target tables",  PURPLE,  "ti-database"),
    ("mapping_workbook.xlsx",  "4-sheet Excel workbook for stakeholder sign-off",      ORANGE,  "ti-file-excel"),
]

sy = H - 105
for fname, desc, col, _ in exports:
    card(c, 30, sy-42, W-60, 50, BG2, BORDER, 7)
    c.setFillColor(col)
    c.roundRect(42, sy-36, 30, 30, 5, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(BG0)
    c.drawCentredString(57, sy-17, "↓")
    label(c, 82, sy-18, fname, 10, TEXT1, "Helvetica-Bold")
    label(c, 82, sy-30, desc, 8, TEXT2)
    c.setStrokeColor(col)
    c.setLineWidth(0.5)
    c.line(30, sy-42, 30, sy+8)
    sy -= 58

# SQL preview box
label(c, 30, sy+2, "GENERATED SQL PREVIEW", 7, TEXT3, "Helvetica-Bold")
sy -= 8
card(c, 30, sy-110, W-60, 115, HexColor("#060912"), HexColor("#001a2e"), 6)
sql_lines = [
    "-- Auto-generated by xREF Agent · DataMapper AI v2.0",
    "-- Session: Frontier → Verizon BQ  ·  45 mappings",
    "",
    "CREATE OR REPLACE TABLE `gcpproject-438715.sqlgen_mockup",
    "    .verizon_customer_profile` AS",
    "SELECT",
    "  frontier_customer_id   AS vz_customer_key,",
    "  frontier_account_num   AS vz_account_id,",
    "  cust_first_name        AS first_nm,",
    "  cust_last_name         AS last_nm,",
    "  ...",
    "FROM `gcpproject-438715.sqlgen_mockup.frontier_data`;",
]
c.setFont("Courier", 7.5)
ly = sy - 14
for line in sql_lines:
    if line.startswith("--"):
        c.setFillColor(TEXT3)
    elif line.startswith("CREATE") or line.startswith("SELECT") or line.startswith("FROM"):
        c.setFillColor(CYAN)
    elif "AS " in line:
        c.setFillColor(TEXT1)
    else:
        c.setFillColor(TEXT2)
    c.drawString(40, ly, line)
    ly -= 9

label(c, 40, 30, "xREF Agent  ·  DataMapper AI v2.0  ·  gcpproject-438715.sqlgen_mockup", 8, TEXT3)
label_r(c, W-40, 30, "5 / 5", 8, TEXT3)
c.showPage()

c.save()
print(f"PDF saved: {OUT}")
