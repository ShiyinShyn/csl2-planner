> [!IMPORTANT]
> **本项目处于早期开发阶段，暂无可用发布版本。** 如对本项目感兴趣，请您点击 **Star** 或 **Fork**，感谢您的关注。
>
> **This project is in early development, and no usable release is available yet.** If you are interested in this project, please **star** or **fork** the repository. Thank you for your interest and support!

[Read in English ↓](#english)

<a id="chinese"></a>

# CSL2 Planner

面向《Cities: Skylines II》的非官方城市规划辅助工具，帮助玩家结合地图地形、用地与交通需求，制定道路和交通设施规划方案。

以下介绍的是计划提供的功能与使用体验，不代表这些功能已经可用。

## 可以用它规划什么？

| 规划内容 | 为玩家提供的帮助 |
|---|---|
| **用地与功能分区** | 识别陆地和水域，结合地形划分宏观功能区，设置保护范围，并调整分区功能。只规划区域功能，不摆放普通建筑。 |
| **城市路网** | 根据功能分区推定交通需求，规划道路外部连接及公路、主干路、次干路、支路，并为每个区域单独设置街区尺度。 |
| **内部道路与停车** | 规划园区或地块的出入口、内部车行交通组织、配建停车和公共停车设施。 |
| **公共交通** | 规划公交客运站与枢纽、地铁线网、客运火车站及轨道、客运港口及航道，以及铁路和海运外部连接。机场仅作为外部客流节点和地面接驳枢纽。 |
| **公路入城** | 比较过境公路与城市道路的连接位置，评估周边交通影响，并规划立交与匝道。 |

## 预期使用流程

1. **导入地图**：载入通过 Carto 导出的地形和水域数据，按需补充现状道路、建筑或设施图层。
2. **设定规划条件**：指定区域功能、街区尺度、保护边界和拆迁许可等条件。
3. **查看候选方案**：由工具推荐道路、轨道和交通设施方案，比较交通表现与空间冲突。
4. **手动调整并锁定**：修改局部道路、出入口或设施位置，锁定希望保留的内容，再重算受影响部分。
5. **参考或导出成果**：在桌面界面中查看地图、纵断面和局部三维校核结果，并导出专业 GIS 文件供进一步查看或作为游戏建设参考。

软件面向 **Windows 本地单机使用**，采用独立桌面界面，离线优先。不需要把它安装为游戏 Mod，也不会直接修改你的游戏存档。

## 需要哪些地图数据？

- **必选：地形数据**，例如 Carto 导出的高程栅格。
- **必选：水域数据**，可以使用 Carto 导出的水深栅格或坐标与范围明确的水域面。
- **可选：现状信息**，例如道路、轨道、建筑、用地、站点及其他交通设施，用于补充规划背景和避让条件。

计划支持 GeoTIFF 栅格、Shapefile 等 GIS 输入，并提供 GeoPackage、GeoTIFF 等成果导出。没有现状建筑或设施数据时，不能据此认定地图上不存在障碍。

## 你可以控制的规划条件

- **逐区域街区尺度**：不同区域可以采用不同的街区大小、道路方向和布局形式，不必全城使用同一套网格。
- **情景参数**：住宅和包含住宅的商住混合用地支持调整相应情景参数及默认值；其他非住宅用地的情景参数使用固定默认值。
- **硬保护边界**：由你指定不得被规划方案突破的保护范围。
- **拆迁许可**：按区域决定是否允许提出拆迁方案。新道路或轨道与不可拆迁建筑、设施发生冲突时，只允许尝试地下隧道，不能从地表穿过，也不能从上方架桥跨越。若地下方案不可行或校核资料不足，工具应明确提示，而不是自动放宽条件。
- **人工锁定**：保留你已确定的道路和设施；局部重算不会静默覆盖已锁定内容。

## 范围与使用限制

- 专注道路、交通枢纽和配套交通设施，**不安排住宅、商场、工厂、学校、医院等普通建筑的位置**。
- 客运涵盖道路小汽车、公交、地铁、客运铁路和客运海运；货运仅考虑公路道路运输。
- 不规划有轨电车、出租车、步行或骑行接驳网络、内部航空网络，也不考虑铁路、水运和航空货运。
- 机场只参与宏观客流与地面接驳，不规划跑道、航线或航站楼内部交通。
- 交通评价基于理论规划模型，不读取游戏动态遥测，也不保证采用方案后一定消除拥堵。
- 空间校核为游戏规划参考，不替代现实工程设计或安全认证。资料缺失时，结果应标明待核实或资料不足。

## 交通指南

想先了解道路分级、路网布局和游戏中的交通组织，可以阅读作者的 [天际线交通指南](https://shiyinshyn.feishu.cn/docx/JbjMddW1soS3eTxlxIdcmpSIn3g)。

软件计划提供内置离线指南查看器，支持目录导航、搜索和图片浏览，便于规划时随时查阅。

## 反馈与关注

欢迎通过 GitHub Issues 提交使用需求、问题或建议。若希望关注后续可用版本，可以点击 **Star**。

## 许可证与作者

Copyright (C) 2026 ShiyinShyn.

原创代码与项目文档采用 **GNU GPL version 3 only**（`GPL-3.0-only`），全文见 [LICENSE](LICENSE)。第三方依赖和参考资料保留各自的许可。软件及规划结果不提供适用性或准确性保证。

---

<a id="english"></a>

# CSL2 Planner — English

[返回中文 / Back to Chinese ↑](#chinese)

An unofficial city-planning assistant for **Cities: Skylines II**, designed to help players plan roads and transport facilities using terrain, land use, and estimated travel demand.

> [!IMPORTANT]
> **No downloadable, usable version is available yet.** The sections below describe planned features and the intended experience, not features you can use today. If you are interested, **star** the repository to follow the project.

## What can you plan?

| Planning area | How it helps players |
|---|---|
| **Land use and functional zones** | Identify land and water, define broad functional zones based on terrain, set protected areas, and adjust zone functions. This does not place individual non-transport buildings. |
| **City road networks** | Estimate travel demand from functional zones, plan outside road connections and a hierarchy of highways, arterials, collectors, and local streets, and set block sizes independently for each zone. |
| **Internal roads and parking** | Plan site entrances, internal vehicle circulation, on-site parking, and public parking facilities. |
| **Public transport** | Plan bus terminals and hubs, metro networks, passenger railway stations and tracks, passenger ports and waterways, and outside rail and shipping connections. Airports serve only as external passenger-demand nodes and ground-transfer hubs. |
| **Highway–city connections** | Compare connections between through highways and city roads, assess nearby traffic impacts, and plan interchanges and ramps. |

## Intended workflow

1. **Import your map**: Load terrain and water data exported through Carto, with optional layers for existing roads, buildings, or facilities.
2. **Set your planning conditions**: Define zone functions, block sizes, protected boundaries, and demolition permissions.
3. **Review candidate plans**: Compare recommended roads, tracks, and transport facilities, including traffic estimates and spatial conflicts.
4. **Adjust and lock**: Edit roads, entrances, or facility locations, lock the elements you want to keep, and recalculate affected parts.
5. **Review or export**: View maps, longitudinal profiles, and local 3D checks in the desktop interface, and export professional GIS files for further inspection or as an in-game construction reference.

The application is intended for **local, single-user use on Windows**, with a standalone desktop interface and an offline-first experience. It is not a game mod and does not directly modify your save files.

## What map data do you need?

- **Required: terrain data**, such as an elevation raster exported by Carto.
- **Required: water data**, such as a Carto water-depth raster or water polygons with clearly defined coordinates and coverage.
- **Optional: existing-city information**, such as roads, tracks, buildings, land use, stops, and other transport facilities, to add context and avoidance constraints.

Planned GIS support includes GeoTIFF rasters and Shapefile inputs, with outputs such as GeoPackage and GeoTIFF. Missing building or facility data does not prove that an area is free of obstacles.

## Planning conditions you control

- **Block size by zone**: Use different block sizes, street directions, and layouts in different areas instead of one citywide grid.
- **Scenario parameters**: Adjust applicable scenario parameters and defaults for residential and residential–commercial mixed-use zones. Other non-residential zones use fixed defaults.
- **Protected boundaries**: Define areas that candidate plans must not violate.
- **Demolition permissions**: Decide by zone whether demolition may be proposed. If a new road or track conflicts with a non-demolishable building or facility, only an underground tunnel may be attempted—neither surface passage nor an overhead bridge is allowed. An infeasible tunnel or insufficient information must be reported rather than silently relaxing the restriction.
- **Manual locks**: Preserve roads and facilities you have already decided on. Recalculation must not silently overwrite locked elements.

## Scope and limitations

- Focuses on roads, transport hubs, and supporting transport facilities. **It does not place homes, shops, factories, schools, hospitals, or other ordinary non-transport buildings.**
- Passenger transport covers private cars, buses, metro, passenger rail, and passenger shipping. Freight is limited to road transport.
- Tram, taxi, walking or cycling access networks, internal aviation networks, and rail, waterborne, or air freight are outside the scope.
- Airports participate only in regional passenger demand and ground transfers; runways, flight routes, and terminal-internal circulation are not planned.
- Traffic estimates use theoretical planning models, not live game telemetry, and do not guarantee that a proposed plan will eliminate congestion.
- Spatial checks are for game-planning reference, not real-world engineering design or safety certification. Missing information must be identified as requiring verification or further data.

## Traffic guide

For an introduction to road hierarchy, network layouts, and traffic organization in the game, see the author's [Cities: Skylines Traffic Guide](https://shiyinshyn.feishu.cn/docx/JbjMddW1soS3eTxlxIdcmpSIn3g) (in Chinese).

An integrated offline guide viewer is planned, with a table of contents, search, and image browsing for quick reference while planning.

## Feedback and updates

Use GitHub Issues to share user needs, report problems, or suggest improvements. **Star** the repository if you would like to follow future usable releases.

## License and author

Copyright (C) 2026 ShiyinShyn.

Original code and project documentation are licensed under **GNU GPL version 3 only** (`GPL-3.0-only`); see [LICENSE](LICENSE). Third-party dependencies and reference materials retain their respective licenses. The software and planning results are provided without a warranty of suitability or accuracy.
