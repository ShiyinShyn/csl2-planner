# Q-01.0-A：原生窗体与合成地图交互 PoC

本目录仅消费 P 的 C0 MapLayer/SpaceRef/ResourceRef，不修改共享契约。
窗口是原生 Qt/PySide6，地图是只读 QGraphicsView；不包含算法、后台作业、指南或真实 GIS 数据。

## 单命令验证

```powershell
conda run -n csl2-planner --no-capture-output python -s scripts/check_q01_shell.py
```

运行时生成临时合成 GeoPackage（含中文和空格路径），展示可见 Windows 窗口，
自动验证缩放、平移、选中、字体与资源边界后关闭并清理临时资源。
必须使用 windows 平台且窗口实际 exposed；offscreen/minimal 不算原生 GUI 通过。
失败退出非零，无跳过冒充通过。截图保存在 outputs/q01a/native-window.png，
报告保存在 .context/q01a-native-report.json，二者仅本地且不进入公开仓库。

人工操作使用同一命令加 `--demo`，关闭窗口即退出。

## 边界

只支持本 PoC 所需的显式同 CRS、有限简单二维多边形 GeoPackage；
越界路径、字节大小/哈希不符、CRS 不符、缺字段、重复 ID、非多边形和带洞多边形拒绝。
不猜 CRS、不转换单位、不移动或修改要素，不写回真实数据。
重解析目标用模拟覆盖，未验证真实 Windows 重解析点。
Q-01.0 的后台作业/取消/失败/旧结果门禁待后续检查点；本检查点不是完整 Q-01.0 或 G0 验收。
