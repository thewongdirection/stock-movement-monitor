from .base import Context, Detector  # noqa: F401
from .block_trades import BlockTradeDetector  # noqa: F401
from .dark_pool import DarkPoolDetector  # noqa: F401
from .insider_trades import InsiderTradeDetector  # noqa: F401
from .options_flow import OptionsFlowDetector  # noqa: F401
from .volume_anomaly import VolumeAnomalyDetector  # noqa: F401

ALL_DETECTORS = (
    VolumeAnomalyDetector,
    BlockTradeDetector,
    DarkPoolDetector,
    OptionsFlowDetector,
    InsiderTradeDetector,
)
