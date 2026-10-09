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


## Q-01.0-B 合成后台作业

```powershell
conda run -n csl2-planner --no-capture-output python -s scripts/check_q01_jobs.py
```

该命令创建可见原生窗口与独立 spawn 假作业进程，验证进度、未知总量、
取消意图/确认、初始化/执行/清理故障、异常退出、版本过期与窗口关闭清理。
加 `--demo` 可人工操作，关闭窗口会有界取消并回收后台进程。

消费 P 的 JobSpec/JobEvent/ResultManifest、JobLifecycle 和 ResultAcceptance，
不重定义共享类型。成功仅代表合成 fixture 检查通过，不代表交通算法或规划已通过。
IPC 是父进程创建的私有 pipe，不提供网络端口，不接受任意外部 pickle 数据。
结果只保存在内存，不写真实工程，输入资源为 C0 假 fixture，不读取真实 GIS。

Windows 管道正常结束按 EOF 处理；非正常 IPC 故障停止计时器并有界回收，
不递归调用轮询/关闭。进程退出且资源证据有效后，GUI 才调用 C0 接受规则。
首轮失败自测已被受控终止；收口核对发现一个仅含 96 KiB 本项目合成 GeoPackage 的残留目录。2026-10-09 经用户明确授权，重核路径/清单/指纹后已精确删除并验证缺失；这不表示全系统 Temp 已被审计或清理。
本地截图 outputs/q01b/native-jobs.png，报告 .context/q01b-native-report.json 均不公开。
