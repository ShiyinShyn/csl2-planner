> [!IMPORTANT]
> **本项目处于早期开发阶段，暂无可用发布版本。** 如对本项目感兴趣，请您点击 **Star** 或 **Fork**，感谢您的关注。
>
> **This project is in early development, and no usable release is available yet.** If you are interested in this project, please **star** or **fork** the repository. Thank you for your interest and support!

# CSL2 Planner

面向《Cities: Skylines II》的 GIS 城市规划辅助工具研发项目。
计划处理 Carto 导出的地形/水深栅格与地块、道路等矢量数据，支持路网拓扑分析、
几何放坡计算及四阶段交通模型相关计算。

**状态：研发规划与底层环境初始化阶段。** 当前仓库提供技术路线、Conda 环境和自检脚本，
不声称已经实现完整规划功能。项目为非官方工具，不隶属于游戏或 Carto 的开发方。

## 快速开始

在已经可以执行 `conda` 的终端中，进入本仓库根目录：

```text
conda env create -f environment.yml
conda activate csl2-planner
python scripts/check_environment.py --synthetic-only
```

预期最后输出 `ALL CHECKS PASSED`。测试自动生成轻量几何、栅格和路网数据，
**不需要原始 Carto 样例、网络下载或游戏存档**。

如果本机 Conda 配有其他渠道，请按照 [环境说明](ENVIRONMENT.md) 设置会话级
conda-forge / strict 策略后创建环境；不要用 Pip 覆盖 GIS 二进制依赖。
主要验证平台为 **Windows x86-64 / Python 3.11**；其他平台尚未完成运行验证。

## 仓库内容

```text
README.md                          项目首页
LICENSE                            GPL-3.0 完整许可证
DATA_SOURCES.md                     数据范围、外部资料与公开边界
ENVIRONMENT.md                     安装、渠道策略和可复现性说明
environment.yml                    跨平台依赖声明
environment-win-64.explicit.txt     Windows x86-64 精确包清单
scripts/check_environment.py       自包含环境自检
docs/planning/                     研发计划与已有样例结构/统计审计
```

原始 `GIS-files-example/`、本地交通指南及图片附件、代理工作规则、日志与缓存
不随本仓库公开；本地保留这些文件不影响合成数据自检。
详细范围见 [数据与资料说明](DATA_SOURCES.md)。

## 交通指南

作者的交通指南通过外部文档提供，不在本仓库复制正文或图片：

[天际线交通指南（飞书）](https://shiyinshyn.feishu.cn/docx/JbjMddW1soS3eTxlxIdcmpSIn3g)

外部链接的访问权限由文档所有者管理，不属于本仓库的离线可复现范围。

## 技术路线与验证边界

- [当前需求基线与 D 系列决策规范](docs/planning/需求基线与D系列决策规范-R1.md)
- [软件研发实施计划与技术路线书（历史方法参考）](docs/planning/软件研发实施计划与技术路线书.md)
- [现有本地样例的结构与统计审计](docs/planning/carto-sample-data-audit.json)

环境自检覆盖 GIS 读写、空间几何/投影、DEM 运算、图论、优化及配置/CLI 基础能力；
它不验证规划模型正确性、交通校准质量或大型路网性能。
环境 YAML 允许范围内版本重新求解；同平台精确复现使用 Windows 显式包清单。

## 许可证与作者

Copyright (C) 2026 ShiyinShyn.

本仓库作者提供的原创代码和原创项目文档采用 **GNU GPL version 3 only**
（SPDX：`GPL-3.0-only`），全文见 [LICENSE](LICENSE)。
不提供适用性或结果准确性的保证。第三方依赖、引用资料、外部指南及未公开数据
保留各自的权利与许可，不因本仓库的许可证而获得额外再分发授权。

项目公开后，请通过 GitHub Issues 提交问题；不提供私人邮箱。
