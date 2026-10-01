"""
Reconstrução de superfície com Open3D.

Cadeia: voxel downsample -> Statistical Outlier Removal -> DBSCAN
(clusters grandes ficam) -> normais orientadas para o roteador ->
Poisson Surface Reconstruction (com fallback Alpha Shapes) -> corte pela
caixa do cômodo -> decimação quadrática -> cor por distância ao roteador.

O Open3D é usado SOMENTE para processamento de geometria; a renderização
interativa é feita pelo VisPy embutido no PySide6.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .colors import distance_colormap
from .config import AppConfig
from .room import Room, plane_distance

try:  # pragma: no cover
    import open3d as o3d
    O3D_OK = True
except Exception:  # pragma: no cover
    o3d = None
    O3D_OK = False

try:  # scikit-learn fornece o DBSCAN usado no agrupamento
    from sklearn.cluster import DBSCAN
    SKLEARN_OK = True
except Exception:  # pragma: no cover
    DBSCAN = None  # type: ignore
    SKLEARN_OK = False

Vec = np.ndarray


@dataclass
class ClusterInfo:
    label: int
    size: int
    centroid: Vec
    extent: Vec


@dataclass
class ReconstructionResult:
    points: Vec                      # nuvem filtrada (N,3)
    point_colors: Vec                # (N,4) RGBA float32
    vertices: Vec                    # malha (V,3)
    triangles: Vec                   # (F,3) int32
    vertex_colors: Vec               # (V,4) RGBA float32
    edges: Vec                       # arestas da malha wireframe (E,2)
    clusters: List[ClusterInfo] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)
    method: str = "poisson"

    @property
    def has_mesh(self) -> bool:
        return len(self.triangles) > 0 and len(self.vertices) > 0


# --------------------------------------------------------------------------
def _mesh_edges(triangles: Vec) -> Vec:
    """Arestas únicas de uma malha triangular."""
    if len(triangles) == 0:
        return np.zeros((0, 2), dtype=np.int32)
    e = np.vstack([triangles[:, [0, 1]], triangles[:, [1, 2]],
                   triangles[:, [2, 0]]])
    e = np.sort(e, axis=1)
    return np.unique(e, axis=0)


def _fallback_hull(pts: Vec) -> Tuple[Vec, Vec]:
    if not O3D_OK or len(pts) < 8:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int32)
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(pts)
    try:
        m = pc.compute_convex_hull()[0]
        return np.asarray(m.vertices), np.asarray(m.triangles, dtype=np.int32)
    except Exception:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int32)


# --------------------------------------------------------------------------
def reconstruct(points: Vec, strengths: Optional[Vec], cfg: AppConfig,
                room: Room, use_poisson: bool = True) -> ReconstructionResult:
    """Executa a cadeia completa de reconstrução."""
    rec = cfg.recon
    metrics: Dict[str, float] = {}
    pts = np.asarray(points, dtype=np.float64)
    metrics["points_raw"] = float(len(pts))
    if len(pts) < 12 or not O3D_OK:
        return ReconstructionResult(
            points=pts,
            point_colors=(distance_colormap(np.linalg.norm(pts, axis=1), 0.0, 1.0)
                          if len(pts) else np.zeros((0, 4), np.float32)),
            vertices=np.zeros((0, 3)), triangles=np.zeros((0, 3), np.int32),
            vertex_colors=np.zeros((0, 4), np.float32),
            edges=np.zeros((0, 2), np.int32),
            metrics={**metrics, "error": float(not O3D_OK)},
            method="indisponível")

    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(pts)

    # --- 1) downsampling por voxel ---
    pc = pc.voxel_down_sample(rec.voxel)
    metrics["points_voxel"] = float(len(pc.points))

    # --- 2) remoção de outliers estatísticos ---
    pc, keep = pc.remove_statistical_outlier(nb_neighbors=rec.sor_k,
                                             std_ratio=rec.sor_std)
    metrics["points_sor"] = float(len(pc.points))

    # --- 3) DBSCAN (scikit-learn): mantém agrupamentos densos ---
    clusters: List[ClusterInfo] = []
    metrics["dbscan_clusters"] = 0.0
    p_arr = np.asarray(pc.points)
    if len(p_arr) >= max(rec.dbscan_min_samples, 4):
        if SKLEARN_OK:
            labels = DBSCAN(eps=rec.dbscan_eps,
                            min_samples=rec.dbscan_min_samples).fit_predict(p_arr)
            labels = np.asarray(labels, dtype=np.int64)
        else:  # pragma: no cover - reserva via Open3D
            labels = np.asarray(pc.cluster_dbscan(
                eps=rec.dbscan_eps, min_points=rec.dbscan_min_samples,
                print_progress=False), dtype=np.int64)
        keep_mask = np.zeros(len(labels), dtype=bool)
        for lab in np.unique(labels[labels >= 0]):
            sel = labels == lab
            if int(sel.sum()) >= rec.min_cluster_pts:
                keep_mask |= sel
                p = p_arr[sel]
                clusters.append(ClusterInfo(
                    label=int(lab), size=int(sel.sum()),
                    centroid=p.mean(axis=0),
                    extent=p.max(axis=0) - p.min(axis=0)))
        if keep_mask.sum() >= 12:
            pc = pc.select_by_index(np.nonzero(keep_mask)[0])
        metrics["dbscan_clusters"] = float(len(clusters))
    metrics["points_dbscan"] = float(len(pc.points))

    # --- 4) normais orientadas para o roteador (interior) ---
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(
        radius=rec.voxel * 4.0, max_nn=rec.normal_k))
    try:
        pc.orient_normals_towards_location(np.zeros(3))
    except Exception:
        pass

    p_np = np.asarray(pc.points)
    p_norm = np.linalg.norm(p_np, axis=1) if len(p_np) else np.zeros(0)
    vmin, vmax = 0.0, float(p_norm.max()) if len(p_norm) else 1.0
    point_colors = distance_colormap(p_norm, 0.0, vmax)

    # --- 5) superfície: Poisson (fallback Alpha Shapes -> Hull) ---
    verts = np.zeros((0, 3))
    tris = np.zeros((0, 3), dtype=np.int32)
    method = "poisson"
    if use_poisson and len(pc.points) >= 30:
        try:
            bbox = pc.get_axis_aligned_bounding_box()
            ext = float(np.linalg.norm(bbox.get_extent()))
            mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
                pc, depth=int(rec.poisson_depth),
                scale=float(max(1.01, rec.poisson_scale) * ext / 1.8),
                linear_fit=False, n_threads=0)
            dens = np.asarray(densities)
            if len(dens) > 0:
                thr = float(np.quantile(dens, rec.density_quantile))
                mesh.remove_vertices_by_mask(dens < thr)
            mesh.remove_degenerate_triangles()
            mesh.remove_duplicated_triangles()
            mesh.remove_duplicated_vertices()
            mesh.remove_unreferenced_vertices()
            if len(mesh.triangles) > 0:
                verts = np.asarray(mesh.vertices)
                tris = np.asarray(mesh.triangles, dtype=np.int32)
            else:
                method = "poisson-vazio"
        except Exception as exc:  # Poisson instável -> fallback
            method = f"poisson-falhou({type(exc).__name__})"

    if len(tris) == 0:
        method = "alpha-shapes"
        try:
            mesh = o3d.geometry.TriangleMesh.create_from_point_cloud_alpha_shape(
                pc, rec.alpha)
            mesh.remove_degenerate_triangles()
            mesh.remove_duplicated_triangles()
            verts = np.asarray(mesh.vertices)
            tris = np.asarray(mesh.triangles, dtype=np.int32)
        except Exception:
            method = "convex-hull"
            verts, tris = _fallback_hull(p_np)

    # --- 6) corte pela caixa do cômodo (margem de 0,30 m) ---
    if len(tris) > 0:
        m = 0.30
        (x0, x1), (y0, y1), (z0, z1) = room.cfg.xlim, room.cfg.ylim, room.cfg.zlim
        lo = np.array([x0 - m, y0 - m, z0 - m])
        hi = np.array([x1 + m, y1 + m, z1 + m])
        inside = np.all((verts >= lo) & (verts <= hi), axis=1)
        vmask = np.nonzero(inside)[0]
        if len(vmask) > 0:
            remap = -np.ones(len(verts), dtype=np.int64)
            remap[vmask] = np.arange(len(vmask))
            tmask = np.all(remap[tris] >= 0, axis=1)
            tris = remap[tris[tmask]]
            verts = verts[vmask]

    # --- 7) decimação para renderização interativa ---
    if len(tris) > rec.decimate_target:
        try:
            mesh = o3d.geometry.TriangleMesh()
            mesh.vertices = o3d.utility.Vector3dVector(verts)
            mesh.triangles = o3d.utility.Vector3iVector(tris.astype(np.int32))
            mesh = mesh.simplify_quadric_decimation(rec.decimate_target)
            verts = np.asarray(mesh.vertices)
            tris = np.asarray(mesh.triangles, dtype=np.int32)
        except Exception:
            pass

    # --- 8) cores por distância ao roteador + métricas ---
    vnorm = np.linalg.norm(verts, axis=1) if len(verts) else np.zeros(0)
    vmax_all = float(max(vmax, vnorm.max() if len(vnorm) else 1.0, 1e-6))
    vertex_colors = distance_colormap(vnorm, 0.0, vmax_all)
    edges = _mesh_edges(tris)

    metrics["vertices"] = float(len(verts))
    metrics["triangles"] = float(len(tris))
    if len(p_np):
        metrics["bbox_x"] = float(p_np[:, 0].max() - p_np[:, 0].min())
        metrics["bbox_y"] = float(p_np[:, 1].max() - p_np[:, 1].min())
        metrics["bbox_z"] = float(p_np[:, 2].max() - p_np[:, 2].min())
        d_cloud = plane_distance(p_np, room)
        metrics["err_cloud_mean_m"] = float(np.mean(d_cloud))
        metrics["err_cloud_median_m"] = float(np.median(d_cloud))
        metrics["cover_cloud_25cm"] = float(np.mean(d_cloud < 0.25))
    if len(verts):
        d_v = plane_distance(verts, room)
        metrics["err_surface_mean_m"] = float(np.mean(d_v))
        metrics["cover_surface_25cm"] = float(np.mean(d_v < 0.25))

    return ReconstructionResult(points=p_np, point_colors=point_colors,
                                vertices=verts, triangles=tris,
                                vertex_colors=vertex_colors, edges=edges,
                                clusters=clusters, metrics=metrics,
                                method=method)
