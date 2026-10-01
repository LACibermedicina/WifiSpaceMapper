"""
Fontes de CSI (Channel State Information).

Prioridade de hardware real: UDP  ->  Serial  ->  Mock (demonstração).

Protocolo UDP aceito (JSON, um datagrama por pacote CSI):
    {
      "rx":  [x, y, z],            # posição do receptor (m, frame do roteador)
      "rot": [9 floats row-major], # rotação local->mundo (frente, esquerda, cima)
      "group": 12,                 # id da posição de varredura (opcional)
      "csi": [[re, im], ...]       # n_ant * n_sub pares, ordem antena-maior
    }
Também aceita o formato binário compacto:
    '<3d 9f 2I' = rx(3d), rot(9f), n_ant(u32), n_sub(u32)  seguido de
    n_ant*n_sub pares int16 (escala 1/2048).

Protocolo Serial (linhas CSV, estilo ESP32-CSI):
    t, x, y, z, r11..r33 (9), re0, im0, re1, im1, ...
"""
from __future__ import annotations

import json
import math
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

import numpy as np

from .config import C_LIGHT, AppConfig
from .room import Room


# --------------------------------------------------------------------------
# Pacote CSI
# --------------------------------------------------------------------------
@dataclass
class CSIPacket:
    csi: np.ndarray                 # (n_ant, n_sub) complex128
    rx_pos: np.ndarray              # (3,)
    rx_rot: np.ndarray              # (3,3) — local->mundo (colunas frente/esq/cima)
    t: float = 0.0
    group: int = 0
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.rx_rot.shape != (3, 3):
            raise ValueError("rx_rot deve ser (3,3)")
        if self.csi.ndim != 2:
            raise ValueError("csi deve ser (n_ant, n_sub)")


class CSISource:
    """Interface base de fonte de CSI (stream de pacotes)."""

    name = "base"
    hardware = False

    def packets(self) -> Iterator[CSIPacket]:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return self.name


