"""
Parâmetros de configuração do sistema (geometria, rádio, DSP e reconstrução).

Convenção de coordenadas (frame do roteador):
    A ORIGEM (0,0,0) é o roteador Wi-Fi, colocado no centro do cômodo.
    X = largura do cômodo (metros)
    Y = profundidade do cômodo (metros)
    Z = altura (metros); piso em z = -router_height, teto em
        z = +size_z - router_height.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

# Velocidade da luz (m/s)
C_LIGHT: float = 299_792_458.0


@dataclass
class Box:
    """Obstáculo paralelepípedo (móvel, pilar, sofá...), alinhado aos eixos."""
    center: Tuple[float, float, float]
    half: Tuple[float, float, float]
    reflectivity: float = 0.42
    label: str = "obstáculo"

    @property
    def lo(self) -> Tuple[float, float, float]:
        return tuple(c - h for c, h in zip(self.center, self.half))  # type: ignore

    @property
    def hi(self) -> Tuple[float, float, float]:
        return tuple(c + h for c, h in zip(self.center, self.half))  # type: ignore


@dataclass
class RoomConfig:
    """Cômodo de demonstração: 4 m x 5 m x 2.8 m, roteador no centro."""
    size: Tuple[float, float, float] = (4.0, 5.0, 2.8)
    router_height: float = 1.4          # altura do roteador acima do piso
    wall_reflectivity: float = 0.62     # alvenaria / gesso
    floor_reflectivity: float = 0.40    # piso (absorve mais)
    ceiling_reflectivity: float = 0.34  # laje (absorve mais)
    obstacles: List[Box] = field(default_factory=lambda: [
        Box(center=(1.30, -1.15, 0.75 - 1.4), half=(0.45, 0.30, 0.75),
            reflectivity=0.45, label="armário"),
        Box(center=(-1.35, 1.10, 0.50 - 1.4), half=(0.35, 1.05, 0.50),
            reflectivity=0.40, label="sofá"),
        Box(center=(-1.40, -1.60, 0.95 - 1.4), half=(0.30, 0.30, 0.95),
            reflectivity=0.55, label="pilar"),
    ])

    # ---------------- limites derivados ----------------
    @property
    def xlim(self) -> Tuple[float, float]:
        return (-self.size[0] / 2.0, self.size[0] / 2.0)

    @property
    def ylim(self) -> Tuple[float, float]:
        return (-self.size[1] / 2.0, self.size[1] / 2.0)

    @property
    def zlim(self) -> Tuple[float, float]:
        return (-self.router_height, self.size[2] - self.router_height)

    @property
    def floor_z(self) -> float:
        return -self.router_height

    def contains(self, p) -> bool:
        x0, x1 = self.xlim
        y0, y1 = self.ylim
        z0, z1 = self.zlim
        return (x0 <= p[0] <= x1 and y0 <= p[1] <= y1 and z0 <= p[2] <= z1)

    def inside_obstacle(self, p, tol: float = 0.0) -> bool:
        for b in self.obstacles:
            lo, hi = b.lo, b.hi
            if all(lo[i] - tol <= p[i] <= hi[i] + tol for i in range(3)):
                return True
        return False


@dataclass
class ArrayConfig:
    """Arranjo de antenas do receptor CSI (Uniform Rectangular Array + Z)."""
    n_x: int = 4
    n_y: int = 4
    n_z_extra: int = 2          # elementos extras em +Z -> resolve o sinal da elevação
    spacing_fraction: float = 0.5   # espaçamento em unidades de lambda (lambda/2)

    @property
    def n_ant(self) -> int:
        return self.n_x * self.n_y + self.n_z_extra


@dataclass
class RadioConfig:
    """Camada física do enlace CSI."""
    f0: float = 2.412e9          # canal 1 (2.4 GHz)
    bandwidth: float = 160e6     # 802.11ax 160 MHz (largura de banda de sounding)
    n_sub: int = 128             # subportadoras CSI aproveitáveis
    snr_db: float = 30.0         # SNR do percurso direto (LoS)
    chain_phase_std_deg: float = 28.0   # descasamento de fase entre cadeias RF
    chain_amp_ripple: float = 0.30      # ripple de amplitude entre cadeias
    cfo_deg: float = 180.0              # desvio de fase comum por pacote (uniforme)
    sub_phase_noise_deg: float = 3.0    # ruído de fase por subportadora

    @property
    def delta_f(self) -> float:
        return self.bandwidth / self.n_sub


@dataclass
class ScanConfig:
    """
    Protocolo de varredura.

    O receptor CSI (agente móvel) percorre uma trajetória elíptica de
    posições conhecidas dentro do cômodo enquanto o roteador transmite.
    A pose (posição + rotação) de cada rajada é conhecida/estimada por
    odometria — é o mesmo pressuposto de qualquer sistema de mapeamento
    Wi-Fi por CSI (CSI-SLAM, Wi-Fi imaging).
    """
    calib_packets: int = 28          # pacotes estáticos para calibração (0-3 s)
    packets_per_pos: int = 8         # snapshots temporais por posição
    points_per_lap: int = 60         # posições por volta
    lap_heights: Tuple[float, ...] = (0.55, -0.15, -0.85)
    ellipse_a: float = 1.28          # semi-eixo X da trajetória (m)
    ellipse_b: float = 1.62          # semi-eixo Y da trajetória (m)
    calib_pos: Tuple[float, float, float] = (0.90, -1.10, 0.0)
    calib_heading_deg: float = -125.0

    # durações-alvo das etapas exibidas na interface
    calib_seconds: float = 3.0
    scan_seconds: float = 7.0

    @property
    def n_positions(self) -> int:
        return len(self.lap_heights) * self.points_per_lap

    @property
    def packets_per_pos_total(self) -> int:
        return self.packets_per_pos


@dataclass
class DspConfig:
    """Parâmetros de estimação de percurso (MUSIC / ToF)."""
    # --- estágio AoA (2D MUSIC sobre (azimute, elevação)) ---
    az_step_coarse: float = 2.5
    el_step_coarse: float = 3.0
    refine_step: float = 0.6
    subband_win: int = 40            # subportadoras por janela (smoothing em frequência)
    subband_stride: int = 8
    max_paths: int = 3               # máx. de percursos (AoA) por posição
    peak_rel_threshold: float = 0.09  # limiar relativo ao pico do espectro
    min_sep_deg: float = 8.0         # separação angular mínima entre picos
    n_sig_ratio: float = 0.08        # limiar (relativo ao maior autovalor) p/ nº de fontes

    # --- estágio ToF (1D MUSIC sobre as subportadoras decimadas) ---
    delay_decim: int = 2             # decimação de subportadoras
    delay_win: int = 40
    delay_stride: int = 4
    delay_step_m: float = 0.02       # passo da grade de caminho total (m)
    delay_max_m: float = 15.0        # caminho total máximo (m)
    los_guard_m: float = 0.30        # rejeita o percurso direto (LoS)
    los_reject_deg: float = 7.0      # rejeita picos de AoA na direção LoS
    delay_peaks: int = 3             # máx. de percursos por AoA
    delay_rel_threshold: float = 0.08
    delay_min_sep_m: float = 0.35    # separação mínima entre picos de atraso (m)


@dataclass
class ReconConfig:
    """Parâmetros de filtragem e reconstrução de superfície (Open3D)."""
    voxel: float = 0.055             # downsampling (m)
    sor_k: int = 14                  # Statistical Outlier Removal
    sor_std: float = 1.7
    dbscan_eps: float = 0.42
    dbscan_min_samples: int = 5
    min_cluster_pts: int = 12
    normal_k: int = 30
    poisson_depth: int = 8
    poisson_scale: float = 1.12      # escala relativa ao tamanho da bounding box
    density_quantile: float = 0.045
    alpha: float = 0.38              # Alpha Shapes (fallback)
    decimate_target: int = 45000     # triângulos para exibição interativa


@dataclass
class AppConfig:
    room: RoomConfig = field(default_factory=RoomConfig)
    array: ArrayConfig = field(default_factory=ArrayConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    dsp: DspConfig = field(default_factory=DspConfig)
    recon: ReconConfig = field(default_factory=ReconConfig)
    source: str = "auto"             # auto | mock | udp | serial
    udp_port: int = 5566
    serial_port: str = ""
    serial_baud: int = 921600


def default_config() -> AppConfig:
    return AppConfig()
