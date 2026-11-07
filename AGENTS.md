# AzurLaneLevelTrace 项目约定

本文件只补充本仓库的路径约定、数据隔离和清空语义；通用规则以全局 `AGENTS.md` 为准。

## 配置与数据库位置

- **正式配置**：`DATA_DIR/config.json`，默认 `/Users/liuqi/Workspace/akise/AzurLaneLevelTrace/data/config.json`，保存 `next_ship_types`（推荐舰种）、`target_limits`（每舰种挂机/练级目标数量上限，1–10）、`manual_targets`（手动录入舰娘 ID 列表，不超过对应上限）、`armament_target_limits`（联动/常规兵装打造与强化目标数量上限，1–10）和 `armament_manual_targets`（手动录入兵装 ID 列表）。调整任一配置字段时必须带全其余字段一并 `write_config`，否则会被规范化丢弃。
- **正式数据库**：`DATA_DIR/blhx-training.sqlite3`，默认 `/Users/liuqi/Workspace/akise/AzurLaneLevelTrace/data/blhx-training.sqlite3`，包含 `ships`、`tech_points`、`armaments`、`operation_logs`。
- **路径来源**：`blhx/core/constants.py` 中 `DATA_DIR`、`DATABASE_PATH`、`CONFIG_PATH`；`DATA_DIR` 可由环境变量覆盖，`DATABASE_PATH` 与 `CONFIG_PATH` 均由 `DATA_DIR` 派生，不单独读环境变量。根级 `blhx/constants.py` 只是兼容转发层。
- **测试配置与测试数据库**：一律使用进程内临时目录，禁止指向正式 `data/`。测试必须通过 `tests/support.py` 的 `isolated_app_data()` 上下文管理器，它会在临时目录内重建 `blhx-training.sqlite3` 和 `config.json`，并在退出时恢复 `app.db.DATA_DIR`、`app.db.DATABASE_PATH`、`app.config.CONFIG_PATH`。
- **禁止事项**：测试代码不得直接 `sqlite3.connect` 正式库，不得写 `data/`、不得复用正式 `config.json`，不得依赖正式库中的既有数据。
- **本地手工验证**：需要试跑真实数据时先复制 `data/` 到临时目录，再用 `DATA_DIR=<临时目录> python app.py` 启动；不要直接对正式库做结构试验。

## 目录结构

- `blhx/core/`：常量与路径、进程内状态、通用工具。
- `blhx/storage/`：SQLite 连接与表结构、配置文件、操作历史。
- `blhx/domain/`：舰船、科技点、兵装、推荐等业务逻辑。
- `blhx/datasource/`：Wiki 抓取与解析。
- `blhx/transfer/`：CSV 导入导出与后台导入任务。
- `blhx/web/`：HTTP 服务、页面与静态资源（`static/icon.png` 以 `/icon.png` 提供）。
- 根级 `blhx/*.py` 为兼容转发层，新增代码应写入对应子包，不要继续加在根级模块。

## “清空数据”功能范围

`clear_ships()`（`POST /api/ships/clear`，页面“清空数据”）必须清空以下全部业务数据：

- `ships` 表：全部舰船。
- `tech_points` 表：全部加成舰种、加成属性记录（含外键级联外的显式删除）。
- `armaments` 表：全部兵装数据（含外键级联外的显式删除）。
- `operation_logs` 表：全部操作历史。
- `sqlite_sequence` 表：重置 `ships`、`tech_points`、`armaments`、`operation_logs` 的自增序列。
- `DATA_DIR/config.json`：删除推荐舰种、目标数量上限、手动录入、兵装目标数量上限和兵装手动录入配置，使 `read_config()` 回到空配置。
- 进程内 `SYNC_STATE`：重置为未开始状态。
- 同步进行中（`SYNC_STATE["running"]` 为真）必须拒绝清空并返回错误；清空与同步启动必须共用 `SYNC_LOCK`，避免检查后被并发同步插入。
- 任一清空步骤失败（如删除 `config.json` 报错）必须整体报错，不得在数据已部分清空时返回成功。

不要在“清空数据”中保留操作历史或推荐配置；新增持久化表或配置文件时，必须同步纳入清空范围并补测试。

## 舰种与筛选项

