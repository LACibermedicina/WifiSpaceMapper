"""
Pipeline de DSP do CSI.

Etapas
------
1. CALIBRAÇÃO (0-3 s): média coerente dos pacotes estáticos, estimação do
   percurso direto (LoS) e extração da fase residual por cadeia RF
   (descasamento de cadeia é estimado e removido), além da covariância de
   ruído / SNR do enlace.
2. VARREDURA (3-10 s): para cada posição de varredura, monta a matriz CSI,
   aplica a correção de cadeia e estima percursos por
      - AoA 2D (azimute x elevação) via MUSIC com suavização em frequência;
      - ToF via MUSIC 1D sobre as subportadoras (projeção no autovetor de
        direção estimado), com suavização espacial forward-backward.
3. GEOMETRIA: cada percurso (u, L) é convertido em um ponto de reflexão 3D
   resolvendo |RX + t*u - TX| + t = L (raio-elipsoide), transformando ToF+AoA
   em coordenadas cartesianas (X, Y, Z).
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from scipy.ndimage import maximum_filter
from scipy.optimize import brentq

from .config import C_LIGHT, AppConfig
from .csi_source import CSIPacket
from .room import Room

Vec = np.ndarray


# --------------------------------------------------------------------------
# geometria do arranjo
# --------------------------------------------------------------------------
def default_array(cfg: AppConfig, n_ant: int) -> Vec:
    """Geometria (n_ant,3) do arranjo no frame local do receptor."""
    lam = C_LIGHT / cfg.radio.f0
    d = lam * cfg.array.spacing_fraction
    ac = cfg.array
    if n_ant == ac.n_ant:
        gx = (np.arange(ac.n_x) - (ac.n_x - 1) / 2.0) * d
        gy = (np.arange(ac.n_y) - (ac.n_y - 1) / 2.0) * d
        pts = [[x, y, 0.0] for x in gx for y in gy]
        for i in range(ac.n_z_extra):
            pts.append([0.0, 0.0, (i - (ac.n_z_extra - 1) / 2.0) * d])
        return np.asarray(pts, dtype=np.float64)
    # fallback: ULA em X (hardware desconhecido)
    gx = (np.arange(n_ant) - (n_ant - 1) / 2.0) * d
    return np.stack([gx, np.zeros(n_ant), np.zeros(n_ant)], axis=1)


def spherical_unit(az_deg: Vec, el_deg: Vec) -> Vec:
    """(az, el) em graus -> vetor unitário (…,3) no frame local."""
    az = np.deg2rad(np.asarray(az_deg, dtype=np.float64))
    el = np.deg2rad(np.asarray(el_deg, dtype=np.float64))
    return np.stack([np.cos(el) * np.cos(az),
                     np.cos(el) * np.sin(az),
                     np.sin(el)], axis=-1)


def unit_to_spherical(u: Vec) -> Tuple[float, float]:
    """Vetor unitário -> (azimute, elevação) em graus."""
    u = np.asarray(u, dtype=np.float64)
    n = float(np.linalg.norm(u))
    if n < 1e-12:
        return 0.0, 0.0
    u = u / n
    az = math.degrees(math.atan2(u[1], u[0]))
    el = math.degrees(math.asin(max(-1.0, min(1.0, u[2]))))
    return az, el


# --------------------------------------------------------------------------
# Estruturas de resultado
# --------------------------------------------------------------------------
@dataclass
class PathEstimate:
    point: Vec                 # ponto de reflexão (m, frame do roteador)
    az_deg: float
    el_deg: float
    total_len: float           # caminho TX->parede->RX (m)
    strength: float
    group: int
    rx_pos: Vec


@dataclass
class CalibrationResult:
    phase_correction: Vec = field(default_factory=lambda: np.ones(1, complex))
    los_len: float = 0.0
    los_az_deg: float = 0.0
    los_el_deg: float = 0.0
    snr_db: float = 0.0
    packets_used: int = 0
    spectrum: Optional[Vec] = None
    az_grid: Optional[Vec] = None
    el_grid: Optional[Vec] = None

    def describe(self) -> str:
        return (f"LoS {self.los_len:.2f} m @ az {self.los_az_deg:+.1f}° / "
                f"el {self.los_el_deg:+.1f}°  |  SNR {self.snr_db:.1f} dB  |  "
                f"{self.packets_used} pacotes")


@dataclass
class MappingResult:
    points: Vec
    strengths: Vec
    groups: Vec
    paths: List[PathEstimate]
    calib: CalibrationResult
    stats: Dict[str, float]
    traj: Vec
    groups_processed: int = 0


# --------------------------------------------------------------------------
# Motor MUSIC
# --------------------------------------------------------------------------
class MusicEstimator:
    """Estimador de super-resolução de AoA (2D) e ToF (1D) sobre CSI MIMO-OFDM."""

    def __init__(self, cfg: AppConfig, array_local: Vec):
        self.cfg = cfg
        self.array = np.asarray(array_local, dtype=np.float64)
        self.n_ant = len(self.array)
        self.lam = C_LIGHT / cfg.radio.f0
        self.freqs = cfg.radio.f0 + cfg.radio.delta_f * np.arange(cfg.radio.n_sub)
        self._build_coarse_grid()
        self._build_delay_grid()

    # ---------------- grades ----------------
    def _build_coarse_grid(self) -> None:
        d = self.cfg.dsp
        az = np.arange(-180.0, 180.0, d.az_step_coarse)
        el = np.arange(-84.0, 84.0 + 1e-9, d.el_step_coarse)
        self.az_grid, self.el_grid = az, el
        AZ, EL = np.meshgrid(az, el, indexing="ij")
        U = spherical_unit(AZ, EL).reshape(-1, 3)
        self._grid_shape = (len(az), len(el))
        self._grid_A = self._steer_U(U)

    def _build_delay_grid(self) -> None:
        d = self.cfg.dsp
        self.delay_grid = np.arange(0.60, d.delay_max_m, d.delay_step_m)
        M = d.delay_win
        f = self.freqs[::d.delay_decim]
        f = f[: M + (len(f) - M)]           # garante >= M
        self.delay_freqs = f
        # matriz de direção de atraso (n_L, M): exp(-j 2pi f L / c)
        self._delay_B = np.exp(-1j * 2.0 * np.pi
                               * self.delay_grid[:, None] * f[None, :] / C_LIGHT)

    # ---------------- vetores de direção ----------------
    def _steer_U(self, U: Vec) -> Vec:
        """(N,3) direções -> (N,n_ant) vetores de direção."""
        ph = (2.0 * np.pi / self.lam) * (U @ self.array.T)
        return np.exp(1j * ph)

    def steering_vec(self, az_deg: float, el_deg: float) -> Vec:
        return self._steer_U(spherical_unit(np.array([az_deg]),
                                            np.array([el_deg])))[0]

    def _fine_spectrum(self, En: Vec, az0: float, el0: float) -> Tuple[float, float, float]:
        """Busca refinada em torno de um pico grosseiro."""
        d = self.cfg.dsp
        span_az = 2.0 * d.az_step_coarse
        span_el = 2.0 * d.el_step_coarse
        az = az0 + np.arange(-span_az, span_az + 1e-9, d.refine_step)
        el = np.clip(el0 + np.arange(-span_el, span_el + 1e-9, d.refine_step),
                     -89.0, 89.0)
        AZ, EL = np.meshgrid(az, el, indexing="ij")
        A = self._steer_U(spherical_unit(AZ, EL).reshape(-1, 3))
        p = 1.0 / np.sum(np.abs(A @ En) ** 2, axis=1)
        i = int(np.argmax(p))
        return float(AZ.reshape(-1)[i]), float(EL.reshape(-1)[i]), float(p[i])

    # ---------------- covariância ----------------
    def _cov_freq(self, H: Vec, win: int, stride: int) -> Vec:
        n, K = H.shape
        win = int(min(win, K))
        starts = np.arange(0, max(K - win + 1, 1), max(1, stride))
        acc = np.zeros((n, n), dtype=np.complex128)
        for s in starts:
            X = H[:, s:s + win]
            X = X - X.mean(axis=1, keepdims=True)
            acc += X @ X.conj().T
        acc /= float(len(starts) * win)
        return (acc + acc.conj().T) * 0.5

    @staticmethod
    def _noise_subspace(R: Vec, n_sig: int) -> Vec:
        w, V = np.linalg.eigh(R)
        order = np.argsort(w)[::-1]
        w = w[order]
        V = V[:, order]
        n_sig = int(max(1, min(n_sig, len(w) - 2)))
        return V[:, n_sig:], w

    def _n_sig(self, w: Vec) -> int:
        d = self.cfg.dsp
        w = np.maximum(w, 1e-14)
        thr = max(d.n_sig_ratio * w[0], 1.3 * float(np.median(w)))
        n = int(np.sum(w > thr))
        return int(max(1, min(n, d.max_paths)))

    # ---------------- AoA 2D ----------------
    def aoa_spectrum(self, H: Vec) -> Tuple[Vec, Vec]:
        d = self.cfg.dsp
        R = self._cov_freq(H, d.subband_win, d.subband_stride)
        _, w = self._noise_subspace(R, d.max_paths)
        En, _ = self._noise_subspace(R, self._n_sig(w))
        p = 1.0 / np.sum(np.abs(self._grid_A @ En) ** 2, axis=1)
        return p.reshape(self._grid_shape), En

    def find_aoa_peaks(self, spec: Vec, En: Vec, n_max: Optional[int] = None
                       ) -> List[Tuple[float, float, float]]:
        d = self.cfg.dsp
        n_max = int(n_max or d.max_paths)
        mf = maximum_filter(spec, size=(7, 7), mode=("wrap", "nearest"))
        mask = (spec >= mf * 0.999) & (spec > d.peak_rel_threshold * spec.max())
        ia, ie = np.nonzero(mask)
        cand = sorted(((float(spec[a, e]), float(self.az_grid[a]),
                        float(self.el_grid[e])) for a, e in zip(ia, ie)),
                      key=lambda c: -c[0])
        picked: List[Tuple[float, float, float]] = []
        for s, az, el in cand:
            if all(_ang_sep(az, el, p[1], p[2]) >= d.min_sep_deg for p in picked):
                picked.append((s, az, el))
            if len(picked) >= n_max:
                break
        # refinamento sub-grau
        out = []
        for s, az, el in picked:
            az2, el2, s2 = self._fine_spectrum(En, az, el)
            if s2 >= s:
                out.append((s2, az2, el2))
            else:
                out.append((s, az, el))
        out.sort(key=lambda c: -c[0])
        return out

    # ---------------- ToF 1D ----------------
    def tof_spectrum(self, H: Vec, az_deg: float, el_deg: float) -> Vec:
        d = self.cfg.dsp
        a = self.steering_vec(az_deg, el_deg)
        y = (a.conj() @ H) / self.n_ant                 # resposta na direção
        dec = int(max(1, d.delay_decim))
        y = y[::dec]
        M = int(min(d.delay_win, len(y)))
        starts = np.arange(0, max(len(y) - M + 1, 1), max(1, d.delay_stride))
        acc = np.zeros((M, M), dtype=np.complex128)
        J = np.fliplr(np.eye(M))
        for s in starts:
            seg = y[s:s + M]
            seg = seg - seg.mean()
            Rf = np.outer(seg, seg.conj())
            acc += 0.5 * (Rf + J @ Rf.conj() @ J)       # forward-backward
        acc /= float(len(starts))
        B = self._delay_B[:, : M]
        w, V = np.linalg.eigh(acc)
        order = np.argsort(w)[::-1]
        w = np.maximum(w[order], 1e-14)
        V = V[:, order]
        n_sig = int(max(1, min(self._n_sig(w), M - 2, d.delay_peaks)))
        En = V[:, n_sig:]
        p = 1.0 / np.sum(np.abs(B @ En) ** 2, axis=1)
        return p

    def find_tof_peaks(self, spec: Vec, n_max: Optional[int] = None
                       ) -> List[Tuple[float, float]]:
        d = self.cfg.dsp
        n_max = int(n_max or d.delay_peaks)
        mf = maximum_filter(spec, size=21, mode="nearest")
        idx = np.nonzero((spec >= mf * 0.999)
                         & (spec > d.delay_rel_threshold * spec.max()))[0]
        cand = sorted(((float(spec[i]), float(self.delay_grid[i])) for i in idx),
                      key=lambda c: -c[0])
        picked: List[Tuple[float, float]] = []
        for s, L in cand:
            if all(abs(L - p[1]) >= d.delay_min_sep_m for p in picked):
                picked.append((s, L))
            if len(picked) >= n_max:
                break
        return picked

    # ---------------- pipeline por posição ----------------
    def estimate_position(self, H: Vec, rx: Vec, R: Vec, tx: Vec, group: int,
                          phase_corr: Optional[Vec] = None,
                          reject_los: bool = True) -> List[PathEstimate]:
        d = self.cfg.dsp
        if phase_corr is not None and len(phase_corr) == H.shape[0]:
            H = H * phase_corr[:, None]
        spec, En = self.aoa_spectrum(H)
        peaks = self.find_aoa_peaks(spec, En)
        if not peaks:
            return []

        # direção LoS (pose conhecida) — usada para separar direto x reflexão
        los_dir = tx - rx
        n = float(np.linalg.norm(los_dir))
        los_len = n
        los_az = los_el = None
        if n > 1e-9:
            u_w = los_dir / n
            los_az, los_el = unit_to_spherical(R.T @ u_w)

        out: List[PathEstimate] = []
        for s_aoa, az, el in peaks:
            if (reject_los and los_az is not None
                    and _ang_sep(az, el, los_az, los_el) < d.los_reject_deg):
                continue
            pspec = self.tof_spectrum(H, az, el)
            for s_tof, L in self.find_tof_peaks(pspec):
                if abs(L - los_len) < d.los_guard_m:
                    continue
                u_local = spherical_unit(np.array([az]), np.array([el]))[0]
                u_world = R @ u_local
                pt = solve_reflection_point(rx, tx, u_world, L)
                if pt is None:
                    continue
                out.append(PathEstimate(point=pt, az_deg=az, el_deg=el,
                                        total_len=L,
                                        strength=float(s_aoa * s_tof),
                                        group=group, rx_pos=rx.copy()))
        return out


def _ang_sep(az1: float, el1: float, az2: float, el2: float) -> float:
    """Separação angular (graus) entre duas direções."""
    u1 = spherical_unit(np.array([az1]), np.array([el1]))[0]
    u2 = spherical_unit(np.array([az2]), np.array([el2]))[0]
    return math.degrees(math.acos(max(-1.0, min(1.0, float(u1 @ u2)))))


def solve_reflection_point(rx: Vec, tx: Vec, u: Vec, total_len: float
                           ) -> Optional[Vec]:
    """
    Resolve |RX + t*u - TX| + t = L (raio x elipsoide de caminho constante).

    Retorna o ponto de reflexão (3,) ou None se não houver solução física.
    """
    u = np.asarray(u, dtype=np.float64)
    nn = float(np.linalg.norm(u))
    if nn < 1e-12:
        return None
    u = u / nn
    d0 = float(np.linalg.norm(rx - tx))
    if total_len <= d0 + 1e-6:
        return None
    tmax = float(total_len)

    def f(t: float) -> float:
        return float(np.linalg.norm(rx + t * u - tx) + t - total_len)

    if f(tmax) < 0.0:
        return None
    try:
        t = brentq(f, 0.0, tmax, xtol=1e-7, rtol=1e-10, maxiter=80)
    except (ValueError, RuntimeError):
        return None
    return rx + t * u


# --------------------------------------------------------------------------
# Orquestrador
# --------------------------------------------------------------------------
ProgressCB = Callable[[str, float, str], None]


class MappingPipeline:
    """
    Executa calibração + varredura + conversão geométrica.

    O callback de progresso recebe (fase, fração 0-1, mensagem).
    """

    def __init__(self, cfg: AppConfig, room: Room,
                 source, on_progress: Optional[ProgressCB] = None,
                 pace: bool = True, cancel: Optional[Callable[[], bool]] = None):
        self.cfg = cfg
        self.room = room
        self.source = source
        self.on_progress = on_progress
        self.pace = pace
        self.cancel = cancel
        self._estimator: Optional[MusicEstimator] = None

    # ------------------------------------------------------------------
    def _emit(self, phase: str, frac: float, msg: str) -> None:
        if self.on_progress is not None:
            self.on_progress(phase, float(np.clip(frac, 0.0, 1.0)), msg)

    def _cancelled(self) -> bool:
        return bool(self.cancel is not None and self.cancel())

    def _pace_until(self, t0: float, target: float) -> None:
        """Garante que a etapa dure ao menos `target` segundos (UI transparente)."""
        if not self.pace:
            return
        while (time.perf_counter() - t0) < target and not self._cancelled():
            time.sleep(0.02)

    # ------------------------------------------------------------------
    def run(self) -> MappingResult:
        t_start = time.perf_counter()
        est = None
        calib: Optional[CalibrationResult] = None
        calib_packets: List[CSIPacket] = []
        paths: List[PathEstimate] = []
        rejected_out = rejected_los = candidates = 0
        groups_seen = 0
        traj_parts: List[Vec] = []
        strengths: List[float] = []
        groups_of_pts: List[int] = []

        t_calib0 = time.perf_counter()
        self._emit("calibracao", 0.0, "Coletando sinal estático do roteador…")
        scan_cfg = self.cfg.scan
        n_scan_guess = max(1, getattr(self.source, "n_groups", 2) - 1)
        current_group = None
        group_buf: List[CSIPacket] = []
        scan_started = False

        def flush_group(buf: List[CSIPacket], gid: int) -> None:
            nonlocal candidates, rejected_out, rejected_los, est
            if not buf or est is None:
                return
            H = np.mean([p.csi for p in buf], axis=0)
            rx = buf[0].rx_pos
            R = buf[0].rx_rot
            tx = np.zeros(3)
            traj_parts.append(rx.copy())
            found = est.estimate_position(H, rx, R, tx, gid,
                                          phase_corr=calib.phase_correction
                                          if calib is not None else None)
            candidates += len(found)
            for pe in found:
                p = pe.point
                if (not self.room.cfg.contains(p)
                        or abs(p[2] - self.room.cfg.floor_z) < 0.02):
                    rejected_out += 1
                    continue
                if self.room.cfg.inside_obstacle(p, tol=0.05):
                    rejected_out += 1
                    continue
                paths.append(pe)
                strengths.append(pe.strength)
                groups_of_pts.append(gid)

        try:
            for pkt in self.source.packets():
                if self._cancelled():
                    break
                if current_group is None:
                    current_group = pkt.group
                if pkt.group != current_group:
                    # fechou um grupo
                    if not scan_started:
                        # grupo 0 = calibração
                        calib_packets.extend(group_buf)
                        calib, est = self._calibrate(calib_packets)
                        self._emit("calibracao", 1.0, calib.describe())
                        self._pace_until(t_calib0, scan_cfg.calib_seconds)
                        scan_started = True
                        self._emit("varredura", 0.0, "Acumulando rebotes de rádio…")
                    else:
                        flush_group(group_buf, current_group)
                        groups_seen += 1
                        frac = groups_seen / n_scan_guess
                        self._emit("varredura", frac,
                                   f"{groups_seen} posições | {len(paths)} pts | "
                                   f"ToF+ AoA (MUSIC)")
                        if self.pace:
                            target = scan_cfg.scan_seconds * frac
                            self._pace_until(t_calib0 + scan_cfg.calib_seconds, 0.0)
                            # pacing suave: dorme frações curtas
                            time.sleep(0.0)
                    group_buf = []
                    current_group = pkt.group
                group_buf.append(pkt)

            # último grupo pendente
            if group_buf and scan_started:
                flush_group(group_buf, current_group)
                groups_seen += 1
        finally:
            if calib is None:
                calib, est = self._calibrate(group_buf)

        self._pace_until(t_calib0, scan_cfg.calib_seconds)
        if not self.pace:
            self._emit("varredura", 1.0, "Varredura concluída.")
        else:
            self._emit("varredura", 1.0, "Varredura concluída.")

        pts = (np.asarray([p.point for p in paths], dtype=np.float64)
               if paths else np.zeros((0, 3)))
        strg = np.asarray(strengths, dtype=np.float64)
        grp = np.asarray(groups_of_pts, dtype=np.int64)
        traj = (np.asarray(traj_parts, dtype=np.float64) if traj_parts
                else np.zeros((0, 3)))

        if len(strg) > 0:
            strg = strg / strg.max()

        stats = dict(
            candidates=float(candidates),
            accepted=float(len(pts)),
            rejected_out_of_bounds=float(rejected_out),
            rejected_direct_path=float(rejected_los),
            groups=float(groups_seen),
            snr_db=float(calib.snr_db if calib else 0.0),
            elapsed_s=float(time.perf_counter() - t_start),
            n_ant=float(self.cfg.array.n_ant),
        )
        self._emit("concluido", 1.0,
                   f"{len(pts)} pontos de reflexão em {stats['elapsed_s']:.2f} s")
        self._estimator = est
        return MappingResult(points=pts, strengths=strg, groups=grp, paths=paths,
                             calib=calib, stats=stats, traj=traj,
                             groups_processed=groups_seen)

    # ------------------------------------------------------------------
    def _calibrate(self, packets: List[CSIPacket]
                   ) -> Tuple[CalibrationResult, MusicEstimator]:
        """Etapa 1: isola o sinal estático direto e calibra as cadeias RF."""
        cfg = self.cfg
        if not packets:
            raise RuntimeError("nenhum pacote de calibração recebido")
        n_ant = packets[0].csi.shape[0]
        est = MusicEstimator(cfg, default_array(cfg, n_ant))
        self._estimator = est

        H_all = np.stack([p.csi for p in packets], axis=0)      # (P, M, K)
        H = H_all.mean(axis=0)                                  # média coerente
        rx = packets[0].rx_pos
        R = packets[0].rx_rot
        tx = np.zeros(3)

        # --- pico dominante (LoS) no espectro AoA ---
        spec, En = est.aoa_spectrum(H)
        peaks = est.find_aoa_peaks(spec, En, n_max=1)
        az, el = (peaks[0][1], peaks[0][2]) if peaks else (0.0, 0.0)
        pspec = est.tof_spectrum(H, az, el)
        tp = est.find_tof_peaks(pspec, n_max=1)
        los_len_est = tp[0][1] if tp else float(np.linalg.norm(rx - tx))

        # --- fase residual por cadeia RF (referenciada à direção estimada) ---
        f = est.freqs
        deembed = np.exp(1j * 2.0 * np.pi * f * (los_len_est / C_LIGHT))
        r = np.angle(H @ deembed / len(f))                       # (M,)
        a0 = est.steering_vec(az, el)
        chain = r - np.angle(a0)
        chain -= chain[0]
        corr = np.exp(-1j * chain)

        # --- SNR por autovalores (subespaço de sinal x subespaço de ruído) ---
        R_cov = est._cov_freq(H, cfg.dsp.subband_win, cfg.dsp.subband_stride)
        w = np.sort(np.maximum(np.linalg.eigvalsh(R_cov).real, 1e-20))[::-1]
        n_sig = est._n_sig(w)
        noise_pow = float(np.mean(w[n_sig:])) if len(w) > n_sig else 1e-20
        sig_pow = max(float(np.mean(w[:n_sig])) - noise_pow, 1e-20)
        snr = 10.0 * math.log10(max(sig_pow / max(noise_pow, 1e-20), 1e-6))

        los_dir = tx - rx
        n = float(np.linalg.norm(los_dir))
        los_az, los_el = unit_to_spherical(R.T @ (los_dir / max(n, 1e-9)))
        calib = CalibrationResult(phase_correction=corr,
                                  los_len=float(np.linalg.norm(los_dir)),
                                  los_az_deg=los_az, los_el_deg=los_el,
                                  snr_db=float(snr), packets_used=len(packets),
                                  spectrum=spec, az_grid=est.az_grid,
                                  el_grid=est.el_grid)
        return calib, est

    # ------------------------------------------------------------------
    @property
    def estimator(self) -> Optional[MusicEstimator]:
        return self._estimator