# --------------------------------------------------------------------------
# 1) Mock: CSI sintético derivado da geometria real do cômodo
# --------------------------------------------------------------------------
class MockCSISource(CSISource):
    """
    Gerador de demonstração.

    NÃO devolve pontos do cômodo: ele **sintetiza o canal MIMO-OFDM** de um
    ambiente 4 x 5 x 2.8 m (roteador no centro) usando traçado de raios de
    primeira ordem (método da imagem), montando a matriz CSI
        H[m,k] = sum_p  a_p * exp(-j*2*pi*f_k*L_p/c) * exp(+j*2*pi*f_k/c*(p_m.u_p))
    com desvios de fase/ganho por cadeia RF, CFO por pacote e ruído AWGN.
    O pipeline de DSP (MUSIC/ToF) recupera depois ToF e AoA "às cegas".
    """

    name = "Simulação (Mock)"
    hardware = False

    def __init__(self, cfg: AppConfig, seed: int = 7):
        self.cfg = cfg
        self.room = Room(cfg.room)
        self.rng = np.random.default_rng(seed)
        self._chain_phase = None
        self._chain_amp = None
        self._prepare_chain()
        self._array_local = self._build_array()
        self._positions, self._rotations = self.room.trajectory(cfg.scan)
        self._calib_pos, self._calib_rot = self.room.calibration_pose(cfg.scan)
        self.stats = {}

    # ---------------- arranjo ----------------
    def _build_array(self) -> np.ndarray:
        ac = self.cfg.array
        lam = C_LIGHT / self.cfg.radio.f0
        d = lam * ac.spacing_fraction
        gx = (np.arange(ac.n_x) - (ac.n_x - 1) / 2.0) * d
        gy = (np.arange(ac.n_y) - (ac.n_y - 1) / 2.0) * d
        pts = [[x, y, 0.0] for x in gx for y in gy]
        for i in range(ac.n_z_extra):
            pts.append([0.0, 0.0, (i + 0.5) * d])
        return np.asarray(pts, dtype=np.float64)

    def _prepare_chain(self) -> None:
        """Descasamento estático entre cadeias RF (fase + amplitude)."""
        r = self.cfg.radio
        n = self.cfg.array.n_ant
        ph = np.deg2rad(r.chain_phase_std_deg) * self.rng.standard_normal(n)
        am = 1.0 + r.chain_amp_ripple * self.rng.standard_normal(n)
        self._chain_phase = ph - ph[0]      # referência relativa à antena 0
        self._chain_amp = am / am.mean()

    # ---------------- física ----------------
    def _ray_paths(self, rx: np.ndarray) -> List[dict]:
        return self.room.trace_first_order(rx)

    def _synth_packet(self, rx: np.ndarray, R: np.ndarray, group: int,
                      t: float) -> CSIPacket:
        r = self.cfg.radio
        n_sub = r.n_sub
        f = r.f0 + r.delta_f * np.arange(n_sub, dtype=np.float64)
        paths = self._ray_paths(rx)
        if not paths:
            paths = [dict(point=rx, length=float(np.linalg.norm(rx)),
                          amplitude=1e-3, label="LoS", los=True)]

        H = np.zeros((self.cfg.array.n_ant, n_sub), dtype=np.complex128)
        amps = np.array([p["amplitude"] for p in paths])
        amps = amps / amps.max()
        for p, a in zip(paths, amps):
            d = p["point"] - rx
            dist = float(np.linalg.norm(d))
            if dist < 1e-9:
                continue
            u_local = R.T @ (d / dist)            # direção de chegada (local)
            diff = self._array_local @ u_local
            phase = 2.0 * np.pi * f[:, None] / C_LIGHT * diff[None, :]   # (K, M)
            taper = max(0.18, math.cos(math.asin(max(-1.0, min(1.0, u_local[2])))))
            sig = (a * taper) * np.exp(-1j * 2.0 * np.pi
                                       * f * (p["length"] / C_LIGHT))     # (K,)
            H += np.exp(1j * phase).T * sig[None, :]

        # --- imperfeições de hardware ---
        cfo = np.deg2rad(r.cfo_deg) * (2.0 * self.rng.random() - 1.0)
        sub_noise = np.deg2rad(r.sub_phase_noise_deg) * self.rng.standard_normal(n_sub)
        H *= np.exp(1j * (self._chain_phase[:, None] + cfo + sub_noise[None, :]))
        H *= self._chain_amp[:, None]

        # --- ruído AWGN na SNR do percurso direto ---
        sig_pow = float(np.mean(np.abs(H) ** 2))
        noise_pow = sig_pow / (10.0 ** (r.snr_db / 10.0))
        H += (self.rng.standard_normal(H.shape)
              + 1j * self.rng.standard_normal(H.shape)) * math.sqrt(noise_pow / 2.0)

        meta = dict(paths=[dict(point=p["point"].copy(), length=p["length"],
                                label=p["label"], los=p["los"]) for p in paths],
                    snr_db=float(r.snr_db))
        return CSIPacket(csi=H, rx_pos=rx.astype(np.float64),
                         rx_rot=R.astype(np.float64), t=t, group=group, meta=meta)

    # ---------------- stream ----------------
    def packets(self) -> Iterator[CSIPacket]:
        sc = self.cfg.scan
        gid = 0
        # etapa 1: pose estática (calibração)
        for _ in range(sc.calib_packets):
            yield self._synth_packet(self._calib_pos, self._calib_rot, gid,
                                     time.time())
        gid += 1
        # etapa 2: varredura
        for i, (p, R) in enumerate(zip(self._positions, self._rotations)):
            for _ in range(sc.packets_per_pos):
                yield self._synth_packet(p, R, gid, time.time())
            gid += 1

    @property
    def n_groups(self) -> int:
        return 1 + self.cfg.scan.n_positions

    def describe(self) -> str:
        c = self.cfg
        return (f"Simulação | sala {c.room.size[0]:.0f}x{c.room.size[1]:.0f}"
                f"x{c.room.size[2]:.1f} m | {c.array.n_ant} antenas | "
                f"{c.radio.n_sub} subportadoras | BW {c.radio.bandwidth/1e6:.0f} MHz")