- 风帆M、风帆S、风帆V 统一归一为 `风帆`，见 `SHIP_TYPE_ALIASES`；Wiki 解析、入库和筛选都走 `normalize_ship_type()`。
- 舰种、稀有度、加成舰种、加成属性的筛选项及顺序集中在 `blhx/core/constants.py`，由 `GET /api/filter-options` 下发，页面不自行排序或硬编码选项。
- “加成舰种”和“加成属性”来自 `tech_points.bonus_ship_type` / `tech_points.bonus_attribute`；`tech_points.attribute` 仅保留原始展示文本。
- 推荐排序读取加成舰种，`航母` 已拆为 `正航` 与 `轻航` 并共用原航母排序规则。
- 推荐舰种点选即保存、取消即删除，页面用右下角 toast 提示，不再有“确认推荐”按钮。
- 推荐舰种展示顺序固定为 `RECOMMEND_SHIP_TYPE_ORDER`（战列、战巡、航母、轻航、重巡、轻巡、驱逐），其余舰种按 `SHIP_TYPE_PINYIN` 拼音排序；不要改回 `SHIP_TYPE_ORDER`。
- 挂机与练级都没有目标项的舰种必须在推荐结果中隐藏，由 `next_ships_by_type()` 负责过滤并排序。
- 推荐舰种配置与舰船现状解绑：`save_next_ship_types()` 只校验舰种合法性（`known_ship_types()` = `SHIP_TYPE_ORDER` 全集 + 库中已存在舰种），不要求当前有可练级舰娘；`available_next_ship_types()` 返回全部合法舰种，无候选时计数为 0。配置在舰船全部完成后仍然保留，后续出现新的可练级舰船时自动参与推荐。不要恢复“舰种不存在或已无可练级舰娘”的配置校验。
- 每个舰种的挂机/练级目标数量由 `target_limits` 控制（1–10，默认 1）；手动录入优先占位，剩余名额由自动推荐补齐，录入数量不得超过上限。
- CSV 导入必须复用全量同步的交互：服务端后台任务 + `GET /api/import/status` 轮询进度弹窗 + 完成后从 `GET /api/import/result` 下载结果。导入完成后必须整页刷新（`window.location.reload()`），不得只做局部刷新；刷新要放在结果文件下载触发之后，避免中断下载。
- 表格操作列顺序固定为“已挂机”“已练级”“清除数据”“删除”；“清除数据”调用 `clear_ship_statuses()` 把 `is_idling` 与 `is_trained` 一并恢复为 `NULL` 并写入操作历史。

## 兵装

- 兵装数据只来自 Wiki「特殊兵装」页面的「已实装角色一览」到「联动兵装为活动赠送」之间，解析逻辑在 `blhx/datasource/wiki.py` 的 `parse_armament_entries()`。
- 兵装页里的显示名可能是假名，原始名称取 `<a title>`；不要改用 `span.AF` 的文本，也不要为取名称额外请求舰娘页面。
- 有具体实装日期的为常规兵装，无日期的为联动兵装；联动的日期字段留空，用 `collab_group` 与 `collab_order` 保留 Wiki 列表顺序。
- 同步只覆盖 Wiki 字段，不得重置 `is_built` / `is_enhanced`。
- 标记强化完成必须自动联动标记打造完成；取消强化不得回退打造状态。
- 兵装只有「是否联动」一个类型维度，推荐按两行固定展示，不做类型配置。
- 推荐行顺序为常规在前、联动在后（`ARMAMENT_RECOMMEND_GROUP_ORDER`）；筛选项顺序是联动、常规（`ARMAMENT_GROUP_ORDER`）。两者刻意分开，不要合并。
- 兵装推荐排序为 `海上传奇 > 决战方案 > 最高方案`，其余稀有度不区分高低、按实装日期升序；见 `ARMAMENT_RARITY_PRIORITY`，不要复用舰船推荐的 `RARITY_PRIORITY_ORDER`。
- 兵装状态与舰船状态一样保持“空 = 未记录”语义，`NULL` 与 `0` 都命中“未完成”筛选。

## 验证

- 运行测试：`python3 -m unittest discover -s tests`。
- 改动筛选、表结构、清空逻辑、筛选项顺序或推荐/录入配置结构时，必须同时更新 `tests/test_app.py` 与 `README.md`。
