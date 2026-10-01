"""
Teste automatizado headless do pipeline completo (sem interface gráfica).

    python selftest.py                  # métricas + PNG de verificação
    python selftest.py --no-png         # apenas métricas no terminal
    python selftest.py --png mapa.png   # caminho do PNG de verificação
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

import numpy as np

from csi_mapper.config import default_config
from csi_mapper.csi_source import MockCSISource
from csi_mapper.dsp import MappingPipeline
from csi_mapper.reconstruction import O3D_OK, reconstruct
from csi_mapper.room import Room


def render_png(rec, room, path: str) -> Optional[str]:
    """Render de verificação com matplotlib (independe de GL/Vulkan)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection

        fig = plt.figure(figsize=(15, 10), facecolor="#0d1117")
        ax = fig.add_subplot(111, projection="3d", facecolor="#0d1117")

        if len(rec.triangles) > 0:
            tris = rec.vertices[rec.triangles]
            cols = rec.vertex_colors[rec.triangles].mean(axis=1)[:, :3]
            pc = Poly3DCollection(tris, facecolors=cols, edgecolors="none",
                                  alpha=0.85)
            ax.add_collection3d(pc)
        if len(rec.points):
            ax.scatter(rec.points[:, 0], rec.points[:, 1], rec.points[:, 2],
                       c=rec.point_colors[:, :3], s=9, depthshade=False,
                       label="reflexões (CSI)")

        (x0, x1), (y0, y1), (z0, z1) = room.cfg.xlim, room.cfg.ylim, room.cfg.zlim
        box = np.array([[x0, y0, z0], [x1, y1, z1]])
        edges = []
        for i in (0, 1):
            for j in (0, 1):
                edges.append(np.array([[x0, y0, z0], [x1, y0, z0]]))
        # caixa de referência da sala real
        for s, e in [((x0, y0, z0), (x1, y0, z0)), ((x1, y0, z0), (x1, y1, z0)),
                     ((x1, y1, z0), (x0, y1, z0)), ((x0, y1, z0), (x0, y0, z0)),
                     ((x0, y0, z1), (x1, y0, z1)), ((x1, y0, z1), (x1, y1, z1)),
                     ((x1, y1, z1), (x0, y1, z1)), ((x0, y1, z1), (x0, y0, z1)),
                     ((x0, y0, z0), (x0, y0, z1)), ((x1, y0, z0), (x1, y0, z1)),
                     ((x1, y1, z0), (x1, y1, z1)), ((x0, y1, z0), (x0, y1, z1))]:
            ax.plot(*zip(s, e), color="#4a90d9", lw=0.8, alpha=0.55)

        ax.scatter([0], [0], [0], c="#00e5ff", s=140, marker="*",
                   label="roteador (0,0,0)")
        lim = max(room.cfg.size) / 2 + 0.4
        ax.set_xlim(-lim, lim); ax.set_ylim(-lim, lim); ax.set_zlim(-lim, lim)
        ax.set_xlabel("X (m)", color="#d7dee8")
        ax.set_ylabel("Y (m)", color="#d7dee8")
        ax.set_zlabel("Z (m)", color="#d7dee8")
        ax.tick_params(colors="#8fa3b8")
        for pane in (ax.xaxis, ax.yaxis, ax.zaxis):
            pane.pane.set_facecolor("#111823")
            pane.pane.set_alpha(1.0)
        m = rec.metrics
        ax.set_title(
            f"Reconstrução por CSI — {rec.method} | "
            f"{len(rec.triangles)} triângulos | erro médio da malha "
            f"{m.get('err_surface_mean_m', 0)*100:.1f} cm | cobertura "
            f"{m.get('cover_surface_25cm', 0)*100:.0f}% (<25 cm)",
            color="#d7dee8", fontsize=12)
        ax.legend(facecolor="#111823", labelcolor="#d7dee8", loc="upper right")
        ax.view_init(elev=22, azim=-48)
        fig.tight_layout()
        fig.savefig(path, dpi=110, facecolor="#0d1117")
        plt.close(fig)
        return path
    except Exception as exc:
        print(f"(PNG não gerado: {type(exc).__name__}: {exc})")
        return None


