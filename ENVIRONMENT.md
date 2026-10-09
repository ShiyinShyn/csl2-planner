# CSL2 Planner 运行环境

## 支持范围与依赖

主要验证平台：Windows x86-64，Python 3.11。Linux/macOS 尚未完成运行验证。
生产依赖在 `environment.yml` 中集中声明，不需要额外 Pip 安装。
`geopandas-base` 提供 `import geopandas`，避免预装绘图组件；
Pyogrio 用于主要矢量读写，Fiona 用于兼容/流式处理。
NumPy、pandas、SciPy 和 NetworkX 提供 DEM、OD、优化与路网算法基础，
并不意味着四阶段交通模型已经实现。

GDAL、GEOS、PROJ 和 BLAS 由 Conda 联合求解。不要混用其他渠道的 GIS 二进制包，
不要用 Pip 覆盖这些包，也不要手工复制 DLL。

## 创建环境

在能运行 `conda` 的终端中进入仓库根目录。渠道配置干净时：

```text
conda env create -f environment.yml
conda activate csl2-planner
python scripts/check_environment.py --synthetic-only
```

如果已经配置多个渠道，创建前在当前会话限定 conda-forge 并启用严格优先级。
PowerShell 示例（不写入全局 `.condarc`，不包含本机安装路径）：

```powershell
$env:CONDA_CHANNELS = 'conda-forge'
$env:CONDA_CHANNEL_PRIORITY = 'strict'
conda env create -f .\environment.yml --no-default-packages --solver libmamba
conda activate csl2-planner
python .\scripts\check_environment.py --synthetic-only
```

Linux/macOS 等 POSIX shell 的对应会话设置为：

```sh
export CONDA_CHANNELS=conda-forge
export CONDA_CHANNEL_PRIORITY=strict
conda env create -f environment.yml --no-default-packages --solver libmamba
```

`nodefaults` 禁止 defaults 回退；会话变量同时排除用户配置的其他渠道。
YAML 中的 `variables` 在激活后延续策略；`PYTHONNOUSERSITE=1` 隔离用户级 Python 包。
如果 `conda` 不可用或激活失败，请先打开安装器提供的 Conda 终端，
确认 `conda info --base` 指向预期发行版，避免混用 Anaconda 和 Miniconda。

## 无需激活的自检

```text
conda run -n csl2-planner --no-capture-output python -s scripts/check_environment.py --synthetic-only
```

预期结果为 `ALL CHECKS PASSED`。合成数据检查不需要本地 Carto 样例或网络。
省略 `--synthetic-only` 时，若本地 `GIS-files-example/` 存在，还会读取可选样例。

## 可复现性

`environment.yml` 是轻量、跨平台的兼容依赖声明，
不精确锁定补丁版本、构建号或传递依赖，也不保证所有平台已经运行验证。
`environment-win-64.explicit.txt` 记录此前 GIS 基线环境的包 URL 与 MD5，不含本次新增的 Qt/PySide6；它不是当前完整 GUI 环境的锁定清单，
只能在匹配平台使用，不能用于 Linux/macOS。

```text
conda create -n csl2-planner-exact --file environment-win-64.explicit.txt
conda run -n csl2-planner-exact --no-capture-output python -s scripts/check_environment.py --synthetic-only
```

显式包清单只锁定包集合，不携带 YAML 中的环境变量；
上面的 `python -s` 可避免用户级 Python 包干扰。日后向该环境安装其他包时，
仍需显式使用 conda-forge/strict 策略。下载还依赖包服务器和对应构建的可用性。

## 验证边界

自检覆盖双矢量引擎、GEOS/PROJ、GeoTIFF/裁剪、DEM 梯度、路网最短路、
优化、配置/CLI 和已安装包渠道。它验证运行环境，不验证城市规划结果或交通模型。
原始数据与外部资料的公开范围见 [DATA_SOURCES.md](DATA_SOURCES.md)。

## Qt/PySide6 依赖检查（不启动 GUI）

environment.yml 已声明 `pyside6>=6.11,<7`，与 GIS 包一并从 conda-forge 求解；
不要另用 Pip 安装或覆盖 Qt/GIS 二进制包。无需激活的导入检查：

```text
conda run -n csl2-planner --no-capture-output python -s -c "import PySide6; from PySide6 import QtCore, QtGui, QtWidgets; print('PySide6', PySide6.__version__, 'Qt', QtCore.qVersion())"
```

2026-10-09 Windows x86-64 环境检查：PySide6 6.11.2、Qt 6.11.2；
QtCore/QtGui/QtWidgets 导入成功，Windows 平台插件 qwindows.dll 存在，
既有 GIS 合成环境自检通过。安装仅新增 21 个包，原有 98 个包的版本、构建号和渠道记录未变。
真实 Carto 样例未运行，脚本中的可选样例 SKIP 不计为通过。

这些结果仅证明依赖可加载及既有合成检查未回退。未创建 QApplication、
未验证原生窗口、字体、地图交互或指南显示，不代表 Q-01.0/Q-02.0 PoC 已通过。
旧显式清单可复现 GIS 基线，但不能单独复现本次 GUI 依赖集合；
Qt/PySide6 的跨平台兼容和完整分发许可仍需对应 Q 检查点验证。
