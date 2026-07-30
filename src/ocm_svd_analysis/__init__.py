"""OCM 表層流場 SVD 分析套件。

此套件只讀取 OCM-Data-Preprocessing 已驗收的 `ocm_surface` `.npy` 快取；它不含、也
不允許回讀原始 SCHISM NetCDF。第一個可執行產品是貢寮候選 focus bbox 的 2025 年
u/v/eta 三變數表層 SVD pilot，後續區域沿用同一資料契約與輸出結構。
"""

from .batch import run_surface_multivariate_svd_batch
from .surface_multivariate_svd import run_surface_multivariate_svd

__all__ = ["run_surface_multivariate_svd", "run_surface_multivariate_svd_batch"]