def run(png: Optional[str] = None) -> int:
    cfg = default_config()
    room = Room(cfg.room)
    print("=" * 74)
    print("MAPEADOR 3D DE ESPAÇO POR CSI — TESTE HEADLESS")
    print("=" * 74)
    print(f"Sala: {cfg.room.size[0]} x {cfg.room.size[1]} x {cfg.room.size[2]} m | "
          f"roteador na origem | {cfg.array.n_ant} antenas | "
          f"{cfg.radio.n_sub} subportadoras | BW {cfg.radio.bandwidth/1e6:.0f} MHz")
    print(f"Open3D disponível: {O3D_OK}")

    source = MockCSISource(cfg, seed=11)
    t0 = time.perf_counter()
    pipe = MappingPipeline(cfg, room, source, pace=False)
    result = pipe.run()
    t_dsp = time.perf_counter() - t0

    print("-" * 74)
    print(f"Calibração: {result.calib.describe()}")
    print(f"Pontos de reflexão aceitos: {len(result.points)} "
          f"(grupos: {result.groups_processed}, candidatos: "
          f"{int(result.stats['candidates'])})")
    print(f"Tempo do DSP + varredura: {t_dsp:.2f} s")

    t1 = time.perf_counter()
    rec = reconstruct(result.points, result.strengths, cfg, room)
    t_rec = time.perf_counter() - t1
    m = rec.metrics
    print("-" * 74)
    print(f"Reconstrução ({rec.method}) em {t_rec:.2f} s")
    print(f"  pontos: {int(m.get('points_raw', 0))} → {len(rec.points)} filtrados")
    print(f"  clusters DBSCAN: {int(m.get('dbscan_clusters', 0))}")
    print(f"  malha: {len(rec.vertices)} vértices / {len(rec.triangles)} triângulos")
    if "bbox_x" in m:
        print(f"  extensão mapeada: {m['bbox_x']:.2f} x {m['bbox_y']:.2f} x "
              f"{m['bbox_z']:.2f} m  (real: {cfg.room.size[0]} x "
              f"{cfg.room.size[1]} x {cfg.room.size[2]} m)")
    if "err_cloud_mean_m" in m:
        print(f"  erro da nuvem: média {m['err_cloud_mean_m']*100:.1f} cm | "
              f"mediana {m['err_cloud_median_m']*100:.1f} cm | "
              f"cobertura <25 cm: {m.get('cover_cloud_25cm', 0)*100:.1f}%")
    if "err_surface_mean_m" in m:
        print(f"  erro da malha: média {m['err_surface_mean_m']*100:.1f} cm | "
              f"cobertura <25 cm: {m.get('cover_surface_25cm', 0)*100:.1f}%")

    ok_mesh = len(rec.triangles) > 0
    dims_ok = True
    if "bbox_x" in m:
        ratio = m["bbox_x"] / cfg.room.size[0]
        dims_ok = 0.5 < ratio < 1.8
        print(f"  razão bbox_x / sala_x: {ratio:.2f}")
    print("-" * 74)
    print(f"MALHA NÃO VAZIA: {ok_mesh}   |   DIMENSÕES PLAUSÍVEIS: {dims_ok}")
    print(f"VEREDITO: {'APROVADO' if (ok_mesh and dims_ok) else 'REPROVADO'}")

    if png:
        out = render_png(rec, room, png)
        if out:
            print(f"PNG de verificação: {os.path.abspath(out)}")
    return 0 if (ok_mesh and dims_ok) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", type=str, default="screenshots/mapa_3d.png")
    ap.add_argument("--no-png", action="store_true")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.png) or ".", exist_ok=True)
    return run(png=None if args.no_png else args.png)


if __name__ == "__main__":
    sys.exit(main())
