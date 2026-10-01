"""
Exportacao dos dados capturados: medidas, distancias, vetores, nuvem e malha.

Formatos: CSV, JSON, PDF (relatorio), XLSX (se openpyxl disponivel),
OBJ / PLY / STL para a malha, XYZ para a nuvem.

O cliente escolhe os campos por checkboxes (`fields`) segundo a necessidade.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import math
import os
import re
from typing import Dict, List, Optional

import numpy as np

from server import db

try:
    from reportlab.lib import colors as rlcolors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    PDF_OK = True
except Exception:  # pragma: no cover
    PDF_OK = False

try:
    import openpyxl
    XLSX_OK = True
except Exception:  # pragma: no cover
    openpyxl = None
    XLSX_OK = False


MEASUREMENT_FIELDS: Dict[str, Dict[str, str]] = {
    "id": {"label": "ID", "type": "int"},
    "kind": {"label": "Tipo (largura/profundidade/altura/distancia)", "type": "str"},
    "label": {"label": "Rotulo", "type": "str"},
    "value_m": {"label": "Valor (m)", "type": "float"},
    "value_cm": {"label": "Valor (cm)", "type": "float"},
    "dx": {"label": "delta X (m)", "type": "float"},
    "dy": {"label": "delta Y (m)", "type": "float"},
    "dz": {"label": "delta Z (m)", "type": "float"},
    "length_3d": {"label": "Distancia 3D (m)", "type": "float"},
    "p1": {"label": "Ponto inicial (x,y,z)", "type": "vec3"},
    "p2": {"label": "Ponto final (x,y,z)", "type": "vec3"},
    "capture_id": {"label": "Captura", "type": "int"},
    "project_id": {"label": "Projeto", "type": "int"},
    "note": {"label": "Observacao", "type": "str"},
    "created_at": {"label": "Criado em", "type": "date"},
    "visible": {"label": "Visivel", "type": "bool"},
}

VECTOR_FIELDS: Dict[str, Dict[str, str]] = {
    "id": {"label": "ID", "type": "int"},
    "name": {"label": "Nome do vetor", "type": "str"},
    "n_points": {"label": "No de pontos", "type": "int"},
    "n_segments": {"label": "No de segmentos", "type": "int"},
    "total_m": {"label": "Comprimento total (m)", "type": "float"},
    "closed": {"label": "Fechado", "type": "bool"},
    "perimeter_m": {"label": "Perimetro/area fechada (m / m2)", "type": "float"},
    "area_m2": {"label": "Area projetada no piso (m2)", "type": "float"},
    "bbox_x": {"label": "Extensao X (m)", "type": "float"},
    "bbox_y": {"label": "Extensao Y (m)", "type": "float"},
    "bbox_z": {"label": "Extensao Z (m)", "type": "float"},
    "segments": {"label": "Segmentos (comprimentos)", "type": "list"},
    "points": {"label": "Vertices do vetor (x,y,z)", "type": "list"},
    "note": {"label": "Observacao", "type": "str"},
    "created_at": {"label": "Criado em", "type": "date"},
}

CLOUD_FIELDS: Dict[str, Dict[str, str]] = {
    "x": {"label": "X (m)"},
    "y": {"label": "Y (m)"},
    "z": {"label": "Z (m)"},
    "distance_m": {"label": "Distancia ao roteador (m)"},
    "strength": {"label": "Intensidade"},
}

SCOPES = ["measurements", "vectors", "capture_summary", "cloud", "mesh"]


def fields_registry() -> dict:
    return {"measurements": MEASUREMENT_FIELDS, "vectors": VECTOR_FIELDS,
            "cloud": CLOUD_FIELDS, "scopes": SCOPES,
            "formats": ["csv", "json", "pdf"] + (["xlsx"] if XLSX_OK else []),
            "mesh_formats": ["obj", "ply", "stl"],
            "pdf": PDF_OK}


# --------------------------------------------------------------------------
def _bbox(pts: np.ndarray):
    if len(pts) == 0:
        return 0.0, 0.0, 0.0
    e = pts.max(axis=0) - pts.min(axis=0)
    return float(e[0]), float(e[1]), float(e[2])


def polygon_area_xy(pts: np.ndarray) -> float:
    """Area projetada no piso (formula do sapateiro) -- util para plantas."""
    if len(pts) < 3:
        return 0.0
    x, y = pts[:, 0], pts[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def vector_derived(vec_row: dict) -> dict:
    pts = np.asarray(db.jloads(vec_row.get("points"), []), dtype=float).reshape(-1, 3)
    segs = []
    for i in range(len(pts) - 1):
        segs.append(float(np.linalg.norm(pts[i + 1] - pts[i])))
    total = float(sum(segs))
    per = total
    if int(vec_row.get("closed", 0)) and len(pts) > 2:
        last = float(np.linalg.norm(pts[0] - pts[-1]))
        segs.append(last)
        per = total + last
    bx, by, bz = _bbox(pts)
    return {"points": db.arrN(pts) if len(pts) else [],
            "segments": [round(s, 4) for s in segs],
            "n_points": int(len(pts)), "n_segments": int(len(segs)),
            "total_m": round(total, 4), "perimeter_m": round(per, 4),
            "area_m2": round(polygon_area_xy(pts), 4),
            "bbox_x": round(bx, 4), "bbox_y": round(by, 4), "bbox_z": round(bz, 4)}


def measurement_row(row: dict) -> dict:
    p1 = np.asarray(db.jloads(row.get("p1"), [0, 0, 0]), dtype=float)
    p2 = np.asarray(db.jloads(row.get("p2"), [0, 0, 0]), dtype=float)
    v = float(row.get("value_m") or 0.0)
    return {"id": row.get("id"), "kind": row.get("kind"), "label": row.get("label") or "",
            "value_m": round(v, 4), "value_cm": round(v * 100.0, 2),
            "dx": round(float(row.get("dx") or 0), 4),
            "dy": round(float(row.get("dy") or 0), 4),
            "dz": round(float(row.get("dz") or 0), 4),
            "length_3d": round(v, 4),
            "p1": db.arr3(p1), "p2": db.arr3(p2),
            "capture_id": row.get("capture_id"), "project_id": row.get("project_id"),
            "note": row.get("note") or "",
            "created_at": _iso(row.get("created_at")),
            "visible": bool(row.get("visible", 1))}


def _iso(ts) -> str:
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


def _fmt_value(v):
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, (list, tuple)):
        return v
    return v


def build_table(scope: str, rows: List[dict], fields: List[str]) -> List[dict]:
    out = []
    for r in rows:
        o = {}
        for f in fields:
            if f in r:
                o[f] = _fmt_value(r[f])
        out.append(o)
    return out


# --------------------------------------------------------------------------
def export_tabular(fmt: str, scope: str, fields: List[str], rows: List[dict],
                   meta: dict, path_base: str):
    """Gera CSV/JSON/XLSX/PDF. Retorna (caminho, mime)."""
    table = build_table(scope, rows, fields)
    title = meta.get("title", scope)
    if fmt == "json":
        path = path_base + ".json"
        payload = {"meta": meta, "scope": scope, "fields": fields, "count": len(table),
                   "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                   "rows": table}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return path, "application/json"
    if fmt == "xlsx" and XLSX_OK:
        path = path_base + ".xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = scope[:28] or "dados"
        ws.append(fields)
        for r in table:
            ws.append([json.dumps(r[f], ensure_ascii=False) if isinstance(r[f], (list, dict))
                       else r[f] for f in fields])
        for i, f in enumerate(fields, start=1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = max(12, len(f) + 4)
        wb.save(path)
        return path, ("application/vnd.openxmlformats-officedocument."
                      "spreadsheetml.sheet")
    if fmt == "pdf" and PDF_OK:
        path = path_base + ".pdf"
        _pdf_report(path, title, meta, fields, table)
        return path, "application/pdf"
    if fmt == "pdf" and not PDF_OK:
        raise RuntimeError("PDF indisponivel: instale reportlab (pip install reportlab)")
    # csv
    path = path_base + ".csv"
    delim = ";" if meta.get("excel", False) else ","
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=delim)
        w.writerow([f for f in fields])
        for r in table:
            row = []
            for f in fields:
                v = r[f]
                if isinstance(v, (list, dict)):
                    row.append(json.dumps(v, ensure_ascii=False))
                elif isinstance(v, bool):
                    row.append("sim" if v else "nao")
                else:
                    row.append(v)
            w.writerow(row)
    return path, "text/csv"


def _pdf_report(path, title, meta, fields, table):
    styles = getSampleStyleSheet()
    h = ParagraphStyle("h", parent=styles["Heading1"], fontSize=16, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=11, spaceAfter=4)
    small = ParagraphStyle("s", parent=styles["BodyText"], fontSize=8, leading=10)
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=16 * mm, rightMargin=14 * mm,
                            topMargin=16 * mm, bottomMargin=14 * mm,
                            title=title, author="Wi-Fi CSI Space Mapper")
    story = [Paragraph(title, h)]
    info = meta.get("info") or {}
    if info:
        for k, v in list(info.items())[:18]:
            story.append(Paragraph(f"<b>{k}:</b> {v}", small))
        story.append(Spacer(1, 8))
    story.append(Paragraph(meta.get("scope_label", ""), h2))
    if table:
        head = [Paragraph(f"<b>{f}</b>", small) for f in fields]
        data = [head]
        for r in table[:900]:
            cells = []
            for f in fields:
                v = r[f]
                if isinstance(v, (list, dict)):
                    v = json.dumps(v, ensure_ascii=False)
                elif isinstance(v, bool):
                    v = "sim" if v else "nao"
                cells.append(Paragraph(str(v), small))
            data.append(cells)
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rlcolors.HexColor("#12314a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rlcolors.white),
            ("GRID", (0, 0), (-1, -1), 0.3, rlcolors.HexColor("#9fb0c4")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [rlcolors.white, rlcolors.HexColor("#eef3f8")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(t)
        if len(table) > 900:
            story.append(Spacer(1, 6))
            story.append(Paragraph(f"({len(table) - 900} linhas omitidas no PDF; "
                                   "use CSV/XLSX para o conjunto completo)", small))
    else:
        story.append(Paragraph("Nenhum registro.", small))
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Gerado pelo portal Wi-Fi CSI Space Mapper. Valores em metros (m) e centimetros (cm). "
        "Medidas obtidas com reguas dinamicas sobre a nuvem de reflexoes CSI.", small))
    doc.build(story)


# --------------------------------------------------------------------------
def export_cloud(fmt: str, points: np.ndarray, strengths, path_base: str,
                 fields: List[str] = None, meta: dict = None):
    fields = fields or ["x", "y", "z", "distance_m", "strength"]
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    st = np.asarray(strengths if strengths is not None else np.ones(len(pts)),
                    dtype=np.float64).reshape(-1)
    dist = np.linalg.norm(pts, axis=1)
    if fmt == "json":
        path = path_base + ".json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"meta": meta or {}, "fields": fields, "count": int(len(pts)),
                       "points": [[round(float(p[0]), 4), round(float(p[1]), 4),
                                   round(float(p[2]), 4), round(float(s), 4),
                                   round(float(d), 4)]
                                  for p, s, d in zip(pts, st, dist)]}, fh, indent=1)
        return path, "application/json"
    if fmt == "xyz":
        path = path_base + ".xyz"
        with open(path, "w", encoding="utf-8") as fh:
            for p in pts:
                fh.write(f"{p[0]:.4f} {p[1]:.4f} {p[2]:.4f}\n")
        return path, "text/plain"
    path = path_base + ".csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        for p, s, d in zip(pts, st, dist):
            row = []
            for f in fields:
                row.append({"x": round(float(p[0]), 4), "y": round(float(p[1]), 4),
                            "z": round(float(p[2]), 4), "distance_m": round(float(d), 4),
                            "strength": round(float(s), 4)}.get(f, ""))
            w.writerow(row)
    return path, "text/csv"


def export_mesh(fmt: str, vertices, triangles, path_base: str, meta: dict = None):
    v = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    f = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if fmt == "obj":
        path = path_base + ".obj"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# Wi-Fi CSI Space Mapper  projeto={meta.get('project', '')}\n")
            for p in v:
                fh.write(f"v {p[0]:.5f} {p[1]:.5f} {p[2]:.5f}\n")
            for t in f:
                fh.write(f"f {t[0] + 1} {t[1] + 1} {t[2] + 1}\n")
        return path, "text/plain"
    if fmt == "ply":
        path = path_base + ".ply"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("ply\nformat ascii 1.0\n")
            fh.write(f"element vertex {len(v)}\nproperty float x\nproperty float y\n"
                     "property float z\n")
            fh.write(f"element face {len(f)}\nproperty list uchar int vertex_indices\n")
            fh.write("end_header\n")
            for p in v:
                fh.write(f"{p[0]:.5f} {p[1]:.5f} {p[2]:.5f}\n")
            for t in f:
                fh.write(f"3 {t[0]} {t[1]} {t[2]}\n")
        return path, "application/octet-stream"
    if fmt == "stl":
        path = path_base + ".stl"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("solid csi_space_mapper\n")
            for t in f:
                a, b, c = v[t[0]], v[t[1]], v[t[2]]
                n = np.cross(b - a, c - a)
                ln = float(np.linalg.norm(n))
                n = n / ln if ln > 1e-12 else np.array([0.0, 0.0, 1.0])
                fh.write(f"facet normal {n[0]:.5f} {n[1]:.5f} {n[2]:.5f}\n outer loop\n")
                for p in (a, b, c):
                    fh.write(f"  vertex {p[0]:.5f} {p[1]:.5f} {p[2]:.5f}\n")
                fh.write(" endloop\nendfacet\n")
            fh.write("endsolid csi_space_mapper\n")
        return path, "model/stl"
    raise RuntimeError(f"formato de malha nao suportado: {fmt}")


def safe_name(text: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^A-Za-z0-9_.\-]+", "_", (text or "").strip())
    s = s.strip("._-") or "export"
    return s[:maxlen]


def export_path(project_name: str, kind: str, stamp: str = None) -> str:
    stamp = stamp or _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{safe_name(project_name, 36)}_{kind}_{stamp}"
    os.makedirs(db.EXPORT_DIR, exist_ok=True)
    return os.path.join(db.EXPORT_DIR, base)
