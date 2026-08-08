"""OCM 表層與完整水柱流速—海面高度 SVD 分析套件。

表層產品只讀取已驗收的 ``ocm_surface`` `.npy` 快取；完整水柱聯合 SVD 則成對讀取
``ocm_native`` 的 ``hvel/zcor`` 與 ``ocm_surface`` 的表層 ``u/v/eta``。完整水柱產品
把表層、10、20、30、40、50 m 的 ``u/v`` 及唯一 ``eta`` 放入同一個直接 SVD，輸出必須
位於獨立的 ``water_column_svd/`` 命名空間，不能與 ``svd/`` 表層成果互相覆寫或混合
解讀。所有公開入口均不回讀原始 SCHISM NetCDF。
"""

from .batch import run_surface_multivariate_svd_batch
from .surface_multivariate_svd import run_surface_multivariate_svd
from .water_column_multivariate_svd import run_water_column_multivariate_svd
from .water_column_host_layout import export_water_column_host_layout
from .water_column_replot import replot_water_column_multivariate_svd

__all__ = [
    "run_surface_multivariate_svd",
    "run_surface_multivariate_svd_batch",
    "run_water_column_multivariate_svd",
    "export_water_column_host_layout",
    "replot_water_column_multivariate_svd",
]
