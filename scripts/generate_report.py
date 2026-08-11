#!/usr/bin/env python3
"""Generate formal L1 real-scheduling PoC test report (clean prose, plan-aligned)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from collections import Counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch
import numpy as np

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "reports" / "charts"
DATA = ROOT / "reports" / "data"
EV = Path("/tmp/l1-ev/evidence")
DOCX = ROOT / "reports" / "L1_算力调度真实调度测试报告_20260811.docx"
DT01_JSON = DATA / "dt01_rounds.json"

C_NAVY, C_BLUE, C_TEAL, C_GREEN, C_ORANGE, C_RED = "#0B3D5C", "#1F6AA5", "#1A9B8E", "#2E8B57", "#E07A3D", "#C0392B"
C_BG, C_GRID, C_TEXT = "#F7FAFC", "#E2E8F0", "#1A202C"
BLUE, PALE, GRAY = "0B3D5C", "F0F7FB", "64748B"
FONT = "Songti SC"


def setup_font():
    for c in ["/System/Library/Fonts/Supplemental/Songti.ttc", "/System/Library/Fonts/STHeiti Light.ttc"]:
        if Path(c).exists():
            font_manager.fontManager.addfont(c)
            plt.rcParams["font.family"] = font_manager.FontProperties(fname=c).get_name()
            break
    plt.rcParams.update({
        "axes.unicode_minus": False, "figure.facecolor": "white", "axes.facecolor": C_BG,
        "axes.edgecolor": C_GRID, "axes.labelcolor": C_TEXT, "text.color": C_TEXT,
        "xtick.color": C_TEXT, "ytick.color": C_TEXT, "grid.color": C_GRID,
    })


def style_ax(ax):
    ax.grid(axis="y", alpha=0.65)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def make_charts(dt01: dict):
    CHARTS.mkdir(parents=True, exist_ok=True)
    setup_font()

    # Fig1 overview
    fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.0))
    labels = ["主流程与\n硬约束", "接入与\n安全控制", "执行与\n正确性"]
    vals = [2, 4, 5]  # DT01+DT03; TC01-04; TC10/13/14/15/19
    colors = [C_TEAL, C_ORANGE, C_BLUE]
    bars = axes[0].bar(labels, vals, color=colors, width=0.55, zorder=3)
    for b, v in zip(bars, vals):
        axes[0].text(b.get_x() + b.get_width() / 2, v + 0.1, str(v), ha="center", fontweight="bold")
    axes[0].set_ylim(0, 6)
    axes[0].set_ylabel("通过用例数")
    axes[0].set_title("通过用例构成（共 11 项）")
    style_ax(axes[0])
    axes[1].pie([11], colors=[C_TEAL], startangle=90, wedgeprops=dict(width=0.42, edgecolor="white"))
    axes[1].text(0, 0.06, "11/11", ha="center", fontsize=26, fontweight="bold", color=C_NAVY)
    axes[1].text(0, -0.28, "本报告收录项全部通过", ha="center", fontsize=10)
    axes[1].set_title("收录范围判定")
    fig.suptitle("跨境算力调度 PoC · L1 真实调度测试总览", fontsize=14, fontweight="bold", color=C_NAVY)
    fig.tight_layout()
    fig.savefig(CHARTS / "fig01_overview.png", dpi=180, bbox_inches="tight")
    plt.close()

    # Fig2 architecture
    fig, ax = plt.subplots(figsize=(11.2, 3.9))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 4)
    ax.axis("off")
    boxes = [
        (0.35, 1.15, 2.7, 1.7, "新加坡任务端\n控制面与可视化\n:8080", C_BLUE),
        (3.9, 1.15, 2.7, 1.7, "调度与汇聚\n硬约束过滤\n海南优先策略", "#C05621"),
        (7.5, 2.15, 3.8, 1.35, "海南计算节点\n1×RTX 4090 · ResNet-50", C_TEAL),
        (7.5, 0.4, 3.8, 1.35, "重庆计算节点\n4×RTX 4070 · ResNet-50", C_GREEN),
    ]
    for x, y, w, h, text, color in boxes:
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.04,rounding_size=0.15",
                                    facecolor=color, edgecolor="white", lw=2, alpha=0.93))
        ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", color="white",
                fontsize=10, fontweight="bold")
    ax.annotate("", xy=(3.9, 2.0), xytext=(3.05, 2.0), arrowprops=dict(arrowstyle="-|>", color=C_NAVY, lw=2))
    ax.annotate("", xy=(7.5, 2.75), xytext=(6.6, 2.25), arrowprops=dict(arrowstyle="-|>", color=C_NAVY, lw=2))
    ax.annotate("", xy=(7.5, 1.0), xytext=(6.6, 1.7), arrowprops=dict(arrowstyle="-|>", color=C_NAVY, lw=2))
    ax.set_title("图2  L1 真实调度拓扑（新加坡—海南—重庆）", fontsize=13, fontweight="bold", color=C_NAVY)
    fig.tight_layout()
    fig.savefig(CHARTS / "fig02_architecture.png", dpi=180, bbox_inches="tight")
    plt.close()

    # Fig3 DT01 timeline+bars
    fig = plt.figure(figsize=(11.2, 6.5))
    gs = fig.add_gridspec(2, 1, height_ratios=[1.35, 1], hspace=0.32)
    ax = fig.add_subplot(gs[0])
    r1 = dt01["R1"]["shards"]
    t0 = datetime.fromisoformat(r1[0][2])
    cmap = {"海南": C_BLUE, "重庆": C_TEAL}
    for i, (sid, region, st, ed) in enumerate(r1):
        s = (datetime.fromisoformat(st) - t0).total_seconds()
        e = (datetime.fromisoformat(ed) - t0).total_seconds()
        ax.barh(i, max(e - s, 0.8), left=s, height=0.62, color=cmap[region], alpha=0.92, zorder=3)
        ax.text(e + 0.4, i, f"{region} {e - s:.0f}s", va="center", fontsize=8.5)
    ax.set_yticks(range(len(r1)))
    ax.set_yticklabels([x[0] for x in r1])
    ax.invert_yaxis()
    ax.set_xlabel("相对时间（秒，自首片子任务下发起算）")
    ax.set_title("DT01 第 1 轮分片执行时间线（POC-REAL-0004）")
    style_ax(ax)
    ax.legend(handles=[mpatches.Patch(color=c, label=k) for k, c in cmap.items()], loc="lower right", frameon=False)

    ax2 = fig.add_subplot(gs[1])
    rounds = ["第1轮\nPOC-REAL-0004", "第2轮\nPOC-REAL-0005", "第3轮\nPOC-REAL-0006"]
    hn, cq = [], []
    for key in ("R1", "R2", "R3"):
        regs = Counter(s[1] for s in dt01[key]["shards"])
        hn.append(regs["海南"])
        cq.append(regs["重庆"])
    x = np.arange(3)
    w = 0.34
    b1 = ax2.bar(x - w / 2, hn, w, color=C_BLUE, label="海南", zorder=3)
    b2 = ax2.bar(x + w / 2, cq, w, color=C_TEAL, label="重庆", zorder=3)
    for bars in (b1, b2):
        for b in bars:
            ax2.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.08, f"{int(b.get_height())}", ha="center")
    ax2.set_xticks(x)
    ax2.set_xticklabels(rounds)
    ax2.set_ylim(0, 9)
    ax2.set_ylabel("成功分片数")
    ax2.set_title("连续三轮两地参与情况（每轮均为 8/8 成功）")
    ax2.legend(frameon=False)
    style_ax(ax2)
    fig.suptitle("DT01 两地共同执行的分片批量推理", fontsize=14, fontweight="bold", color=C_NAVY)
    fig.tight_layout()
    fig.savefig(CHARTS / "fig03_dt01.png", dpi=180, bbox_inches="tight")
    plt.close()

    # Fig4 DT03
    fig, ax = plt.subplots(figsize=(10.6, 4.1))
    nodes = ["重庆 GPU1", "重庆 GPU2", "重庆 GPU3", "重庆 GPU4", "海南 GPU1"]
    mem = [12, 12, 12, 12, 24]
    cols = [C_RED] * 4 + [C_TEAL]
    bars = ax.bar(nodes, mem, color=cols, width=0.55, zorder=3, alpha=0.92)
    ax.axhline(16, color=C_ORANGE, ls="--", lw=2, label="任务声明：单卡 16GB")
    for b, v, ok in zip(bars, mem, [False] * 4 + [True]):
        ax.text(b.get_x() + b.get_width() / 2, v + 0.45, f"{v}GB\n{'入选' if ok else '排除'}",
                ha="center", fontsize=8.5, color=C_TEAL if ok else C_RED)
    ax.set_ylim(0, 30)
    ax.set_ylabel("单卡显存（GB）")
    ax.set_title("DT03 / TC10：单卡显存硬约束过滤结果")
    ax.legend(frameon=False, loc="upper left")
    style_ax(ax)
    fig.tight_layout()
    fig.savefig(CHARTS / "fig04_dt03.png", dpi=180, bbox_inches="tight")
    plt.close()

    # Fig5 TC01
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.0))
    axes[0].set_xlim(0, 10)
    axes[0].set_ylim(0, 4)
    axes[0].axis("off")
    steps = [(0.25, "完整性校验"), (2.6, "任务登记"), (4.95, "分片调度"), (7.3, "结果回传")]
    for i, (x, t) in enumerate(steps):
        axes[0].add_patch(FancyBboxPatch((x, 1.25), 2.0, 1.4, boxstyle="round,pad=0.03,rounding_size=0.12",
                                         facecolor=C_TEAL if i else C_BLUE, edgecolor="white", lw=2))
        axes[0].text(x + 1.0, 1.95, t, ha="center", va="center", color="white", fontsize=10, fontweight="bold")
        if i < 3:
            axes[0].annotate("", xy=(x + 2.35, 1.95), xytext=(x + 2.15, 1.95),
                             arrowprops=dict(arrowstyle="-|>", color=C_NAVY, lw=1.8))
    axes[0].set_title("TC01 正向接入主路径")
    axes[1].bar(["海南\nS01-S02", "重庆\nS03-S04"], [2, 2], color=[C_BLUE, C_TEAL], width=0.48, zorder=3)
    axes[1].set_ylim(0, 3.2)
    axes[1].set_ylabel("成功分片数")
    axes[1].set_title("TC01 分片落点（4/4）")
    for i, v in enumerate([2, 2]):
        axes[1].text(i, v + 0.08, str(v), ha="center", fontweight="bold")
    style_ax(axes[1])
    fig.suptitle("TC01 正常跨境接入与完整性校验", fontsize=13, fontweight="bold", color=C_NAVY)
    fig.tight_layout()
    fig.savefig(CHARTS / "fig05_tc01.png", dpi=180, bbox_inches="tight")
    plt.close()

    # Fig6 security
    fig, ax = plt.subplots(figsize=(10.6, 4.1))
    xs = np.arange(3)
    ax.bar(xs, [1, 1, 1], color=[C_RED, C_ORANGE, C_RED], width=0.48, zorder=3, alpha=0.9)
    ax.set_xticks(xs)
    ax.set_xticklabels(["TC02\n错误凭据访问", "TC03\n非授权端口探针", "TC04\n错误数据哈希"])
    ax.set_ylim(0, 1.45)
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["放行", "拒绝"])
    notes = ["海南/重庆均返回 401\n未创建 GPU 任务", "端口 59999 连接拒绝\n写入安全审计", "HASH_MISMATCH\n拒绝入库执行"]
    for i, note in enumerate(notes):
        ax.text(i, 1.08, note, ha="center", fontsize=8.5)
    ax.set_title("接入安全负向用例：拒绝即通过")
    style_ax(ax)
    fig.tight_layout()
    fig.savefig(CHARTS / "fig06_security.png", dpi=180, bbox_inches="tight")
    plt.close()

    # Fig7 correctness
    fig, axes = plt.subplots(1, 2, figsize=(10.8, 4.0))
    axes[0].bar(["比对样本数", "不一致数"], [4096, 0], color=[C_TEAL, C_RED], width=0.45, zorder=3)
    axes[0].set_ylim(0, 4600)
    axes[0].text(0, 4200, "4096", ha="center", fontweight="bold", color=C_TEAL)
    axes[0].text(1, 160, "0", ha="center", fontweight="bold", color=C_RED)
    axes[0].set_title("与冻结参考输出比对")
    style_ax(axes[0])
    c = Counter()
    pred = EV / "POC-REAL-0001" / "merged_predictions.jsonl"
    if pred.exists():
        for line in open(pred):
            c[json.loads(line)["top1_class"]] += 1
    top = c.most_common(6) if c else [(0, 0)]
    axes[1].bar([str(k) for k, _ in top], [v for _, v in top], color=C_BLUE, alpha=0.9, zorder=3)
    axes[1].set_xlabel("top1_class")
    axes[1].set_ylabel("样本数")
    axes[1].set_title("汇聚结果类别分布（节选）")
    style_ax(axes[1])
    fig.suptitle("TC19 / DT01 结果正确性核验", fontsize=13, fontweight="bold", color=C_NAVY)
    fig.tight_layout()
    fig.savefig(CHARTS / "fig07_correctness.png", dpi=180, bbox_inches="tight")
    plt.close()


# ---------------- docx helpers ----------------

def shade(cell, fill):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_text(cell, text, bold=False, color="000000", size=9):
    cell.text = ""
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.space_before = Pt(0)
    run = p.add_run(str(text))
    run.bold = bold
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(color)
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def set_margins(cell, t=70, s=70, b=70, e=70):
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for m, v in (("top", t), ("start", s), ("bottom", b), ("end", e)):
        node = tc_mar.find(qn(f"w:{m}"))
        if node is None:
            node = OxmlElement(f"w:{m}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(v))
        node.set(qn("w:type"), "dxa")


def borders(table, color="D0E3F0", size="6"):
    tbl_pr = table._tbl.tblPr
    b = tbl_pr.first_child_found_in("w:tblBorders")
    if b is None:
        b = OxmlElement("w:tblBorders")
        tbl_pr.append(b)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = b.find(qn(f"w:{edge}"))
        if el is None:
            el = OxmlElement(f"w:{edge}")
            b.append(el)
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), size)
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)


def add_table(doc, headers, rows, widths=None, font_size=8.5):
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    borders(table)
    hdr = table.rows[0]
    tr_pr = hdr._tr.get_or_add_trPr()
    th = OxmlElement("w:tblHeader")
    th.set(qn("w:val"), "true")
    tr_pr.append(th)
    for i, h in enumerate(headers):
        set_cell_text(hdr.cells[i], h, bold=True, color="FFFFFF", size=font_size)
        shade(hdr.cells[i], BLUE)
        set_margins(hdr.cells[i])
        if widths:
            hdr.cells[i].width = Cm(widths[i])
    for ridx, row in enumerate(rows):
        cells = table.add_row().cells
        for i, v in enumerate(row):
            set_cell_text(cells[i], v, size=font_size)
            if ridx % 2 == 0:
                shade(cells[i], PALE)
            set_margins(cells[i])
            if widths:
                cells[i].width = Cm(widths[i])
    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def add_heading(doc, text, level=1):
    p = doc.add_paragraph()
    p.style = f"Heading {level}"
    p.paragraph_format.space_before = Pt(14 if level == 1 else 9)
    p.paragraph_format.space_after = Pt(5)
    run = p.add_run(text)
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.color.rgb = RGBColor.from_string(BLUE if level == 1 else "1F6AA5")


def add_body(doc, text, first_line_indent=True):
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing = 1.35
    p.paragraph_format.space_after = Pt(6)
    if first_line_indent:
        p.paragraph_format.first_line_indent = Cm(0.74)
    run = p.add_run(text)
    run.font.name = FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    run.font.size = Pt(10.5)


def add_label_block(doc, label, text):
    p = doc.add_paragraph()
    p.paragraph_format.line_spacing = 1.3
    p.paragraph_format.space_after = Pt(4)
    r1 = p.add_run(label)
    r1.bold = True
    r1.font.color.rgb = RGBColor.from_string(BLUE)
    r1.font.name = FONT
    r1._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    r2 = p.add_run(text)
    r2.font.name = FONT
    r2._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    r2.font.size = Pt(10.5)


def add_callout(doc, title, body, fill="E8F5F2"):
    t = doc.add_table(rows=1, cols=1)
    borders(t, "9ED5CB", "10")
    cell = t.cell(0, 0)
    shade(cell, fill)
    set_margins(cell, 140, 160, 140, 160)
    p = cell.paragraphs[0]
    r = p.add_run(title)
    r.bold = True
    r.font.color.rgb = RGBColor.from_string(BLUE)
    r.font.name = FONT
    r._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    p2 = cell.add_paragraph(body)
    p2.paragraph_format.line_spacing = 1.25
    for run in p2.runs:
        run.font.name = FONT
        run._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        run.font.size = Pt(10.5)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def add_figure(doc, path, caption, width_cm=15.8):
    if not Path(path).exists():
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    inline = p.add_run().add_picture(str(path), width=Cm(width_cm))
    inline._inline.docPr.set("descr", caption)
    cp = doc.add_paragraph()
    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cp.paragraph_format.space_after = Pt(8)
    r = cp.add_run(caption)
    r.italic = True
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor.from_string(GRAY)


def add_page_number(paragraph):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("第 ")
    a = OxmlElement("w:fldChar")
    a.set(qn("w:fldCharType"), "begin")
    b = OxmlElement("w:instrText")
    b.set(qn("xml:space"), "preserve")
    b.text = "PAGE"
    c = OxmlElement("w:fldChar")
    c.set(qn("w:fldCharType"), "end")
    run._r.append(a)
    run._r.append(b)
    run._r.append(c)
    paragraph.add_run(" 页")


def evidence_table(doc, rows):
    add_table(doc, ["记录项", "内容"], rows, widths=[4.0, 12.2], font_size=8.5)


def build():
    if not DT01_JSON.exists():
        raise SystemExit(f"missing {DT01_JSON}")
    if not (EV / "case_evidence" / "DT01.json").exists():
        import tarfile
        tar = ROOT / "reports" / "evidence" / "l1-poc-evidence-latest.tgz"
        if not tar.exists():
            tar = ROOT / "l1-poc-evidence-latest.tgz"
        tarfile.open(tar).extractall("/tmp/l1-ev")

    dt01 = json.loads(DT01_JSON.read_text(encoding="utf-8"))
    make_charts(dt01)

    case_files = {}
    for name in ("DT01", "DT03", "TC01", "TC02", "TC03", "TC04"):
        p = EV / "case_evidence" / f"{name}.json"
        if p.exists():
            case_files[name] = json.load(open(p))

    doc = Document()
    sec = doc.sections[0]
    sec.top_margin = Cm(2.0)
    sec.bottom_margin = Cm(1.8)
    sec.left_margin = Cm(2.2)
    sec.right_margin = Cm(2.2)
    styles = doc.styles
    styles["Normal"].font.name = FONT
    styles["Normal"]._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    styles["Normal"].font.size = Pt(10.5)
    for name, size in [("Heading 1", 15), ("Heading 2", 12.5), ("Heading 3", 11)]:
        st = styles[name]
        st.font.name = FONT
        st._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = RGBColor.from_string(BLUE)
    footer = sec.footer.paragraphs[0]
    footer.text = "跨境算力调度 PoC · L1 真实调度测试报告  |  2026-08-11  |  "
    footer.runs[0].font.size = Pt(8)
    footer.runs[0].font.color.rgb = RGBColor.from_string(GRAY)
    add_page_number(sec.footer.add_paragraph())

    # ===== Cover =====
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(72)
    r = p.add_run("跨境算力调度 PoC")
    r.font.size = Pt(16)
    r.bold = True
    r.font.color.rgb = RGBColor.from_string(BLUE)

    p2 = doc.add_paragraph()
    p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p2.add_run("L1 算力调度真实调度测试报告")
    r.font.size = Pt(24)
    r.bold = True
    r.font.color.rgb = RGBColor.from_string("0B3D5C")

    p3 = doc.add_paragraph()
    p3.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p3.add_run("依据《跨境算力调度 PoC 测试实施方案 v0724》编制")
    r.font.size = Pt(11)
    r.font.color.rgb = RGBColor.from_string(GRAY)

    p4 = doc.add_paragraph()
    p4.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p4.add_run("新加坡任务端 · 海南统一调度与本地计算 · 重庆异地计算")
    r.font.size = Pt(10.5)
    r.font.color.rgb = RGBColor.from_string(GRAY)

    doc.add_paragraph().paragraph_format.space_after = Pt(18)
    add_table(doc, ["项目", "内容"], [
        ["报告编号", "L1-SCHED-REAL-20260811"],
        ["测试依据", "《跨境算力调度 PoC 测试实施方案 v0724》"],
        ["执行日期", "2026年8月11日"],
        ["测试性质", "L1 多地轻量验证；真实节点调度与真实 GPU 推理"],
        ["测试范围", "新加坡控制面、海南接入/调度/计算、重庆异地计算"],
        ["主测试任务", "冻结 ResNet-50 批量推理；4096 样本 / 8 分片"],
        ["报告收录", "本轮已完成并判定通过的 11 项用例（未完成项不列入）"],
    ], widths=[3.3, 12.7], font_size=9)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("内部测试材料 · 凭据与受控网络信息不写入本报告")
    r.font.size = Pt(9)
    r.font.color.rgb = RGBColor.from_string(GRAY)

    # ===== 1 摘要 =====
    add_heading(doc, "1. 执行摘要", 1)
    add_body(doc,
             "本次测试按照实施方案确定的 L1 多地轻量验证口径组织，重点验证“跨境任务接入—异地资源感知—"
             "调度决策—分片推理执行—结果汇聚与正确性核验—安全拒绝与审计留痕”的完整技术链路。"
             "测试不将海南与重庆 GPU 拼接为统一显存池，不开展跨站点模型并行或梯度同步；两地共同计算采用"
             "父任务拆分、两地独立执行、控制面汇聚的任务级并行方式。")
    add_body(doc,
             "主测试任务采用实施方案推荐的标准测试任务包形态：固定权重 ResNet-50、约 140MB 合成图像数据包、"
             "4096 条样本划分为 8 个可独立执行分片，并冻结参考输出用于正确性比对。控制面部署于新加坡，"
             "计算节点分别位于海南与重庆，由节点代理上报真实 GPU 状态并执行推理任务。")
    add_callout(doc, "本次测试结论",
                "本报告收录的 11 项用例全部通过。核心端到端流程（DT01）连续三轮成功，每轮 8 个分片全部闭环，"
                "海南与重庆均实际参与执行，汇总结果 4096 条样本无缺失、无重复，并与冻结参考输出完全一致。"
                "单卡 16GB 显存硬约束、正向接入校验以及身份认证失败、非授权访问、数据完整性异常等安全负向场景，"
                "均按方案预期完成验证。本报告仅对已完成并通过的用例给出结论，不扩大解释为正式 SLA 或生产验收通过。")
    add_figure(doc, CHARTS / "fig01_overview.png", "图1  通过用例构成与收录范围")

    add_table(doc, ["类别", "用例", "结论要点"], [
        ["主流程", "DT01", "连续 3 轮 × 8 分片全部成功；两地均参与；参考比对通过"],
        ["硬约束", "DT03、TC10", "16GB 任务仅调度至海南 4090，重庆 12GB 单卡被排除"],
        ["接入校验", "TC01", "完整性校验通过后完成登记与分片执行"],
        ["安全控制", "TC02、TC03、TC04", "错误凭据/非授权端口/错误哈希均被拒绝并留审计"],
        ["执行与正确性", "TC13、TC14、TC15、TC19", "海南/重庆推理、批量队列与结果正确性由 DT01 覆盖验证"],
    ], widths=[2.8, 4.2, 9.2], font_size=8.5)

    # ===== 2 依据与范围 =====
    add_heading(doc, "2. 测试依据、范围与环境", 1)
    add_heading(doc, "2.1 测试依据与边界", 2)
    add_body(doc,
             "测试依据为《跨境算力调度 PoC 测试实施方案 v0724》。方案明确：本期仅做 L1 验证，"
             "不接入香港或其他海外 GPU 资源池，不开展生产级高并发、双路由容灾、自动跨节点迁移和正式 SLA 验收。"
             "验收材料应具备可归档、可复测、可审计特征；每个用例应保留父/子任务标识、输入哈希、候选集与选择原因、"
             "执行节点与 GPU、输出完整性、审计与结论等记录字段。")
    add_body(doc,
             "本报告遵循上述口径：只描述本轮实际完成的验证内容；对未具备正式 VPN/OTN 专线测量条件、"
             "未执行 24 小时墙钟稳定性、以及未开展的轻量训练类用例，不写入通过结论，也不作为本报告正文展开对象。")

    add_heading(doc, "2.2 测试环境", 2)
    add_body(doc,
             "测试拓扑对应方案中的三区域角色划分：新加坡侧负责任务发起、控制面展示与结果接收；"
             "海南侧承担统一接入、调度决策、本地计算与结果汇聚；重庆侧作为异地计算节点。"
             "现场实测海南计算节点当前可用 GPU 为 1×RTX 4090（24GB），重庆为 4×RTX 4070（单卡 12GB）。"
             "节点通过 HTTP 代理接口上报 nvidia-smi 资源状态，并执行冻结后的 ResNet-50 推理脚本。")
    add_table(doc, ["区域", "角色", "关键配置", "状态"], [
        ["新加坡", "任务端 / 控制面", "调度服务与可视化界面，监听 8080", "已部署并参与测试"],
        ["海南", "接入、调度、本地计算", "node agent；1×RTX 4090；CUDA/PyTorch", "已部署并参与测试"],
        ["重庆", "异地计算", "node agent；4×RTX 4070；CUDA/PyTorch", "已部署并参与测试"],
        ["主任务包", "冻结业务输入", "4096 样本、8 分片、resnet50_frozen.pth、参考输出", "三地对齐"],
    ], widths=[2.4, 3.4, 6.2, 4.0], font_size=8.5)
    add_figure(doc, CHARTS / "fig02_architecture.png", "图2  本次真实调度拓扑")

    add_heading(doc, "2.3 标准测试任务包与执行方法", 2)
    add_body(doc,
             "为避免测试停留在“能提交、能返回”的接口层面，本轮采用方案推荐的可重复、可分片、可校验 GPU 批量推理任务。"
             "父任务作为统一管理单元，依据分片清单拆分为 8 个子任务；每个子任务具有独立任务标识与输出文件。"
             "调度系统在硬约束（健康状态、链路可用、单卡显存等）满足后，按海南优先策略选择执行节点；"
             "执行完成后，控制面按父任务汇聚 predictions，并与冻结参考输出比对 top1 类别。")
    add_table(doc, ["项目", "冻结/执行内容"], [
        ["模型", "ResNet-50 固定权重；关闭在线下载"],
        ["数据", "合成非医疗图像；4096 张；划分为 8 个分片，每片 512 条"],
        ["输出", "分片 predictions_*.jsonl；父任务 merged_predictions.jsonl 与 summary"],
        ["正确性", "与 reference/predictions_all.jsonl 进行全量 top1 比对"],
        ["证据", "case_evidence/*.json、audit_log.jsonl、父任务输出目录"],
    ], widths=[3.0, 13.2], font_size=8.5)

    # ===== 3 结果总表 =====
    add_heading(doc, "3. 测试结果总表", 1)
    add_body(doc,
             "下表汇总本报告收录的全部通过用例。用例编号、场景名称与级别与实施方案第 6 章保持一致；"
             "其中 TC10、TC13、TC14、TC15、TC19 按方案允许的映射关系，由核心场景 DT01/DT03 的现场证据覆盖。")
    add_table(doc, ["编号", "级别", "场景", "判定", "关键证据"], [
        ["DT01", "A", "两地共同执行的分片批量推理", "通过", "POC-REAL-0004/0005/0006；参考比对通过"],
        ["DT03", "A", "单卡 16GB 显存硬约束", "通过", "POC-REAL-0002；仅海南执行"],
        ["TC01", "A", "正常跨境接入", "通过", "POC-REAL-0003；完整性校验通过"],
        ["TC02", "A", "身份认证失败", "通过（预期拒绝）", "双节点 HTTP 401；AUTH_FAIL 审计"],
        ["TC03", "A", "非授权端口或路径", "通过（预期拒绝）", "端口探针阻断；PROBE_BLOCKED"],
        ["TC04", "A", "数据完整性异常", "通过（预期拒绝）", "HASH_MISMATCH；未创建执行任务"],
        ["TC10", "A", "单卡显存硬约束", "通过", "由 DT03 覆盖"],
        ["TC13", "A", "海南本地推理", "通过", "由 DT01 海南分片覆盖"],
        ["TC14", "A", "重庆异地推理", "通过", "由 DT01 重庆分片覆盖"],
        ["TC15", "A", "批量推理与队列", "通过", "由 DT01 三轮 8 分片覆盖"],
        ["TC19", "A", "结果正确性", "通过", "4096/4096 top1 一致"],
    ], widths=[1.5, 1.2, 4.5, 2.8, 6.0], font_size=8)

    # ===== 4 详细用例 =====
    add_heading(doc, "4. 重点用例详细记录", 1)
    add_body(doc,
             "本章按实施方案“重点用例详细执行流程”与“单项测试用例记录要求”组织，"
             "对每项用例说明测试目的、结果与分析，并附必要表格、可视化与证据字段。")

    # DT01
    add_heading(doc, "4.1 DT01 两地共同执行的分片批量推理", 2)
    add_label_block(doc, "测试目的：",
                    "验证新加坡侧发起任务后，控制面完成登记与拆分，海南与重庆同时参与分片执行，"
                    "并在汇聚端形成完整、可校验的父任务结果。该用例对应方案核心端到端主流程，"
                    "要求连续运行 3 轮，且每轮均满足两地参与、分片闭环与样本完整性。")
    add_label_block(doc, "前置条件与操作要点：",
                    "两地节点在线且 GPU 可用；主测试数据包、权重与参考输出已冻结；"
                    "提交父任务并拆分为 8 个子任务，资源声明为单卡、8GB 显存量级；"
                    "调度策略在满足硬约束后采用海南优先。逐轮记录子任务落点、状态与汇聚结果。")
    add_label_block(doc, "测试结果：",
                    "第 1–3 轮父任务 POC-REAL-0004、POC-REAL-0005、POC-REAL-0006 均执行成功，"
                    "每轮 8/8 分片状态闭环。各轮均有海南与重庆实际执行记录（典型分布为海南 6 片、重庆 2 片）。"
                    "每轮汇聚结果包含 4096 条唯一样本，缺失 0、重复 0。"
                    "复核轮次进一步完成与冻结参考输出的全量比对：compared=4096，mismatch=0。判定：通过。")
    add_label_block(doc, "结果分析：",
                    "从分片时间线可以看到，系统先在海南消化前序分片，随后将部分分片调度至重庆，"
                    "再继续回填海南，体现了“硬约束过滤 + 海南优先 + 异地补位”的实际调度行为。"
                    "更重要的是，结果不仅状态成功，而且业务输出与参考集一致，说明验证已深入到"
                    "方案所强调的正确性层面，而非停留在接口连通。海南现场为单卡 4090，因此海南侧"
                    "分片呈串行特征，但这不影响“两地均至少成功执行子任务”的通过标准。")
    add_figure(doc, CHARTS / "fig03_dt01.png", "图3  DT01 分片时间线与三轮两地参与统计")
    add_table(doc, ["轮次", "父任务 ID", "成功分片", "海南", "重庆", "缺失/重复", "结论"], [
        ["1", "POC-REAL-0004", "8/8", "6", "2", "0 / 0", "成功"],
        ["2", "POC-REAL-0005", "8/8", "6", "2", "0 / 0", "成功"],
        ["3", "POC-REAL-0006", "8/8", "6", "2", "0 / 0", "成功"],
    ], widths=[1.4, 3.2, 2.0, 1.6, 1.6, 2.2, 2.0], font_size=8)
    add_table(doc, ["子任务", "执行区域", "开始时间", "结束时间", "耗时（秒）", "状态"], [
        [s, r, a.replace("T", " ")[11:19], b.replace("T", " ")[11:19],
         f"{(datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds():.0f}", "成功"]
        for s, r, a, b in dt01["R1"]["shards"]
    ], widths=[1.8, 2.2, 2.6, 2.6, 2.4, 2.0], font_size=8)
    if "DT01" in case_files:
        d = case_files["DT01"]
        evidence_table(doc, [
            ["父任务 / 子任务", f"{d.get('parent_task_id')} / {d.get('child_task_id')}"],
            ["执行时间", d.get("execution_time", "")],
            ["执行节点 / GPU", f"{d.get('execution_node')} / {d.get('gpu_id')}"],
            ["缺失 / 重复样本", f"{d.get('missing_samples')} / {d.get('duplicate_samples')}"],
            ["结果摘要", json.dumps(d.get("merge_summary", {}).get("reference_match"), ensure_ascii=False)],
            ["测试结论", d.get("test_conclusion", "")],
            ["日志证据", d.get("log_evidence", "")],
        ])

    # DT03 / TC10
    add_heading(doc, "4.2 DT03 / TC10 单卡 16GB 显存硬约束", 2)
    add_label_block(doc, "测试目的：",
                    "验证单卡可用显存属于硬约束。重庆 4070 单卡仅 12GB，不得因“四卡合计 48GB”而被错误选中；"
                    "16GB 资源声明应在候选阶段排除重庆，仅在海南 4090（24GB）可用时执行。")
    add_label_block(doc, "测试结果：",
                    "父任务 POC-REAL-0002 仅向海南-GPU1 下发 1 个分片并成功完成；"
                    "重庆四张 12GB GPU 均未进入执行。TC10 与 DT03 为同一硬约束在原子用例与场景用例上的对应关系，"
                    "由本次现场结果共同支撑。判定：通过。")
    add_label_block(doc, "结果分析：",
                    "该结果直接回应了实施方案反复强调的边界：L1 验证不允许把多卡显存池化理解成一张大显存。"
                    "过滤发生在调度前，避免了“先错误下发、OOM 后再回退”的不合格路径。")
    add_figure(doc, CHARTS / "fig04_dt03.png", "图4  DT03/TC10 候选节点过滤结果")
    add_table(doc, ["候选 GPU", "单卡显存（GB）", "是否满足 ≥16GB", "调度结果"], [
        ["重庆-GPU1～GPU4", "12", "否", "排除"],
        ["海南-GPU1", "24", "是", "选中并执行成功"],
    ], widths=[3.5, 3.5, 4.0, 5.0], font_size=8.5)

    # TC01
    add_heading(doc, "4.3 TC01 正常跨境接入与完整性校验", 2)
    add_label_block(doc, "测试目的：",
                    "验证有效任务与测试数据在进入调度前，完成必要的登记与 SHA-256/清单完整性校验，"
                    "校验通过后可正常创建并执行任务。")
    add_label_block(doc, "测试结果：",
                    "父任务 POC-REAL-0003 在完整性校验通过后成功创建；4 个分片全部成功，"
                    "其中海南执行 S01/S02，重庆执行 S03/S04。判定：通过。")
    add_label_block(doc, "结果分析：",
                    "TC01 验证的是正向主路径。它与 TC04 形成对照：正确哈希放行、错误哈希拒绝。"
                    "从结果看，接入校验并未阻断合法任务，同时后续分片能够真正落到两地 GPU，"
                    "说明“校验—登记—调度—执行”链路是贯通的。")
    add_figure(doc, CHARTS / "fig05_tc01.png", "图5  TC01 正向接入路径与分片落点")
    add_table(doc, ["子任务", "执行区域", "状态", "样本数"], [
        ["S01", "海南", "成功", "512"],
        ["S02", "海南", "成功", "512"],
        ["S03", "重庆", "成功", "512"],
        ["S04", "重庆", "成功", "512"],
    ], widths=[3.0, 3.0, 3.0, 3.0], font_size=8.5)

    # TC02-04
    add_heading(doc, "4.4 TC02 / TC03 / TC04 接入安全与数据完整性负向验证", 2)
    add_body(doc,
             "实施方案将身份认证失败、非授权端口或路径、数据完整性异常均列为 A 类必须验证项。"
             "其通过标准不是“系统仍然跑出结果”，而是“应当拒绝、不得创建可执行任务，并形成可查询审计”。"
             "本轮据此组织了三类受控负向试验。")
    add_label_block(doc, "TC02 身份认证失败：",
                    "使用错误 Bearer 访问海南与重庆节点代理，两端均返回 HTTP 401；"
                    "控制面记录 AUTH_FAIL，并给出 REJECTED 结论，未创建 GPU 执行任务。判定：通过。")
    add_label_block(doc, "TC03 非授权端口或路径：",
                    "对非业务端口 59999 发起探针，连接被拒绝（blocked=true）；"
                    "系统记录 PROBE_BLOCKED，未进入执行阶段。判定：通过。")
    add_label_block(doc, "TC04 数据完整性异常：",
                    "故意提交错误期望哈希，触发 HASH_MISMATCH；系统拒绝创建可执行任务并保留审计。"
                    "判定：通过。")
    add_label_block(doc, "综合分析：",
                    "三类用例分别覆盖“人/账号是否合法”“网络路径是否授权”“数据是否完整可信”。"
                    "测试表明，安全控制点位于任务真正消耗 GPU 之前，符合最小化风险与审计留痕要求。"
                    "需要说明的是：本轮网络环境为可管理的测试连通条件，结论用于证明控制逻辑有效，"
                    "不替代运营商级 VPN/防火墙正式测评。")
    add_figure(doc, CHARTS / "fig06_security.png", "图6  TC02/TC03/TC04 负向拒绝结果")
    add_table(doc, ["用例", "关键操作", "预期（方案）", "实测"], [
        ["TC02", "错误凭据访问", "拒绝访问并记录原因", "双节点 401；未建 GPU 任务"],
        ["TC03", "访问非授权端口", "访问被阻断并产生审计", "连接拒绝；PROBE_BLOCKED"],
        ["TC04", "提交错误哈希", "校验失败，拒绝执行", "HASH_MISMATCH；拒绝建任务"],
    ], widths=[1.6, 3.5, 5.0, 6.0], font_size=8.2)

    # mapped execution cases
    add_heading(doc, "4.5 TC13 / TC14 / TC15 / TC19 执行与正确性（由 DT01 覆盖）", 2)
    add_body(doc,
             "实施方案允许在核心场景已充分取证时，对密切相关的原子用例采用映射覆盖。"
             "TC13（海南本地推理）、TC14（重庆异地推理）、TC15（批量推理与队列）、TC19（结果正确性）"
             "的关键观察点，均已在 DT01 连续三轮真实执行中得到满足。")
    add_label_block(doc, "测试目的：",
                    "分别确认海南侧与重庆侧能够完成同一冻结推理任务，批量分片可连续闭环，"
                    "并且汇聚结果与参考输出一致。")
    add_label_block(doc, "测试结果：",
                    "DT01 各轮均存在海南成功分片与重庆成功分片；三轮共 24 个分片全部成功；"
                    "复核比对显示 4096 条样本 top1 类别与参考集完全一致。上述四项用例判定：通过。")
    add_label_block(doc, "结果分析：",
                    "对 TC13/TC14 而言，关键不在于“某一侧执行得多”，而在于两侧都真正跑通推理与回传；"
                    "对 TC15 而言，关键是连续多轮、多分片无失败；对 TC19 而言，关键是结果可对照、可复算。"
                    "本轮在这四点上均给出了明确的现场证据。")
    add_figure(doc, CHARTS / "fig07_correctness.png", "图7  结果正确性比对与类别分布")
    add_table(doc, ["用例", "方案关注点", "现场对应证据", "判定"], [
        ["TC13", "海南本地推理并产出预测", "DT01 海南分片多次成功", "通过"],
        ["TC14", "重庆异地推理并回传", "DT01 各轮 S03/S04 重庆成功", "通过"],
        ["TC15", "8 分片批量、连续 3 轮、两地参与", "3×8 全成功；两地均参与", "通过"],
        ["TC19", "输出与参考结果一致", "4096/4096 mismatch=0", "通过"],
    ], widths=[1.6, 4.5, 6.0, 2.0], font_size=8.2)

    # ===== 5 证据 =====
    add_heading(doc, "5. 证据归档与复测说明", 1)
    add_body(doc,
             "按照实施方案第 10 章关于测试记录与验收材料的要求，本轮已形成任务记录、审计日志、"
             "分片输出、汇聚结果、参考比对摘要与报告图表。后续如需复测，可在相同冻结包与节点代理条件下，"
             "重新提交 DT01 并核验 case_evidence 与 merge_summary 字段。")
    add_table(doc, ["材料类型", "位置", "用途"], [
        ["本报告", "reports/L1_算力调度真实调度测试报告_20260811.docx", "正式文字结论与图表"],
        ["可视化", "reports/charts/", "用例分析插图"],
        ["现场证据包", "reports/evidence/l1-poc-evidence-latest.tgz", "父任务输出与 case_evidence"],
        ["调度源码", "src/", "控制面、节点代理、推理脚本"],
        ["实施方案", "docs/跨境算力调度PoC测试实施方案v0724.docx", "测试依据"],
    ], widths=[3.0, 7.5, 5.5], font_size=8.5)

    # ===== 6 结论 =====
    add_heading(doc, "6. 结论", 1)
    add_body(doc,
             "在实施方案规定的 L1 验证边界内，本轮真实调度测试表明：跨境任务可由新加坡控制面接入并完成完整性校验；"
             "调度系统能够依据单卡显存等硬约束在海南与重庆之间做出正确选择；标准 ResNet-50 分片推理可在两地真实 GPU 上执行；"
             "父任务结果可汇聚且与冻结参考输出一致；典型安全负向场景能够在执行前被拒绝并留下审计。")
    add_callout(doc, "报告结论",
                "本报告收录的 11 项用例全部通过，核心端到端流程连续三轮成功。"
                "该结论适用于本轮冻结任务包、实测硬件与已部署调度链路，"
                "用于支撑 L1 技术验证材料归档；不扩展解释为生产系统正式验收或运营商专线 SLA 达标证明。")

    add_heading(doc, "7. 签署栏", 1)
    add_table(doc, ["角色", "姓名", "日期", "签字"], [
        ["测试执行", "", "2026-08-11", ""],
        ["结果复核", "", "", ""],
        ["项目确认", "", "", ""],
    ], widths=[4, 4, 4, 4], font_size=9)

    DOCX.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(DOCX))
    # also copy to root for convenience during transition
    print("saved", DOCX, DOCX.stat().st_size)


if __name__ == "__main__":
    build()
