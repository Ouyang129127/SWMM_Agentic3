# 模型目录

本版本不附带具体模型、降雨或历史运行结果。将模型目录放在此处后再进行完整模拟验证。
每个模型的标准布局：

| 相对模型目录的路径 | 内容 |
|---|---|
| `model.yaml` | 模型元数据 |
| `simulation_timing.json` | 模型自身的保存间隔、二维边界间隔和计算时长配置 |
| `raw/` | 原始资料目录；目录检查需要它存在 |
| `swmm/scenarios/<scenario>/model.inp` | 对应情景的 SWMM 输入，默认情景为 `baseline` |
| `static/config.json` | 地表网格配置 |
| `static/elevation.npy`、`smid_grid.npy` | 地形与网格编号 |
| `static/flow_mask.npy`、`building_mask.npy`、`resistance.npy` | 地表流动、建筑及阻力数据 |
| `static/node_to_cell_mapping.csv` | SWMM 节点到地表网格的映射 |
| `mapping/` | 模型映射及解释口径等辅助资源 |
| `events/` | 本模型的降雨事件文件 |
| `runs/` | 每次运行的输出位置 |

优先放入一个已经准备完整的模型目录，保持其内部结构。`scenario/validation.py` 检查目录和必要文件；实际模拟还会校验降雨与时间设置。
也可以通过 SWMM_MODELS_DIR 指定外部模型目录；修改环境变量后重新启动程序。
