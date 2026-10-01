"""个人 AI ETF 资产配置助手 V2（纯规则、无自动下单）。"""

from .engine import allocate_new_cash, scan_candidates

__all__ = ["allocate_new_cash", "scan_candidates"]
