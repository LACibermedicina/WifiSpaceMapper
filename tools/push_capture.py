"""
Envia uma captura do software desktop (hardware real ou simulacao) para o portal.

Exemplos
--------
# 1) simulacao rapida desta maquina -> projeto 3 do portal
python tools/push_capture.py --server http://servidor:8080 --email eu@dominio.com \
       --password 'SenhaForte123' --project 3 --source mock --label "Sala 2 - manha"

# 2) CSI real por UDP (ESP32 / Nexmon transmitindo na porta 5566)
python tools/push_capture.py --server http://servidor:8080 --email eu@dominio.com \
       --password 'SenhaForte123' --project 3 --source udp --udp-port 5566 --seconds 12

# 3) CSI real por porta serial
python tools/push_capture.py --server http://servidor:8080 --email eu@dominio.com \
       --password 'SenhaForte123' --project 3 --source serial --serial-port /dev/ttyUSB0

Se --project nao for informado, um projeto novo e criado automaticamente
(uma captura = um projeto novo, conforme o fluxo padrao do portal).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from csi_mapper.config import default_config            # noqa: E402
from csi_mapper.csi_source import (MockCSISource, SerialCSISource,  # noqa: E402
                                  UDPCSISource)
from csi_mapper.dsp import MappingPipeline              # noqa: E402
from csi_mapper.reconstruction import O3D_OK, reconstruct  # noqa: E402
from csi_mapper.room import Room                        # noqa: E402


def http(method: str, url: str, payload=None, token: str = ""):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                headers={"Content-Type": "application/json",
                                         **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=900) as rsp:
        body = rsp.read().decode()
        return json.loads(body) if body else {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Envia captura CSI ao portal web")
    ap.add_argument("--server", required=True, help="ex.: http://192.168.0.10:8080")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--project", type=int, default=0, help="0 = cria projeto novo")
    ap.add_argument("--name", default="", help="nome do projeto novo")
    ap.add_argument("--label", default="", help="rotulo da captura")
    ap.add_argument("--source", default="mock", choices=["mock", "udp", "serial"])
    ap.add_argument("--udp-port", type=int, default=5566)
    ap.add_argument("--serial-port", default="")
    ap.add_argument("--seconds", type=float, default=12.0,
                    help="duracao da varredura com hardware real")
    ap.add_argument("--room", default="4,5,2.8", help="largura,profundidade,altura")
    ap.add_argument("--voxel", type=float, default=0.12)
    args = ap.parse_args()

    srv = args.server.rstrip("/")
    auth = http("POST", f"{srv}/api/auth/login",
                {"email": args.email, "password": args.password})
    token = auth.get("token", "")
    print(f"[ok] autenticado como {auth['user']['email']}")

    sx, sy, sz = (float(v) for v in args.room.split(","))
    if args.project:
        pid = args.project
    else:
        pj = http("POST", f"{srv}/api/projects",
                  {"name": args.name or f"Captura {time.strftime('%Y-%m-%d %H:%M')}",
                   "room_x": sx, "room_y": sy, "room_z": sz,
                   "source_mode": args.source, "voxel": args.voxel}, token)
        pid = pj["project"]["id"]
        print(f"[ok] projeto novo #{pid}: {pj['project']['name']}")

    cfg = default_config()
    cfg.room.size = (sx, sy, sz)
    cfg.recon.voxel = args.voxel
    room = Room(cfg.room)
    if args.source == "udp":
        src = UDPCSISource(cfg, port=args.udp_port, timeout=args.seconds)
    elif args.source == "serial":
        src = SerialCSISource(cfg, port=args.serial_port)
    else:
        src = MockCSISource(cfg)

    print(f"[..] executando pipeline ({args.source}"
          f"{f', {args.seconds:g}s' if args.source != 'mock' else ''})")
    pipe = MappingPipeline(cfg, room, src, on_progress=None, pace=False,
                           cancel=lambda: False)
    res = pipe.run()
    pts = np.asarray(res.points, dtype=float).reshape(-1, 3)
    st = np.asarray(res.strengths, dtype=float).reshape(-1)
    if len(st) != len(pts):
        st = np.ones(len(pts))
    mesh = {}
    if O3D_OK:
        rec = reconstruct(pts, st, cfg, room)
        if getattr(rec, "has_mesh", False):
            mesh = {"vertices": np.asarray(rec.vertices).round(4).tolist(),
                    "triangles": np.asarray(rec.triangles, dtype=int).tolist()}
    print(f"[..] {len(pts)} reflexoes CSI, {len(mesh.get('triangles', []))} triangulos")

    payload = {"label": args.label or f"Captura {args.source} {time.strftime('%H:%M:%S')}",
               "voxel": args.voxel, "metric": "csi",
               "points": np.hstack([pts, st.reshape(-1, 1)]).round(4).tolist(),
               "mesh": mesh,
               "calib": {"snr_db": float(getattr(res.calib, "snr_db", 0.0)),
                         "los_len_m": float(getattr(res.calib, "los_len", 0.0))},
               "metrics": {k: float(v) for k, v in (getattr(res, "stats", {}) or {}).items()},
               "method": "csi_mapper desk" + (" + poisson" if mesh else "")}
    out = http("POST", f"{srv}/api/projects/{pid}/captures/upload", payload, token)
    print(f"[ok] captura #{out['capture_id']} salva no projeto {pid}")
    print(f"[ok] abra {srv}/#/projeto/{pid} para medir e exportar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