# --------------------------------------------------------------------------
# 2) UDP (hardware real)
# --------------------------------------------------------------------------
class UDPCSISource(CSISource):
    name = "UDP (hardware)"
    hardware = True

    def __init__(self, cfg: AppConfig, port: Optional[int] = None,
                 timeout: float = 1.0):
        self.cfg = cfg
        self.port = int(port or cfg.udp_port)
        self.timeout = timeout
        self._q: "queue.Queue[CSIPacket]" = queue.Queue(maxsize=4096)
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.settimeout(0.25)
        self._thr = threading.Thread(target=self._loop, daemon=True)
        self._thr.start()
        self._group = 0
        self._last_pos = None

    def _parse(self, data: bytes) -> Optional[CSIPacket]:
        try:
            if data[:1] == b"{":
                obj = json.loads(data.decode("utf-8"))
                csi = np.asarray(obj["csi"], dtype=np.float64)
                csi = csi[:, 0] + 1j * csi[:, 1]
                n_sub = int(obj.get("n_sub", self.cfg.radio.n_sub))
                csi = csi.reshape(int(obj.get("n_ant", len(csi) // n_sub)), n_sub)
                rx = np.asarray(obj["rx"], dtype=np.float64)
                rot = np.asarray(obj.get("rot", np.eye(3).ravel()),
                                 dtype=np.float64).reshape(3, 3)
                return CSIPacket(csi=csi, rx_pos=rx, rx_rot=rot,
                                 t=float(obj.get("t", time.time())),
                                 group=int(obj.get("group", self._group)))
            hdr = struct.calcsize("<3d9f2I")
            rx = np.array(struct.unpack_from("<3d", data, 0))
            rot = np.array(struct.unpack_from("<9f", data, 24)).reshape(3, 3)
            n_ant, n_sub = struct.unpack_from("<2I", data, 24 + 36)
            vals = np.frombuffer(data, dtype="<i2", count=n_ant * n_sub,
                                 offset=hdr).astype(np.float64) / 2048.0
            csi = (vals[0::2] + 1j * vals[1::2]).reshape(n_ant, n_sub)
            return CSIPacket(csi=csi, rx_pos=rx, rx_rot=rot,
                             t=time.time(), group=self._group)
        except Exception:
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            pkt = self._parse(data)
            if pkt is None:
                continue
            # agrupa por posição (mudança > 8 cm inicia nova posição)
            if self._last_pos is None or np.linalg.norm(pkt.rx_pos - self._last_pos) > 0.08:
                self._group += 1
                self._last_pos = pkt.rx_pos
            pkt.group = self._group
            try:
                self._q.put(pkt, timeout=0.5)
            except queue.Full:
                pass

    def packets(self) -> Iterator[CSIPacket]:
        deadline = time.time() + self.timeout
        while not self._stop.is_set():
            try:
                yield self._q.get(timeout=0.2)
                deadline = time.time() + self.timeout
            except queue.Empty:
                if time.time() > deadline:
                    raise TimeoutError(
                        f"nenhum pacote CSI recebido na porta UDP {self.port}")

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thr.join(timeout=1.0)

    def describe(self) -> str:
        return f"UDP :{self.port} (aguardando CSI do hardware)"


# --------------------------------------------------------------------------
# 3) Serial (hardware real, ESP32-CSI)
# --------------------------------------------------------------------------
class SerialCSISource(CSISource):
    name = "Serial (hardware)"
    hardware = True

    def __init__(self, cfg: AppConfig, port: str = "", baud: Optional[int] = None):
        try:
            import serial  # pyserial
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pyserial não instalado (pip install pyserial)") from exc
        if not port:
            raise RuntimeError("porta serial não informada (ex.: /dev/ttyUSB0)")
        self.cfg = cfg
        self._ser = serial.Serial(port, int(baud or cfg.serial_baud), timeout=1.0)
        self._group = 0
        self._last_pos = None

    def packets(self) -> Iterator[CSIPacket]:
        while True:
            line = self._ser.readline()
            if not line:
                raise TimeoutError("timeout na porta serial")
            try:
                vals = [float(x) for x in line.decode("ascii", "ignore").strip().split(",")]
            except ValueError:
                continue
            if len(vals) < 13:
                continue
            t, x, y, z = vals[0:4]
            rot = np.asarray(vals[4:13], dtype=np.float64).reshape(3, 3)
            rest = np.asarray(vals[13:], dtype=np.float64)
            if rest.size % 2 or rest.size == 0:
                continue
            n_sub = self.cfg.radio.n_sub
            n_ant = rest.size // (2 * n_sub)
            csi = (rest[0::2] + 1j * rest[1::2]).reshape(n_ant, n_sub)
            pos = np.array([x, y, z], dtype=np.float64)
            if self._last_pos is None or np.linalg.norm(pos - self._last_pos) > 0.08:
                self._group += 1
                self._last_pos = pos
            yield CSIPacket(csi=csi, rx_pos=pos, rx_rot=rot, t=t, group=self._group)

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass

    def describe(self) -> str:
        return f"Serial {self._ser.portstr} @ {self._ser.baudrate}"


# --------------------------------------------------------------------------
# Fábrica
# --------------------------------------------------------------------------
class FallbackSource(CSISource):
    """Tenta o hardware; se falhar, cai para o modo de demonstração."""

    def __init__(self, cfg: AppConfig, prefer: str = "auto"):
        self.cfg = cfg
        self.prefer = prefer
        self._inner: CSISource = MockCSISource(cfg)
        self.fallback_reason = ""
        if prefer in ("auto", "udp"):
            try:
                self._inner = UDPCSISource(cfg)
                self.hardware = True
            except OSError as exc:
                self.fallback_reason = f"UDP indisponível ({exc})"
        if prefer in ("serial",):
            try:
                self._inner = SerialCSISource(cfg)
                self.hardware = True
            except Exception as exc:
                self.fallback_reason = f"Serial indisponível ({exc})"

    @property
    def inner(self) -> CSISource:
        return self._inner

    def packets(self) -> Iterator[CSIPacket]:
        yield from self._inner.packets()

    @property
    def n_groups(self) -> int:
        return getattr(self._inner, "n_groups", 1)

    def close(self) -> None:
        self._inner.close()

    def describe(self) -> str:
        return self._inner.describe()


def make_source(cfg: AppConfig, prefer: Optional[str] = None) -> CSISource:
    """Cria a fonte de CSI conforme a preferência (auto|mock|udp|serial)."""
    prefer = prefer or cfg.source
    if prefer == "mock":
        return MockCSISource(cfg)
    return FallbackSource(cfg, prefer)
