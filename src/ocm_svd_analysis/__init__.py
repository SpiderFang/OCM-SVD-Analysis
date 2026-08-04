"""OCM 表層與固定物理深度流速—海面高度 SVD 分析套件。

表層產品只讀取已驗收的 ``ocm_surface`` `.npy` 快取；固定深度 family 則成對讀取
``ocm_native`` 的 ``hvel/zcor`` 與 ``ocm_surface`` 的 eta／表層參考。兩種成果共用上游
快取契約，但輸出必須分別位於 ``svd/`` 與 ``fixed_depth_svd/``，不得互相覆寫或混合
解讀。所有公開入口均不回讀原始 SCHISM NetCDF。
"""

from .batch import run_surface_multivariate_svd_batch
from .fixed_depth_batch import run_fixed_depth_multivariate_svd_batch
from .fixed_depth_multivariate_svd import run_fixed_depth_multivariate_svd
from .surface_multivariate_svd import run_surface_multivariate_svd

__all__ = [
    "run_surface_multivariate_svd",
    "run_surface_multivariate_svd_batch",
    "run_fixed_depth_multivariate_svd",
    "run_fixed_depth_multivariate_svd_batch",
]
