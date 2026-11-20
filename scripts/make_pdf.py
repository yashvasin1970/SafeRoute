# ruff: noqa: E501  (long prose strings in a document generator)
"""Build docs/SafeRoute_Detailed_Explanation.pdf with ReportLab."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from PIL import Image as PILImage
from reportlab.graphics.shapes import Circle, Drawing, Line, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    Image,
    KeepTogether,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

PROJECT = Path(sys.argv[1])
SHOTS = Path(sys.argv[2])
WORK = Path(sys.argv[3])
OUT = PROJECT / "docs" / "SafeRoute_Detailed_Explanation.pdf"

# ---- fonts and colours -------------------------------------------------------------------------
FONTS = Path("C:/Windows/Fonts")
pdfmetrics.registerFont(TTFont("UI", str(FONTS / "segoeui.ttf")))
pdfmetrics.registerFont(TTFont("UI-Bold", str(FONTS / "segoeuib.ttf")))
pdfmetrics.registerFont(TTFont("UI-Italic", str(FONTS / "segoeuii.ttf")))
pdfmetrics.registerFont(TTFont("UI-Light", str(FONTS / "segoeuisl.ttf")))
pdfmetrics.registerFont(TTFont("Mono", str(FONTS / "consola.ttf")))
pdfmetrics.registerFontFamily("UI", normal="UI", bold="UI-Bold", italic="UI-Italic", boldItalic="UI-Bold")

BLUE = colors.HexColor("#1d4ed8")
BLUE_SOFT = colors.HexColor("#e8eefc")
INK = colors.HexColor("#0f172a")
INK2 = colors.HexColor("#334155")
MUTED = colors.HexColor("#5b6678")
LINE = colors.HexColor("#d9e0ea")
SOFT = colors.HexColor("#f4f6f9")
RED = colors.HexColor("#dc2626")
AMBER = colors.HexColor("#d97706")
GREEN = colors.HexColor("#15803d")
ORANGE = colors.HexColor("#ea580c")

PAGE_W, PAGE_H = A4
MARGIN = 20 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

# ---- styles ------------------------------------------------------------------------------------
BODY = ParagraphStyle("body", fontName="UI", fontSize=10.2, leading=15, textColor=INK2, spaceAfter=7)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=8.8, leading=12, textColor=MUTED, spaceAfter=4)
CAPTION = ParagraphStyle("caption", parent=SMALL, alignment=TA_CENTER, spaceBefore=4, spaceAfter=12)
H1 = ParagraphStyle("h1", fontName="UI-Bold", fontSize=19, leading=24, textColor=INK, spaceBefore=4, spaceAfter=10)
H2 = ParagraphStyle(
    "h2", fontName="UI-Bold", fontSize=12.5, leading=17, textColor=BLUE, spaceBefore=10, spaceAfter=5, keepWithNext=1
)
CELL = ParagraphStyle("cell", parent=BODY, fontSize=9, leading=12.2, spaceAfter=0)
CELL_B = ParagraphStyle("cellb", parent=CELL, fontName="UI-Bold", textColor=INK)
CODE = ParagraphStyle(
    "code",
    fontName="Mono",
    fontSize=8.6,
    leading=11.6,
    textColor=INK,
    backColor=SOFT,
    borderColor=LINE,
    borderWidth=0.6,
    borderPadding=7,
    spaceBefore=4,
    spaceAfter=12,
)
CALLOUT = ParagraphStyle("callout", parent=BODY, textColor=INK, spaceAfter=0)


def p(text, style=BODY):
    return Paragraph(text, style)


def h1(num, text):
    return [CondPageBreak(60 * mm), Paragraph(f'<font color="#1d4ed8">{num}</font>&nbsp;&nbsp;{text}', H1)]


def h2(text):
    return Paragraph(text, H2)


def bullets(items, style=BODY):
    return ListFlowable(
        [ListItem(Paragraph(t, style), leftIndent=12, value="•") for t in items],
        bulletType="bullet",
        bulletFontName="UI",
        bulletFontSize=9,
        bulletColor=BLUE,
        leftIndent=12,
        spaceBefore=0,
        spaceAfter=6,
    )


def code(text):
    return Paragraph(
        text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>").replace(" ", "&nbsp;"), CODE
    )


def table(rows, widths, header=True, zebra=True):
    data = [[Paragraph(str(c), CELL_B if (header and r == 0) else CELL) for c in row] for r, row in enumerate(rows)]
    t = Table(data, colWidths=[w * CONTENT_W for w in widths], repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), BLUE_SOFT), ("LINEBELOW", (0, 0), (-1, 0), 0.8, BLUE)]
    if zebra:
        for r in range(1 if header else 0, len(rows)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor("#fafbfd")))
    t.setStyle(TableStyle(style))
    return KeepTogether([t, Spacer(1, 10)]) if len(rows) <= 9 else t


def callout(text, color=BLUE, bg=BLUE_SOFT):
    t = Table([[Paragraph(text, CALLOUT)]], colWidths=[CONTENT_W])
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("LINEBEFORE", (0, 0), (0, -1), 3, color),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return KeepTogether([t, Spacer(1, 10)])


def shot(name, caption, crop_right=None, width=CONTENT_W):
    src = SHOTS / name
    img = PILImage.open(src)
    if crop_right:
        img = img.crop((0, 0, crop_right, img.height))
    out = WORK / f"{src.stem}-crop.png"
    img.save(out)
    h = width * img.height / img.width
    im = Image(str(out), width=width, height=h)
    frame = Table([[im]], colWidths=[width + 2])
    frame.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    return KeepTogether([frame, Paragraph(caption, CAPTION)])


def figure(drawing, caption):
    return KeepTogether([drawing, Paragraph(caption, CAPTION)])


# ---- diagrams ----------------------------------------------------------------------------------
def text(d, x, y, s, size=8.5, font="UI", color=INK, anchor="middle"):
    d.add(String(x, y, s, fontName=font, fontSize=size, fillColor=color, textAnchor=anchor))


def arrow(d, x1, y1, x2, y2, color=MUTED, width=1.2):
    d.add(Line(x1, y1, x2, y2, strokeColor=color, strokeWidth=width))
    import math

    a = math.atan2(y2 - y1, x2 - x1)
    s = 5
    d.add(
        Polygon(
            [
                x2,
                y2,
                x2 - s * math.cos(a - 0.45),
                y2 - s * math.sin(a - 0.45),
                x2 - s * math.cos(a + 0.45),
                y2 - s * math.sin(a + 0.45),
            ],
            fillColor=color,
            strokeColor=color,
            strokeWidth=0.5,
        )
    )


def box(d, x, y, w, h, lines, fill=BLUE, fg=colors.white, size=8.6, bold_first=True, stroke=None):
    d.add(Rect(x, y, w, h, rx=5, ry=5, fillColor=fill, strokeColor=stroke or fill, strokeWidth=0.8))
    total = len(lines) * (size + 2.5)
    cy = y + h / 2 + total / 2 - size + 1
    for i, line in enumerate(lines):
        text(
            d,
            x + w / 2,
            cy - i * (size + 2.5),
            line,
            size=size,
            color=fg,
            font="UI-Bold" if (bold_first and i == 0) else "UI",
        )


def pipeline():
    w = CONTENT_W
    d = Drawing(w, 112)
    steps = [
        ("1  Load roads", "OpenStreetMap", "via OSMnx"),
        ("2  Mark blocked", "responders and", "flood sensors"),
        ("3  Remove closures", "closed edges hidden", "in a graph view"),
        ("4  Shortest path", "Dijkstra to nearest", "open, reachable shelter"),
        ("5  Route + ETA", "Leaflet map, steps,", "each shelter's status"),
    ]
    gap = 10
    bw = (w - gap * 4) / 5
    for i, (a, b, c) in enumerate(steps):
        x = i * (bw + gap)
        fill = colors.HexColor("#1e40af") if i != 1 else BLUE
        d.add(Rect(x, 38, bw, 62, rx=6, ry=6, fillColor=fill, strokeColor=fill))
        num, _, title = a.partition("  ")
        text(d, x + bw / 2, 84, title, size=8.2, font="UI-Bold", color=colors.white)
        text(d, x + bw / 2, 94, f"STEP {num}", size=6.6, font="UI-Bold", color=colors.HexColor("#9db4f5"))
        text(d, x + bw / 2, 66, b, size=7.6, color=colors.HexColor("#dbe4ff"))
        text(d, x + bw / 2, 56, c, size=7.6, color=colors.HexColor("#dbe4ff"))
        if i < 4:
            arrow(d, x + bw + 1, 69, x + bw + gap - 1, 69)
    # live loop
    d.add(Line(bw * 4.5 + gap * 4, 38, bw * 4.5 + gap * 4, 14, strokeColor=ORANGE, strokeWidth=1.2))
    d.add(Line(bw * 4.5 + gap * 4, 14, bw * 1.5 + gap, 14, strokeColor=ORANGE, strokeWidth=1.2))
    arrow(d, bw * 1.5 + gap, 14, bw * 1.5 + gap, 37, color=ORANGE)
    text(
        d,
        w / 2 + 20,
        4,
        "New closure or sensor reading: every open map recomputes within about 3 seconds",
        size=7.8,
        color=ORANGE,
    )
    return d


def architecture():
    w = CONTENT_W
    d = Drawing(w, 250)
    # users column
    users = [("Resident", "find a route"), ("Responder", "close roads, shelters"), ("Planner", "what-if tests")]
    for i, (a, b) in enumerate(users):
        box(d, 0, 186 - i * 52, 104, 40, [a, b], fill=BLUE_SOFT, fg=INK, stroke=colors.HexColor("#bfd0f7"))
    text(d, 52, 236, "Browser / installed app", size=8, font="UI-Bold", color=MUTED)
    text(d, 52, 72, "Leaflet map, polls state", size=7.4, color=MUTED)
    text(d, 52, 63, "every 3 s, works offline", size=7.4, color=MUTED)
    # sensors
    box(
        d,
        0,
        8,
        104,
        40,
        ["Flood sensors", "water level, cm"],
        fill=colors.HexColor("#ecfeff"),
        fg=INK,
        stroke=colors.HexColor("#a5f3fc"),
    )
    # API
    box(
        d,
        150,
        60,
        120,
        156,
        [
            "Flask API",
            "",
            "/api/route",
            "/api/state?since=v",
            "/api/closures",
            "/api/shelters",
            "/api/impact",
            "/api/sensors/.../readings",
        ],
        fill=BLUE,
        size=8,
    )
    text(d, 210, 48, "sign-in, CSRF, rate limit,", size=7.4, color=MUTED)
    text(d, 210, 39, "device keys, validation", size=7.4, color=MUTED)
    for i in range(3):
        arrow(d, 105, 206 - i * 52, 149, 206 - i * 52 if i < 2 else 112)
    arrow(d, 105, 28, 149, 72)
    # engine + store
    box(
        d,
        318,
        140,
        164,
        76,
        [
            "Routing engine (pure Python)",
            "network.py  graph + snapping",
            "routing.py  Dijkstra, coverage",
            "sensors.py  flood rules",
        ],
        fill=colors.HexColor("#1e3a8a"),
        size=7.8,
    )
    box(
        d,
        318,
        60,
        164,
        62,
        ["SQLite store", "closures, shelter status,", "sensor levels, activity log,", "version number"],
        fill=colors.HexColor("#334155"),
        size=7.8,
    )
    arrow(d, 271, 178, 317, 178)
    arrow(d, 271, 92, 317, 92)
    # offline build
    box(
        d,
        318,
        6,
        164,
        40,
        ["Built offline: build_graph.py", "OpenStreetMap -> area JSON"],
        fill=SOFT,
        fg=INK,
        stroke=LINE,
        size=7.8,
    )
    arrow(d, 400, 47, 400, 59, color=MUTED)
    d.add(Line(484, 26, 492, 26, strokeColor=MUTED, strokeWidth=1))
    d.add(Line(492, 26, 492, 178, strokeColor=MUTED, strokeWidth=1))
    arrow(d, 492, 178, 483, 178)
    return d


def town():
    d = Drawing(CONTENT_W, 150)
    ox, oy, s = 110, 30, 120
    pos = {0: (0, 1), 1: (1, 1), 2: (2, 1), 3: (0, 0), 4: (1, 0), 5: (2, 0)}
    xy = {k: (ox + a * s, oy + b * 80) for k, (a, b) in pos.items()}
    roads = [
        (0, 1, "Main Road", BLUE, 3),
        (1, 2, "Main Road", BLUE, 3),
        (0, 3, "West Lane", MUTED, 2),
        (1, 4, "Middle Lane", MUTED, 2),
        (2, 5, "East Lane", MUTED, 2),
        (3, 4, "South Street", MUTED, 2),
        (4, 5, "South Street", MUTED, 2),
    ]
    for u, v, name, c, wdt in roads:
        (x1, y1), (x2, y2) = xy[u], xy[v]
        d.add(Line(x1, y1, x2, y2, strokeColor=c, strokeWidth=wdt))
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if y1 == y2:
            text(d, mx, my + 5, name, size=7.4, color=INK2)
        else:
            text(d, mx - 4, my, name, size=7.4, color=INK2, anchor="end")
    for k, (x, y) in xy.items():
        d.add(Circle(x, y, 9, fillColor=colors.white, strokeColor=INK, strokeWidth=1))
        text(d, x, y - 3, str(k), size=8.5, font="UI-Bold")
    for k, label in ((2, "Shelter A"), (3, "Shelter B")):
        x, y = xy[k]
        d.add(Rect(x + 12 if k == 2 else x - 66, y - 9, 54, 18, rx=4, ry=4, fillColor=GREEN, strokeColor=GREEN))
        text(d, (x + 39) if k == 2 else (x - 39), y - 3, label, size=7.6, font="UI-Bold", color=colors.white)
    text(d, ox + 2 * s + 80, oy + 40, "~110 m between", size=7.4, color=MUTED)
    text(d, ox + 2 * s + 80, oy + 31, "neighbouring junctions", size=7.4, color=MUTED)
    return d


def bands():
    w = CONTENT_W
    d = Drawing(w, 78)
    x0, x1 = 20, w - 20
    scale = (x1 - x0) / 50

    def X(cm):
        return x0 + cm * scale

    for a, b, c, label in ((0, 15, GREEN, "dry"), (15, 30, AMBER, "caution"), (30, 50, RED, "flooded: roads close")):
        d.add(Rect(X(a), 30, X(b) - X(a), 18, fillColor=c, strokeColor=c))
        text(d, (X(a) + X(b)) / 2, 36, label, size=8, font="UI-Bold", color=colors.white)
    for cm in range(0, 51, 5):
        d.add(Line(X(cm), 27, X(cm), 30, strokeColor=MUTED, strokeWidth=0.6))
        text(d, X(cm), 17, f"{cm}", size=7, color=MUTED)
    text(d, X(50) + 2, 17, "cm", size=7, color=MUTED, anchor="start")
    arrow(d, X(15), 66, X(15), 50, color=GREEN)
    text(d, X(15) + 4, 62, "below 15 cm: reopens roads it closed (re-arms)", size=7.6, color=GREEN, anchor="start")
    arrow(d, X(30), 66, X(30), 50, color=RED)
    text(d, X(30) + 4, 62, "30 cm or more: closes every road within 80 m", size=7.6, color=RED, anchor="start")
    text(
        d,
        w / 2,
        3,
        "~15 cm of moving water can knock a person over; ~30 cm can float a car (flood-safety guidance)",
        size=7.4,
        color=MUTED,
    )
    return d


def road_class_chart(rows):
    w = CONTENT_W
    d = Drawing(w, 18 * len(rows) + 10)
    maxv = max(v for _, v, _ in rows)
    for i, (label, km, segs) in enumerate(rows):
        y = 18 * (len(rows) - 1 - i) + 6
        text(d, 90, y + 3, label, size=8.2, color=INK2, anchor="end")
        bw = (w - 220) * km / maxv
        d.add(Rect(96, y, bw, 11, fillColor=BLUE, strokeColor=BLUE))
        text(d, 100 + bw, y + 2.5, f"{km:.1f} km  ({segs} segments)", size=7.8, color=INK2, anchor="start")
    return d


# ---- page templates ----------------------------------------------------------------------------
class Doc(BaseDocTemplate):
    def __init__(self, path):
        super().__init__(
            str(path),
            pagesize=A4,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=22 * mm,
            bottomMargin=20 * mm,
            title="SafeRoute: Detailed Explanation",
            author="Team AstroSync",
            subject="SIH 2026 DM03 Blocked Evacuation Route Finder",
        )
        frame = Frame(
            MARGIN,
            20 * mm,
            CONTENT_W,
            PAGE_H - 42 * mm,
            id="body",
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        cover = Frame(0, 0, PAGE_W, PAGE_H, id="cover", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates(
            [PageTemplate("cover", [cover], onPage=draw_cover), PageTemplate("body", [frame], onPage=draw_chrome)]
        )

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name == "h1":
            txt = flowable.getPlainText()
            self.notify("TOCEntry", (0, txt, self.page))


def draw_chrome(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(MARGIN, PAGE_H - 14 * mm, PAGE_W - MARGIN, PAGE_H - 14 * mm)
    canvas.setFont("UI-Bold", 8.5)
    canvas.setFillColor(BLUE)
    canvas.drawString(MARGIN, PAGE_H - 12 * mm, "SafeRoute")
    canvas.setFont("UI", 8.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN + 44, PAGE_H - 12 * mm, "Detailed explanation  ·  SIH 2026  ·  DM03")
    canvas.drawRightString(PAGE_W - MARGIN, 11 * mm, f"{doc.page}")
    canvas.drawString(MARGIN, 11 * mm, "Team AstroSync")
    canvas.restoreState()


def draw_cover(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(BLUE)
    canvas.rect(0, PAGE_H - 150 * mm, PAGE_W, 150 * mm, fill=1, stroke=0)
    canvas.drawImage(
        str(PROJECT / "saferoute/static/icons/maskable-512.png"),
        MARGIN - 4 * mm,
        PAGE_H - 66 * mm,
        width=38 * mm,
        height=38 * mm,
        mask="auto",
    )
    canvas.setFillColor(colors.white)
    canvas.setFont("UI-Bold", 40)
    canvas.drawString(MARGIN, PAGE_H - 88 * mm, "SafeRoute")
    canvas.setFont("UI-Light", 17)
    canvas.drawString(MARGIN, PAGE_H - 100 * mm, "Real-time evacuation routing around blocked roads")
    canvas.setFont("UI", 11)
    canvas.setFillColor(colors.HexColor("#dbe4ff"))
    canvas.drawString(MARGIN, PAGE_H - 120 * mm, "Detailed explanation of the working model")
    canvas.drawString(
        MARGIN,
        PAGE_H - 127 * mm,
        "Smart India Hackathon 2026  ·  Problem statement DM03, Blocked Evacuation Route Finder",
    )
    canvas.drawString(MARGIN, PAGE_H - 134 * mm, "Theme: Disaster Management  ·  Team AstroSync")

    y = PAGE_H - 175 * mm
    canvas.setFillColor(INK)
    canvas.setFont("UI-Bold", 11)
    canvas.drawString(MARGIN, y, "At a glance")
    facts = [
        ("927 junctions, 1,130 road segments", "real OpenStreetMap roads for Velachery, Chennai"),
        ("Safest routes in ~30 ms", "Dijkstra over travel time, weighted by live risk"),
        ("3 roles + live feeds", "residents, responders, planners; sensors, rain, reports"),
        ("Rescue requests with head count", "SOS pins only responders see, with location and people"),
        ("170 automated tests", "routing, risk, presets, API, security and real map data"),
        ("Android app + installable web app", "APK for Android 6.0+, works on a VIVO Y55S"),
    ]
    y -= 9 * mm
    for i, (a, b) in enumerate(facts):
        col = i % 2
        row = i // 2
        x = MARGIN + col * (CONTENT_W / 2)
        yy = y - row * 15 * mm
        canvas.setFillColor(BLUE)
        canvas.rect(x, yy - 8.5 * mm, 1.2 * mm, 10 * mm, fill=1, stroke=0)
        canvas.setFillColor(INK)
        canvas.setFont("UI-Bold", 10.5)
        canvas.drawString(x + 4 * mm, yy - 1 * mm, a)
        canvas.setFillColor(MUTED)
        canvas.setFont("UI", 8.6)
        canvas.drawString(x + 4 * mm, yy - 6 * mm, b)
    canvas.setFont("UI", 8.6)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 24 * mm, "Code: github.com/yashvasin1970/SafeRoute")
    canvas.drawString(
        MARGIN, 19 * mm, f"Prepared {date.today().strftime('%d %B %Y')}. Road data © OpenStreetMap contributors (ODbL)."
    )
    canvas.restoreState()


# ---- content -----------------------------------------------------------------------------------
def build(stats):
    s = []
    s += [NextPageTemplate("body"), PageBreak()]

    s.append(Paragraph("Contents", H1))
    toc = [
        "Executive summary",
        "The problem",
        "The solution at a glance",
        "Using the app: residents",
        "How it works, end to end",
        "System architecture",
        "The road network data",
        "The routing algorithm",
        "Live feeds and risk",
        "Past cyclones and floods",
        "Live updates and rerouting",
        "Responders",
        "Test cases for demos",
        "Planners: what-if testing",
        "SMS service",
        "Mobile: Android app and web app",
        "Security",
        "Testing and quality",
        "API reference",
        "Running, sharing and deploying",
        "Demo script",
        "Feasibility, risks and limits",
        "Impact and roadmap",
        "References",
    ]
    rows = [[f"{i + 1}", t] for i, t in enumerate(toc)]
    t = Table([[Paragraph(a, CELL_B), Paragraph(b, CELL)] for a, b in rows], colWidths=[12 * mm, CONTENT_W - 12 * mm])
    t.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 3.5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                ("TEXTCOLOR", (0, 0), (0, -1), BLUE),
            ]
        )
    )
    s += [t, PageBreak()]

    # 1
    s += h1("1", "Executive summary")
    s.append(
        p(
            "During floods and cyclones, roads close much faster than maps are updated. A person following an ordinary "
            "navigation app can be sent into a flooded underpass, and a shelter that looks close may already be full or "
            "cut off. Problem statement DM03 asks for a <b>Blocked Evacuation Route Finder</b>: a tool that routes "
            "evacuees only along roads that are actually open."
        )
    )
    s.append(
        p(
            "<b>SafeRoute</b> is our working model. It treats the road network as a graph, keeps a live list of blocked "
            "and risky roads and of shelter status, and gives each resident the <b>safest</b> route to the nearest "
            "shelter that is open, has space and can still be reached. When a responder marks a road blocked, or a "
            "water-level sensor detects flooding, every open map recomputes its route within about three seconds and "
            "explains why it changed. If every road is blocked, the app switches to rescue mode: a <b>CALL RESCUE</b> "
            "button appears, and the resident can send their location and the number of people who need help. "
            "Responders see each request as a point on their map."
        )
    )
    s.append(
        callout(
            "<b>What the model demonstrates.</b> Real OpenStreetMap roads for Velachery, Chennai (927 junctions, "
            f"1,130 segments, {stats['total_km']:.0f} km); risk-weighted Dijkstra routing in about 30 ms; live feeds from "
            "flood sensors, a rain forecast and resident reports; past flood records from the 2015 floods and Cyclone "
            "Michaung (2023); a responder console with one-click test cases and rescue pins; a planner tool that "
            "measures how closures change evacuation times; an SMS service; English, Tamil and Hindi; an installable "
            "web app and an Android APK; and 170 automated tests."
        )
    )

    # 2
    s += h1("2", "The problem")
    s.append(
        p(
            "Evacuation in an urban flood is a routing problem whose inputs change by the minute. Four gaps make "
            "it dangerous today:"
        )
    )
    s.append(
        bullets(
            [
                "<b>Maps assume roads are open.</b> Everyday navigation does not know that a subway is under water or a road "
                "has been barricaded.",
                "<b>Closure information travels slowly.</b> Responders learn about blocked roads by radio or phone, but that "
                "knowledge rarely reaches residents in a form they can act on.",
                "<b>Shelters change state.</b> A relief camp can fill up or become surrounded by water.",
                "<b>People who are trapped are hard to find.</b> Calls come in without a precise location or a head count.",
            ]
        )
    )
    s.append(
        p(
            "We built the model on <b>Velachery, Chennai</b>, one of the city's most flood-prone neighbourhoods, badly hit "
            "in the 2015 floods and during Cyclone Michaung in 2023. The same approach works for any area OpenStreetMap "
            "covers; a second area around Sathyabama Institute of Science and Technology has also been built."
        )
    )

    # 3
    s += h1("3", "The solution at a glance")
    s.append(
        table(
            [
                ["Who", "What they do in SafeRoute"],
                [
                    "Residents",
                    "Use their location or pick their street, get the safest route to the nearest open shelter with "
                    "ETA, distance and street-by-street steps, report problems, and say when they are safe. When "
                    "every road is blocked, call for rescue and send their head count.",
                ],
                [
                    "Responders",
                    "Sign in with a name or unit (no passcode). Block and reopen roads, set shelter status and "
                    "headcount, see rescue requests as points on the map, acknowledge or dismiss them, and run "
                    "one-click test cases or the flood story.",
                ],
                [
                    "Planners",
                    "Build a what-if scenario without publishing anything, and compare live and scenario coverage: "
                    "who is cut off, and how long evacuation takes.",
                ],
                [
                    "Live feeds",
                    "Flood sensors close roads at 30 cm of water; a rain forecast and resident reports mark roads "
                    "as risky; past flood records add a small caution on roads that flooded before.",
                ],
            ],
            [0.18, 0.82],
        )
    )
    s.append(h2("Design principles"))
    s.append(
        bullets(
            [
                "<b>Only open roads.</b> A route never uses a blocked road.",
                "<b>Never suggest an unusable shelter</b>, but always show it and say why (full, closed or cut off).",
                "<b>People stay in charge.</b> Only signed-in responders and keyed sensors change the map; a resident report "
                "never closes a road by itself.",
                "<b>Explain every change.</b> When a route changes, the resident sees which road closed or which shelter filled.",
                "<b>Protect people's locations.</b> Rescue requests and notes are shown to responders only.",
            ]
        )
    )

    # 4
    s += h1("4", "Using the app: residents")
    s.append(
        p(
            "The <b>Find a route</b> tab is laid out as numbered cards, so a stressed person can follow it top to bottom:"
        )
    )
    s.append(
        table(
            [
                ["Card", "What it does"],
                [
                    "1  Use my location",
                    "Uses GPS. If the phone is outside the mapped area, the start snaps to the nearest "
                    "mapped road and says so. Tapping the map also sets the start.",
                ],
                ["2  Choose your street", "A list of the area's named streets, for people without GPS."],
                [
                    "Route result",
                    "Shelter, ETA, distance and steps. Safest routes say what risk they avoided and what it cost.",
                ],
                ["3  Shelters", "Every shelter with how full it is and whether it is open, full, closed or cut off."],
                ["4  Report something here", "Flooding, road blocked or need rescue, with an optional note."],
                ["Already safe?", "Tells responders this person is out of danger, so they know who still needs help."],
            ],
            [0.3, 0.7],
        )
    )
    s.append(
        p(
            "An <b>emergency level</b> chip on the map shows Normal, High alert (at least one road blocked) or "
            "<b>CODE RED · HIGH ALERT</b> (five or more). The <b>I'm trapped / need help</b> button appears only when every "
            "road on the map is blocked; it then reads <b>CALL RESCUE</b>. The SOS block asks how many people need help "
            "(1 to 20), sends a rescue request with the location, and its button dials the emergency number."
        )
    )
    s.append(
        shot(
            "resident-route-v2.jpg",
            "Figure 1. Resident view: the route from Sastri Road to the nearest open shelter, D.M.V, an 18-minute "
            "walk, with the emergency level chip and numbered cards.",
        )
    )
    s.append(
        shot(
            "all-blocked-sos.jpg",
            "Figure 2. Every road blocked (a test case). The level is CODE RED, the trapped button reads CALL "
            "RESCUE, and the SOS block asks how many people need help.",
        )
    )

    # 5
    s += h1("5", "How it works, end to end")
    s.append(figure(pipeline(), "Figure 3. The SafeRoute pipeline, with the live update loop."))
    s.append(
        table(
            [
                ["Step", "What happens", "Where in the code"],
                [
                    "1. Load road network",
                    "Roads are downloaded from OpenStreetMap once, simplified with OSMnx and saved as a "
                    "compact JSON file, loaded into a NetworkX graph at startup.",
                    "scripts/build_graph.py, network.py",
                ],
                [
                    "2. Mark blocked roads",
                    "Responders close and reopen roads; flood sensors do the same automatically. Each "
                    "change is stored with a reason, author and time.",
                    "api.py, store.py, sensors.py",
                ],
                [
                    "3. Remove closed edges",
                    "Routing works on a filtered view of the graph that hides closed roads.",
                    "routing.open_roads()",
                ],
                [
                    "4. Shortest path to shelter",
                    "Dijkstra over travel time, multiplied by each road's risk in safest mode, gives "
                    "the time to every shelter; the best open, reachable one with space is chosen.",
                    "routing.find_route(), risk.py",
                ],
                [
                    "5. Show route + ETA",
                    "The map draws the route and lists ETA, distance, steps and every shelter's status.",
                    "static/app.js",
                ],
            ],
            [0.2, 0.52, 0.28],
        )
    )

    # 6
    s += h1("6", "System architecture")
    s.append(figure(architecture(), "Figure 4. Components and how they talk to each other."))
    s.append(
        table(
            [
                ["Module", "Responsibility"],
                [
                    "network.py",
                    "Loads the area file into an immutable graph; snaps points to the nearest junction or road; "
                    "works out which roads each sensor watches and which roads have a past flood record.",
                ],
                [
                    "routing.py",
                    "Closure- and risk-aware routing, shelter load balancing, coverage and what-if comparisons.",
                ],
                [
                    "risk.py",
                    "Risk for every open road from sensors, rain, resident reports and past floods, with reasons.",
                ],
                ["sensors.py, feeds.py", "Water-level thresholds; the Open-Meteo rain forecast."],
                [
                    "reports.py, sms.py, alerts.py",
                    "Resident reports (with head count for rescue), SMS commands, alert texts.",
                ],
                ["story.py", "The scripted flood story and the one-click test cases."],
                [
                    "store.py",
                    "SQLite: closures, shelters, headcounts, reports, sensor levels, subscribers, the activity log "
                    "and a version number bumped with every change.",
                ],
                [
                    "api.py, security.py",
                    "HTTP endpoints, responder sign-in, CSRF, rate limits, device keys, validation, security headers.",
                ],
                ["static/app.js, sw.js", "The Leaflet map client for all roles; the service worker for offline use."],
            ],
            [0.27, 0.73],
        )
    )

    # 7
    s += h1("7", "The road network data")
    s.append(
        p(
            "The Velachery area is about 2.3 km by 2.7 km. It was turned into SafeRoute's area file by "
            "<font name='Mono'>scripts/build_graph.py</font>, which:"
        )
    )
    s.append(
        bullets(
            [
                "keeps only roads a vehicle can use, dropping private and no-entry roads;",
                "simplifies with OSMnx so each edge runs junction to junction, keeping the road's real shape;",
                "treats roads as two-way, because responders control traffic during an evacuation;",
                "keeps the largest connected piece, and one edge per pair of junctions;",
                "picks five candidate shelters from mapped schools, colleges and halls, spread across the area.",
            ]
        )
    )
    s.append(
        figure(
            road_class_chart(stats["classes"]),
            f"Figure 5. Road length by class ({stats['total_km']:.1f} km in total, {stats['named']} named segments).",
        )
    )
    s.append(
        p(
            "<b>Other areas.</b> A 2 km area around Sathyabama Institute of Science and Technology (1,570 roads, five "
            "shelters) was built the same way and can run as a second copy of the app. Larger areas can be downloaded "
            "in tiles with <font name='Mono'>scripts/build_tiled.py</font>; a 30 km build was attempted, but the public "
            "map server did not return every tile, so it is not finished."
        )
    )
    s.append(
        callout(
            "<b>Sample data.</b> Shelters are real mapped buildings picked as examples, not official relief camps, and the "
            "sensor positions are illustrative. A real deployment would load the official shelter list and real sensors.",
            color=AMBER,
            bg=colors.HexColor("#fff7e6"),
        )
    )

    # 8
    s += h1("8", "The routing algorithm")
    s.append(h2("Graph and travel time"))
    s.append(
        p(
            "Each junction is a node and each road segment an edge, with a walking time and a driving time. Routes "
            "minimise <b>time</b>. The resident app routes on foot by default."
        )
    )
    s.append(
        table(
            [
                ["Mode / road class", "Speed used"],
                ["On foot (any road)", "4 km/h"],
                ["Motorway, trunk", "40 km/h"],
                ["Primary / Secondary / Tertiary", "30 / 25 / 22 km/h"],
                ["Unclassified / Residential / Living street", "18 / 15 / 10 km/h"],
            ],
            [0.6, 0.4],
        )
    )
    s.append(h2("Finding the route"))
    s.append(
        bullets(
            [
                "<b>Snap</b> the start to the nearest junction; the walk to it is added to the ETA.",
                "<b>Remove closures</b> with a graph view that hides closed roads, without copying the graph.",
                "<b>Weight by risk.</b> In safest mode each road's time is multiplied by (1 + risk), capped at five times, so "
                "routes avoid risky roads unless the detour is long. ETAs stay real travel times.",
                "<b>One Dijkstra run</b> from the start gives the time to every shelter at once.",
                "<b>Choose</b> the best shelter that is open and reachable. Shelters over 70% full get a growing penalty, "
                "counting people already on their way, so people spread out.",
                "<b>If none is reachable</b>, the app says so plainly and switches to rescue mode.",
            ]
        )
    )
    s.append(p("Dijkstra runs in O((V + E) log V); a full route with every shelter's status takes about <b>30 ms</b>."))
    s.append(
        KeepTogether([h2("Worked example"), figure(town(), "Figure 6. The six-junction test town used by the tests.")])
    )
    s.append(
        table(
            [
                ["Situation (start at junction 0)", "Result"],
                ["No closures", "Shelter B via West Lane, about 110 m."],
                ["West Lane closed", "Shelter A via Main Road, about 220 m."],
                ["West Lane and Main Road 0-1 closed", "No route: junction 0 is cut off; rescue mode."],
                ["Shelter B marked full", "Shelter A, even though B is closer."],
            ],
            [0.42, 0.58],
        )
    )

    # 9
    s += h1("9", "Live feeds and risk")
    s.append(
        table(
            [
                ["Source", "Effect on roads", "Extra cost"],
                ["Water at a sensor, 30 cm or more", "Closes every road within 80 m", "Closed"],
                ["Water at a sensor, 15 to 30 cm", "Every road the sensor watches is risky", "+1.0"],
                [
                    "Resident report of flooding or a blocked road",
                    "Roads within 60 m are risky until verified or dismissed",
                    "+1.5",
                ],
                ["Heavy or extreme rain forecast (Open-Meteo)", "Each sensor's low-lying stretch is risky", "+0.5"],
                ["Past flood record", "Roads that flooded in earlier cyclones or floods", "+0.5"],
            ],
            [0.38, 0.47, 0.15],
        )
    )
    s.append(figure(bands(), "Figure 7. Water-level bands and what a sensor does at each."))
    s.append(
        p(
            "A sensor never overrides a responder: it does not reopen a road a responder closed, and it stays quiet if a "
            "responder reopens one of its roads. Reports never close a road by themselves; a responder verifies them."
        )
    )

    # 10
    s += h1("10", "Past cyclones and floods")
    s.append(
        p(
            "<font name='Mono'>data/history.json</font> records past events with sources, and the roads they affected. "
            'Those roads get a small +0.5 caution in routing (never a closure), and the risk reason reads "flooded in '
            "past cyclones or floods\". The events are also served at <font name='Mono'>GET /api/history</font>."
        )
    )
    s.append(
        table(
            [
                ["Event", "What the sources report"],
                [
                    "Chennai floods, Nov-Dec 2015",
                    "Velachery flooded more than older parts of the city; blocked lake outlets "
                    "to the Pallikaranai marsh sent water back into the streets.",
                ],
                [
                    "Cyclone Michaung, Dec 2023",
                    "Velachery Main Road was heavily flooded; parts of Velachery stayed inundated for days.",
                ],
            ],
            [0.3, 0.7],
        )
    )
    s.append(
        p(
            "Only Velachery Main Road is flagged so far, because the sources name no other specific roads. Cyclones "
            "Vardah, Nivar and Fengal are not included yet."
        )
    )

    # 11
    s += h1("11", "Live updates and rerouting")
    s.append(
        p(
            "Every change increments a version number in the same database transaction. Each open map asks "
            "<font name='Mono'>/api/state?since=&lt;version&gt;</font> every three seconds; if nothing changed the server "
            "answers <b>204 No Content</b>. When something changed, a resident's route is recomputed and a banner explains "
            "why it changed, for example that a road on it was just blocked."
        )
    )

    # 12
    s += h1("12", "Responders")
    s.append(
        bullets(
            [
                "<b>Sign in</b> with a name or unit, for example NDRF Team 4. There is no passcode; a deployment can restrict "
                "sign-in to one name with <font name='Mono'>SAFEROUTE_RESPONDER_ID</font>, and sign-in is rate-limited.",
                "<b>Roads:</b> tap a road to block it (Flooded, Fallen tree, Debris, Fire, Accident, Other) or reopen it.",
                "<b>Shelters:</b> tap a shelter to mark it open, full or closed and set how many people are inside.",
                "<b>Rescue requests:</b> each person who pressed CALL RESCUE appears as a point. Tapping it shows a speech "
                "bubble with the <b>location</b>, the <b>number of people</b> and any note, with Acknowledge and Dismiss. The "
                "list under the map shows only these requests.",
                "<b>At a glance:</b> people marked safe, open reports and rescue requests, people on the way to shelters, and "
                "a CSV download of the activity log.",
                "<b>Flood scenario, flood story and test cases</b> for demos, and Reset demo.",
            ]
        )
    )
    s.append(
        shot(
            "responder-rescue.jpg",
            "Figure 8. Responder view: a rescue request on Velachery Bypass Road for four people, with the "
            "level at CODE RED after the road-closed test case.",
        )
    )

    # 13
    s += h1("13", "Test cases for demos")
    s.append(p("Each test case resets the demo and sets up one known situation through the normal code paths:"))
    s.append(
        table(
            [
                ["Test case", "What it sets up"],
                ["Calm day", "Dry weather, no closures. The baseline."],
                ["Heavy rain", "Heavy rain and rising water at one sensor: risky roads, no closures."],
                ["Road closed", "Water at 34 cm on Velachery Main Road closes every road within 80 m."],
                ["Shelter full", "D.M.V at capacity, so routes go to the next shelter."],
                ["Rescue request", "A resident's SMS asking for rescue."],
                ["Rescue: group of 4", "A rescue request for four people, shown as a point on the responder map."],
                ["All roads blocked", "Every road closed: no safe route, CODE RED, and the CALL RESCUE flow."],
                ["Start at St. Joseph's", "A start outside the map, which snaps to the nearest mapped road."],
            ],
            [0.3, 0.7],
        )
    )

    # 14
    s += h1("14", "Planners: what-if testing")
    s.append(
        p(
            "Planners tap roads and shelters to build a hypothetical scenario on top of the live map. Nothing is "
            "published. A <b>multi-source Dijkstra</b> from every open shelter gives each junction's time to safety, so a "
            "full comparison over 927 junctions takes about 40 ms. <b>Test the flood scenario</b> loads Michaung-style "
            "closures in one click: 40 segments, 22 junctions cut off (2.4%), and the time within which 9 in 10 people "
            "reach a shelter rises from 18 to 19 minutes on foot."
        )
    )
    s.append(
        shot(
            "planner-flood.jpg",
            "Figure 9. Planner view: roads coloured by time to the nearest shelter, with the flood scenario's "
            "impact table.",
        )
    )

    # 15
    s += h1("15", "SMS service")
    s.append(
        p(
            "For phones without data, the same routing engine answers text commands in English, Tamil and Hindi: "
            "<font name='Mono'>ROUTE &lt;street&gt;</font>, <font name='Mono'>SHELTERS</font>, <font name='Mono'>FLOOD</font>, "
            "<font name='Mono'>HELP</font>, <font name='Mono'>SAFE</font>, <font name='Mono'>STATUS</font> and "
            "<font name='Mono'>JOIN</font> for alerts. A real SMS gateway posts messages to "
            "<font name='Mono'>/api/sms/inbound</font>. The SMS panels are hidden in the current web interface, but the "
            "service and its API remain."
        )
    )

    # 16
    s += h1("16", "Mobile: Android app and web app")
    s.append(
        p(
            "<b>Android APK.</b> A small Android app (<font name='Mono'>android/</font>) opens SafeRoute full screen. It "
            "runs on Android 6.0 and newer, so it works on a VIVO Y55S (Android 6.0.1). It asks for location, opens the "
            "dialler for call buttons, and if it cannot reach the server it asks for the current link and remembers it, "
            "so a new public link does not need a new APK. Version 1.1 is about 0.2 MB."
        )
    )
    s.append(
        p(
            "<b>Installable web app.</b> In a browser SafeRoute can be installed with Add to Home screen. It caches the "
            "map and last state, so offline it still shows known closures and works out routes on the device."
        )
    )
    s.append(
        shot(
            "phone-view.jpg",
            "Figure 10. Phone-width view during the heavy-rain test case: risky roads in "
            "orange and the route bending around them.",
            width=70 * mm,
        )
    )

    # 17
    s += h1("17", "Security")
    s.append(
        bullets(
            [
                "<b>Only signed-in responders change the map</b>; resident reports never close roads.",
                "<b>CSRF protection</b> on every responder change; HttpOnly, SameSite and (when hosted) Secure cookies.",
                "<b>Rate limits per visitor</b> for sign-in and reports. Behind a tunnel or proxy, "
                "<font name='Mono'>SAFEROUTE_TRUST_PROXY=1</font> makes each visitor count separately.",
                "<b>Sensors and SMS gateways use their own keys.</b>",
                "<b>Strict Content Security Policy</b>, X-Frame-Options DENY, nosniff, no-referrer and HSTS over HTTPS.",
                "<b>Input validation</b> on every endpoint, including the rescue head count (whole number 1 to 20).",
                "<b>Privacy:</b> rescue requests, their head counts and notes are sent to responders only.",
            ]
        )
    )

    # 18
    s += h1("18", "Testing and quality")
    s.append(
        p(
            "170 automated tests run with <font name='Mono'>pytest</font>, and the code passes "
            "<font name='Mono'>ruff</font>."
        )
    )
    s.append(
        table(
            [
                ["Test file", "What it covers"],
                ["test_routing.py", "Closures, shelters skipped, ETAs, steps, coverage, what-if impact"],
                [
                    "test_features.py, test_features_api.py",
                    "Risk, load balancing, reports, weather, SMS, the flood story",
                ],
                ["test_sensors_and_store.py", "Every sensor decision; the store's versioning and activity log"],
                ["test_api.py", "Endpoints, sign-in, CSRF, rate limits, validation, hosted mode"],
                ["test_alerts.py, test_checkin_and_scenario.py", "SMS alerts, check-ins, the flood scenario"],
                ["test_presets.py", "Every test case, including all roads blocked and the group of four"],
                ["test_history.py", "Past flood data, its caution, and the history endpoint"],
                ["test_rescue_people.py", "The rescue head count and its limits"],
                ["test_velachery_data.py", "The real Velachery map: every junction can reach a shelter"],
            ],
            [0.42, 0.58],
        )
    )

    # 19
    s += h1("19", "API reference")
    s.append(
        table(
            [
                ["Method and path", "Who", "Purpose"],
                ["GET /api/area", "Anyone", "Roads, junctions, shelters and sensors"],
                [
                    "GET /api/state?since=v",
                    "Anyone",
                    "Closures, shelters, sensors, risk, reports, weather; 204 if unchanged",
                ],
                ["POST /api/route", "Anyone", "{lat, lon, mode, prefer}: route, ETA, steps, every shelter's status"],
                ["POST /api/reports", "Anyone", "{lat, lon, kind, note, people}: flooding, blocked or rescue"],
                ["POST /api/arrivals, /api/checkin, /api/safe", "Anyone", "On the way, arrived, safe"],
                ["POST /api/impact", "Anyone", "What-if coverage (publishes nothing)"],
                ["GET /api/history", "Anyone", "Past cyclones and floods"],
                ["POST /api/responder/login, /logout", "Responders", "{name}"],
                ["POST /api/closures, DELETE /api/closures/&lt;road&gt;", "Responders", "Block or reopen a road"],
                ["POST /api/shelters/&lt;id&gt;", "Responders", "Status and headcount"],
                ["POST /api/reports/&lt;id&gt;/&lt;verify|dismiss&gt;", "Responders", "Handle a report"],
                ["GET /api/scenario/presets, POST /api/scenario/preset/&lt;id&gt;", "Responders", "Test cases"],
                ["POST /api/story/&lt;action&gt;, /api/reset", "Responders", "Flood story, reset"],
                ["GET /api/stats, /api/events.csv", "Responders", "At a glance, activity log"],
                ["POST /api/sensors/&lt;id&gt;/readings", "Devices", "{level_cm} with X-Device-Key"],
                ["POST /api/sms/inbound", "SMS gateway", "Incoming text, with X-Gateway-Key"],
            ],
            [0.45, 0.15, 0.4],
        )
    )

    # 20
    s += h1("20", "Running, sharing and deploying")
    s.append(h2("On a laptop"))
    s.append(code("python -m pip install -r requirements.txt\npython -m saferoute          # http://127.0.0.1:5050"))
    s.append(h2("Sharing a public link"))
    s.append(code("cloudflared tunnel --protocol http2 --url http://127.0.0.1:5050"))
    s.append(
        p(
            "A Cloudflare quick tunnel gives a temporary https link. The link changes each time the tunnel "
            "restarts; HTTP/2 is steadier than QUIC on a phone hotspot. Cloudflare error 1033 means the tunnel is "
            "not connected."
        )
    )
    s.append(
        table(
            [
                ["Variable", "Purpose"],
                ["SAFEROUTE_TRUST_PROXY", "Set to 1 behind a tunnel or proxy, so rate limits are per visitor"],
                ["SAFEROUTE_RESPONDER_ID", "Optional: the only name allowed to sign in as a responder"],
                ["SAFEROUTE_AREA_FILE, _SENSORS_FILE, _SHELTERS_FILE", "Run on another area (empty means none)"],
                ["SAFEROUTE_DATABASE", "Database path, so two copies of the app keep separate data"],
                ["SAFEROUTE_SENSOR_KEY, SAFEROUTE_SECRET_KEY", "Device key for sensors; session signing key"],
            ],
            [0.45, 0.55],
        )
    )
    s.append(h2("Another area"))
    s.append(
        code(
            'python scripts/build_graph.py --name "My Area" --lat 12.87 --lon 80.22 --dist 2000 --out data/my_area.json'
        )
    )
    s.append(h2("Building the Android APK"))
    s.append(code("cd android\ngradle assembleDebug     # app/build/outputs/apk/debug/app-debug.apk"))
    s.append(
        p(
            "Needs Java 17 and the Android SDK (platform 34, build tools 34.0.0); see "
            "<font name='Mono'>android/README.md</font>."
        )
    )

    # 21
    s += h1("21", "Demo script (about 4 minutes)")
    s.append(
        table(
            [
                ["#", "Do this", "Point out"],
                ["1", "Find a route: pick Sastri Road", "Nearest open shelter, ETA, steps, emergency level Normal"],
                [
                    "2",
                    "Responders: sign in, run Road closed",
                    "Roads close at 34 cm; the level turns CODE RED; routes go around",
                ],
                ["3", "Run Rescue: group of 4, tap the red point", "Location and number of people in the bubble"],
                ["4", "Run All roads blocked, go to Find a route", "No safe route; CALL RESCUE and the head count"],
                [
                    "5",
                    "Planners: Test the flood scenario",
                    "Junctions cut off and extra minutes; the live map is untouched",
                ],
                ["6", "Open the Android app on a phone", "The same live map in its own app"],
                ["7", "Responders: Calm day", "Back to a clean map"],
            ],
            [0.05, 0.47, 0.48],
        )
    )

    # 22
    s += h1("22", "Feasibility, risks and limits")
    s.append(
        table(
            [
                ["Risk", "How the model handles it"],
                ["Map data out of date", "The area file is rebuilt from OpenStreetMap with one command"],
                [
                    "Inaccurate closures",
                    "Only responders and keyed sensors close roads; reports need verification; all logged",
                ],
                [
                    "Not a certified warning system",
                    "Stated in the app; it supports responders and does not replace official alerts",
                ],
                [
                    "Scaling beyond one area",
                    "Each area runs from its own file; city-scale graphs are still small for Dijkstra",
                ],
            ],
            [0.36, 0.64],
        )
    )
    s.append(h2("Known limits"))
    s.append(
        bullets(
            [
                "Shelters and sensors are sample data, and the sensors are simulated.",
                "The public link is a temporary tunnel from a laptop; it stops when the laptop sleeps or loses its connection.",
                "Live news is not used: the news sites checked do not allow automated collection. A responder can still close "
                "roads from news by hand.",
                "The whole of Tamil Nadu and the 30 km Sathyabama area are not loaded yet; the 2 km Sathyabama area is.",
                "On narrow phones the top bar is cut off at the right edge.",
                "The Tamil and Hindi text should be reviewed by native speakers; some newer labels are English only.",
            ]
        )
    )

    # 23
    s += h1("23", "Impact and roadmap")
    s.append(
        table(
            [
                ["Who benefits", "How"],
                ["Residents", "A clear route that avoids closed and risky roads, and a direct way to call for rescue"],
                [
                    "Response teams",
                    "Rescue requests with location and head count; faster redirection of everyone at once",
                ],
                ["Planners", "Measure in advance how losing a road or shelter changes evacuation time"],
            ],
            [0.22, 0.78],
        )
    )
    s.append(h2("Next steps"))
    s.append(
        bullets(
            [
                "Load the official relief-camp list and real sensor locations in a pilot ward.",
                "A permanent public server instead of a laptop tunnel.",
                "Larger areas (30 km around Sathyabama, then city scale) and one-way rules.",
                "A licensed news or government alert feed for closures.",
                "Integration with state disaster authorities and the emergency response system.",
            ]
        )
    )

    # 24
    s += h1("24", "References")
    s.append(
        bullets(
            [
                "Dijkstra, E. W. (1959). A note on two problems in connexion with graphs. <i>Numerische Mathematik</i>, 1, 269-271.",
                "Boeing, G. (2017). OSMnx: New methods for acquiring, constructing, analyzing, and visualizing complex street "
                "networks. <i>Computers, Environment and Urban Systems</i>, 65, 126-139.",
                "NetworkX, networkx.org; Flask, flask.palletsprojects.com; Leaflet, leafletjs.com.",
                "OpenStreetMap contributors, openstreetmap.org (Open Database License). Esri World Imagery for the satellite view.",
                "Open-Meteo weather forecast API, open-meteo.com.",
                'The News Minute, "Did Chennai learn any lessons from the 2015 floods?" and "Why Chennai\'s Velachery floods '
                'more but recovers faster than Mylapore".',
                'Citizen Matters, "Calm after the storm in Chennai: What Cyclone Michaung left behind" (December 2023).',
                'US National Weather Service, "Turn Around Don\'t Drown" flood-safety guidance.',
            ],
            SMALL,
        )
    )
    return s


def main():
    import collections
    import json

    area = json.loads((PROJECT / "data/velachery.json").read_text(encoding="utf-8"))
    seg = collections.Counter(r["kind"] for r in area["roads"])
    km = collections.defaultdict(float)
    for r in area["roads"]:
        km[r["kind"]] += r["length_m"] / 1000
    label = {
        "residential": "Residential",
        "primary": "Primary",
        "tertiary": "Tertiary",
        "secondary": "Secondary",
        "living_street": "Living street",
    }
    sys.path.insert(0, str(PROJECT))
    from saferoute.network import RoadNetwork

    net = RoadNetwork.load(PROJECT / "data/velachery.json", PROJECT / "data/velachery.sensors.json")
    sizes = [len(s.roads) for s in net.sensors.values()]
    stats = {
        "classes": [(label.get(k, k), km[k], n) for k, n in seg.most_common()],
        "total_km": sum(km.values()),
        "named": sum(1 for r in area["roads"] if r["name"] != "Unnamed road"),
        "sensor_min": min(sizes),
        "sensor_max": max(sizes),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = Doc(OUT)
    doc.build(build(stats))
    print(OUT)


if __name__ == "__main__":
    main()
