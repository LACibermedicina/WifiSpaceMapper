"""
Mapeador 3D de Espaço por CSI Wi-Fi
====================================

Software standalone que reconstrói a geometria do cômodo em volta de um
roteador Wi-Fi a partir de Channel State Information (CSI), usando
ToF (Time of Flight) + AoA (Angle of Arrival) 2D por MUSIC, agrupamento
DBSCAN e reconstrução de superfície por Poisson / Alpha Shapes.

Pacote: csi_mapper
"""
from .config import (  # noqa: F401
    C_LIGHT, RoomConfig, Box, ArrayConfig, RadioConfig,
    ScanConfig, DspConfig, ReconConfig, AppConfig, default_config,
)

__version__ = "1.0.0"
__all__ = [
    "C_LIGHT", "RoomConfig", "Box", "ArrayConfig", "RadioConfig",
    "ScanConfig", "DspConfig", "ReconConfig", "AppConfig", "default_config",
    "__version__",
]
