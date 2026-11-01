# 碧蓝航线练级数据记录

一个用于记录碧蓝航线舰船练级状态的小型 Web 应用。当前实现使用“模块化 Python 应用 + 标准库 + SQLite”，不依赖前端构建工具，便于部署和继续二次开发。

## 技术架构

### 运行形态

```text
浏览器前端
    ↓ HTTP / JSON
Python 标准库 HTTP Server
    ↓
业务规则层（Wiki 同步、CSV 导入、推荐排序）
    ↓
SQLite 数据库
```

- 后端框架：Python `http.server.ThreadingHTTPServer`，由 `blhx/web/server.py` 内的 `TrackerHandler` 统一处理路由。
- 数据库：SQLite，默认位于 `DATA_DIR/blhx-training.sqlite3`；推荐配置保存在 `DATA_DIR/config.json`。
- 前端：位于 `blhx/web/page.html` 的 HTML/CSS/JavaScript 页面，没有 Node.js 构建流程；站点图标为 `blhx/web/static/icon.png`，通过 `/icon.png` 提供。
- 外部数据源：碧蓝航线 Wiki 后端接口（MediaWiki revisions API 与 Semantic MediaWiki ask API）。
- CSV：通过标准库 `csv` 读写 UTF-8（带 BOM）`.csv`，Excel 与常见表格软件均可直接打开，没有引入额外依赖。
- 部署：单容器 Docker 部署，宿主机 `./data` 目录挂载到容器 `/data`。

### 项目结构

```text
.
├── app.py                     # 兼容入口与统一导出
├── blhx/
│   ├── __init__.py
│   ├── core/                  # 通用基础设施
│   │   ├── constants.py       # 常量、排序规则与路径配置
│   │   ├── state.py           # 同步与导入任务状态
│   │   └── utils.py           # 时间与通用工具
│   ├── storage/               # 持久化层
│   │   ├── db.py              # SQLite 连接与表结构初始化
│   │   ├── config.py          # data/config.json 配置读写
│   │   └── history.py         # 操作历史记录与查询
│   ├── domain/                # 业务领域层
│   │   ├── tech.py            # 加成属性与舰种解析
│   │   ├── ships.py           # 舰船 CRUD、筛选与状态更新
│   │   └── recommendations.py # 单目标与按舰种分组推荐
│   ├── datasource/            # 外部数据源
│   │   └── wiki.py            # Wiki 抓取、解析与同步
│   ├── transfer/              # 数据导入导出
│   │   └── csv_io.py          # CSV 读写、导入任务与模板
│   ├── web/                   # Web 层
│   │   ├── server.py          # HTTP 路由与服务入口
│   │   ├── page.py            # 前端页面加载
│   │   ├── page.html          # 前端页面
│   │   └── static/icon.png    # 站点图标
│   └── <旧模块>.py            # 兼容转发层，转发到上述子包
├── tests/
│   ├── support.py             # 测试数据目录隔离
│   └── test_app.py            # Wiki 解析、CSV 读写、筛选与导入测试
├── Dockerfile                 # Python 3.12-slim-bookworm 镜像
├── docker-compose.yml         # 拉取 Docker Hub 预构建镜像的部署配置
├── .github/workflows/         # GitHub Actions 自动构建与镜像推送
├── README.md
├── .dockerignore
└── .gitignore
```

### 数据模型

#### `ships`

| 字段 | 说明 |
| --- | --- |
| `id` | 自增主键 |
| `name` | 舰船名称，唯一；Wiki 同步时取 Wiki 页面 URL 解码后的名称 |
| `ship_type` | 舰种，取值见下方“舰种顺序”；风帆M、风帆S、风帆V 统一归一为 `风帆` |
| `rarity` | 稀有度，例如海上传奇、决战方案、普通、稀有、精锐、超稀有、最高方案 |
| `release_date` | 实装日期，格式 `YYYY-MM-DD` |
| `needs_training` | 是否需要练级；Wiki 同步时根据是否有加成属性自动写入 |
| `is_idling` | 是否已挂机；同步后为空，CSV 导入后写入 `0/1` |
| `is_trained` | 是否已练级完成；同步后为空，CSV 导入后写入 `0/1` |
| `wiki_url` | 舰船 Wiki 页面地址 |
| `created_at` / `updated_at` | UTC 时间戳 |

#### `tech_points`

| 字段 | 说明 |
| --- | --- |
| `id` | 自增主键 |
| `ship_id` | 关联 `ships.id` |
| `stage` | 加成阶段：解锁图鉴、达到120级 |
| `attribute` | 原始加成描述，例如 `战巡/战列/航战 耐久` |
| `bonus_ship_type` | 加成舰种，多个值用 `/` 分隔，例如 `战巡/战列/航战` |
| `bonus_attribute` | 加成属性，例如 `耐久`；用于“加成属性”精确筛选 |
| `points` | 保留字段，当前不再用于展示和筛选，固定为 `0` |

一艘舰船可以有多条 `tech_points` 记录。若某舰船没有任何有效加成属性，同步时会将 `needs_training` 写为 `否`；否则写为 `是`。`bonus_ship_type` 与 `bonus_attribute` 由 `attribute` 解析写入，是“加成舰种”“加成属性”筛选的依据；旧库缺少这两列时会在启动时重建该表。

#### `operation_logs`

记录状态更新、删除、清空和 CSV 导入等操作。`created_at` 使用带毫秒精度的 UTC 时间戳。单舰删除会通过外键级联删除关联 `tech_points`；“清空数据”会清空 `ships`、`tech_points`、`operation_logs`、`sqlite_sequence` 以及 `DATA_DIR/config.json`。

## 开发环境

- 推荐 Python：3.11 或更高版本；容器运行时使用 Python 3.12-slim。
- 第三方 Python 包：无。
- 数据库：Python 内置 `sqlite3`。
- 本地启动：

  ```bash
  python app.py
  ```

  默认访问 `http://localhost:8000`。

- 运行测试：

  ```bash
  python -m unittest discover -s tests
  ```

### 环境变量

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `./data` | 正式数据目录（数据库与推荐配置） |
| `HOST` | `0.0.0.0` | HTTP 服务监听地址 |
| `PORT` | `8000` | HTTP 服务端口 |
| `WIKI_BASE_URL` | `https://wiki.biligame.com` | Wiki 站点地址 |
| `WIKI_REQUEST_INTERVAL` | `0.12` | 相邻 Wiki 请求之间的等待秒数 |
| `WIKI_CONCURRENCY` | `4` | 单舰 Wiki 页面并发请求数 |
| `WIKI_RETRY_COUNT` | `4` | Wiki 请求失败后的最大尝试次数 |
| `WIKI_RETRY_DELAY` | `1.5` | Wiki 请求重试的基础等待秒数，实际按指数退避 |
| `LOG_LEVEL` | `INFO` | 日志级别，例如 `INFO`、`WARNING`、`ERROR` |
| `USER_AGENT` | `BLHX-Training-Tracker/1.0` | 请求 Wiki 时的 UA |

配置文件 `DATA_DIR/config.json` 结构：

```json
{
  "next_ship_types": ["战列", "重巡"],
  "target_limits": {
    "战列": {"is_idling": 1, "is_trained": 2}
  },
  "manual_targets": {
    "战列": {"is_trained": [580, 581]}
  }
}
```

`target_limits` 是每个舰种挂机/练级的目标数量上限（1–10）；`manual_targets` 保存手动录入的舰娘 ID，数量不超过对应上限。

数据库路径由 `DATA_DIR` 派生为：

```text
DATA_DIR/blhx-training.sqlite3
```

它不是独立的环境变量。

## 功能细节

### 1. Wiki 数据同步

数据来源：

- MediaWiki revisions API：按舰娘标题读取 `{{舰娘图鉴}}` 原始模板内容。
- Semantic MediaWiki ask API：按舰娘分类分页查询名称、编号和实装日期。

同步逻辑：

1. 通过 `action=ask` 请求常规、联动、方案和 META 舰娘分类，并按实装日期升序分页读取。
2. 使用接口返回的页面标题作为数据库中的 `name`，避免假名或页面显示名影响主数据。
3. 如果数据库中已存在同名舰船，直接跳过。
4. 对新舰船以 `WIKI_CONCURRENCY` 个线程并发请求 `action=query` 的 revision wikitext；全局仍按 `WIKI_REQUEST_INTERVAL` 控制请求节奏。
5. 解析舰种、稀有度，以及解锁图鉴和达到120级对应的加成属性。
6. 将舰船与多条加成记录写入 SQLite。
7. `is_idling`、`is_trained` 初始为空，留给 CSV 导入或后续功能更新。

加成属性解析自以下 Wiki 字段：

- `解锁图鉴属性加成`
- `达到120级属性加成`

突破至满星加成不解析。

风帆M、风帆S、风帆V 在解析与入库时统一归一为 `风帆`。

同步是异步执行的，前端通过 `/api/sync/status` 轮询进度，并在弹窗中实时展示总进度、新增、跳过和错误数量。同步期间页面会被 loading 遮罩锁定。

少量同步复用上述抓取和解析流程。点击“少量同步”后输入一个或多个舰娘名称；同步前会通过 Wiki 后端接口精确匹配名称，因此也支持数据库中尚不存在的舰娘。匹配成功后只同步这些舰娘，并覆盖对应的 Wiki 字段和加成记录。

### 2. CSV 批量导入

导入模板列：

| 列名 | 可填值 |
| --- | --- |
| 舰船名称 | 需要与数据库中的 `name` 完全一致 |
| 是否已挂机 | `是` / `否` |
| 是否已练级完成 | `是` / `否` |

处理规则：

- 按舰船名称精确匹配数据库记录。
- 未匹配到的记录跳过，并在结果表中标注“未匹配到数据跳过”。
- 状态列只接受 `是` 或 `否`，其他值跳过。
- 与全量同步一致，导入在后台任务中执行，页面通过进度弹窗轮询展示已处理、跳过和错误数量。
- 导入完成后从 `/api/import/result` 下载 `.csv` 结果文件，包含原值、处理状态和处理说明。
- 导入中重复上传会被拒绝；完成前结果文件不可下载。
- 服务端不会把导入文件或结果文件永久写入磁盘，结果保存在进程内状态中并通过 HTTP 响应下载。
- 导入完成后页面会先下载结果文件，再整页刷新，避免前端残留筛选状态或缓存数据造成显示遗漏。

模板下载接口：

```text
GET /api/template
```

导入接口：

```text
POST /api/import          # 上传 CSV，校验通过后启动后台导入
GET  /api/import/status   # 导入进度（与同步进度字段一致）
GET  /api/import/result   # 下载导入结果 CSV
```

### 3. 推荐下一个目标

推荐接口：

```text
GET /api/next?mode=idle&ship_type=战列
GET /api/next?mode=train&ship_type=重巡
GET /api/next?mode=train&ship_types=战列&ship_types=重巡
GET /api/config
POST /api/config
GET /api/next-options
GET /api/next-group?ship_types=战列&ship_types=重巡
GET /api/operations?limit=200
```

`ship_types` 支持传入多个舰种；页面推荐配置会使用该参数，风帆 M/S/V 等别名会归一为同一舰种。两类接口对无效舰种的处理不同：

| 接口 | 传入不存在或无候选的舰种 |
| --- | --- |
| `GET /api/next` | 返回错误，提示舰种不存在或已无可练级舰娘 |
| `GET /api/next-group` | 返回 200，对应舰种在结果中被过滤掉；全部为空时返回空 `rows` |

`GET /api/next-options` 只返回仍有可练级舰娘的舰种，并附带当前挂机与练级候选数量。

页面展示可选舰种，点选即自动保存、取消选择即自动移除对应的推荐配置，并在右下角弹出“推荐舰种已更新”通知，不再需要点“确认推荐”。

推荐舰种按固定顺序展示：战列、战巡、航母、轻航、重巡、轻巡、驱逐，其余舰种按名称拼音排序。挂机与练级都没有目标的舰种不展示。

每个舰种单独占一行，挂机目标与练级目标各自维护一个数量上限（1–10，默认 1），可用 `+` / `-` 调整。每个目标位支持手动录入舰娘：录入列表从表格数据中按舰种筛选，并支持关键字模糊查询，已录入项会打勾，再次点击即取消录入；录入数量不能超过上限。手动录入优先占用名额，剩余名额由自动推荐按加成舰种优先级、稀有度、名称补齐。推荐结果中的“挂机完成”“练级完成”按钮可直接更新状态，并自动刷新舰船表格、候选数量与推荐结果。在推荐结果或舰船表格中标记“练级”时，如果舰娘尚未挂机，系统会同时自动标记为已挂机完成。

`mode` 含义：

| 模式 | 筛选条件 |
| --- | --- |
| `idle` | `needs_training = 是` 且 `is_idling = 否` |
| `train` | `needs_training = 是`、`is_trained = 否` 且 `is_idling = 是` |

排序规则：

1. 加成舰种优先级（读取 `bonus_ship_type`，不再使用舰船自身 `ship_type`）。
2. 稀有度优先级。
3. 舰船名称。

稀有度优先级从高到低：

```text
海上传奇 > 决战方案 > 普通 > 稀有 > 精锐 > 超稀有 > 最高方案
```

该顺序只用于推荐排序；筛选按钮的展示顺序见下文“筛选项顺序”。

加成舰种优先级：

| 加成舰种 | 优先级从高到低 |
| --- | --- |
| 战列 | 装填 > 命中 > 炮击 > 防空 > 耐久 > 其他 |
| 正航 | 装填 > 命中 > 航空 > 防空 > 其他 |
| 轻航 | 装填 > 命中 > 航空 > 防空 > 其他 |
| 重巡 | 机动 > 炮击 > 装填 > 命中 > 雷击 > 防空 > 耐久 > 其他 |
| 轻巡 | 机动 > 炮击 > 命中 > 装填 > 雷击 > 防空 > 耐久 > 其他 |
| 驱逐 | 机动 > 炮击 > 装填 > 雷击 > 命中 > 其他 |
| 潜艇 | 雷击 > 其他 |

`航母` 项已细化为 `正航` 与 `轻航`，二者共用原航母排序规则。

### 4. 查询与筛选

查询接口：

```text
GET /api/ships
```

支持的查询参数：

| 参数 | 说明 |
| --- | --- |
| `name` | 舰船名称模糊查询 |
| `ship_type` | 舰种精确匹配；风帆M/S/V 均视为 `风帆` |
| `rarity` | 稀有度精确匹配 |
| `bonus_ship_type` | 加成舰种精确匹配，按 `bonus_ship_type` 中的任一项命中 |
| `bonus_attribute` | 加成属性精确匹配，按 `bonus_attribute` 命中 |
| `needs_training` | `是` / `否` |
| `is_idling` | `是` / `否` |
| `is_trained` | `是` / `否` |

接口返回：

```json
{
  "ships": [
    {
      "id": 1,
      "name": "武藏",
      "ship_type": "战列",
      "rarity": "海上传奇",
      "bonus_ship_types": ["战巡", "战列", "航战"],
      "release_date": "2023-08-17",
      "needs_training": true,
      "is_idling": null,
      "is_trained": null,
      "wiki_url": "https://wiki.biligame.com/blhx/%E6%AD%A6%E8%97%8F",
      "tech_points": [
        {
          "stage": "解锁图鉴",
          "attribute": "战巡/战列/航战 耐久",
          "bonus_ship_type": "战巡/战列/航战",
          "bonus_attribute": "耐久",
          "points": 0
        }
      ]
    }
  ]
}
```

## HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | Web 页面 |
| `GET` | `/api/ships` | 查询舰船列表 |
| `GET` | `/api/filter-options` | 获取各筛选项及其固定顺序 |
| `GET` | `/api/config` | 读取推荐舰种、数量上限与手动录入配置 |
| `POST` | `/api/config` | 保存推荐舰种配置 |
| `POST` | `/api/target-limit` | 保存某舰种挂机/练级目标数量上限（1–10） |
| `POST` | `/api/manual-target` | 录入/取消某舰种某模式的手动目标舰娘 |
| `GET` | `/api/next` | 获取下一个挂机或练级目标 |
| `GET` | `/api/next-options` | 获取可配置的推荐舰种及候选数量 |
| `GET` | `/api/next-group` | 按舰种分别获取挂机与练级推荐 |
| `GET` | `/api/operations` | 查询操作历史 |
| `POST` | `/api/sync` | 启动 Wiki 同步 |
| `GET` | `/api/sync/status` | 查询同步进度 |
| `GET` | `/api/template` | 下载 CSV 导入模板 |
| `POST` | `/api/import` | 上传 CSV 并启动后台导入 |
| `GET` | `/api/import/status` | 查询导入进度 |
| `GET` | `/api/import/result` | 下载导入结果 CSV |
| `POST` | `/api/ship/status` | 更新单舰已挂机或已练级状态 |
| `POST` | `/api/ship/clear-status` | 清除单舰已挂机与已练级状态 |
| `POST` | `/api/ship/delete` | 删除单舰及其加成记录 |
| `POST` | `/api/ships/clear` | 清空全部业务数据与配置 |

Web 页面筛选：

- 舰种、稀有度、加成舰种、加成属性和三个状态筛选均为平铺按钮，点选后立即重新查询。
- 各筛选项顺序固定由后端 `/api/filter-options` 下发，前端不再按数据排序。
- 舰种与加成属性同时筛选时按 AND 组合；若要按“某舰船提供的加成面向哪个舰种”筛选，请使用“加成舰种”。
- 推荐舰种点选即保存、取消即删除，配置保存在 `data/config.json` 的 `next_ship_types`；可选舰种仅包含仍存在可练级舰娘的类型。
- 每个舰种的挂机/练级目标数量保存在 `target_limits`，手动录入舰娘保存在 `manual_targets`。
- 推荐结果按舰种逐行展示挂机与练级目标，并提供“挂机完成”“练级完成”快捷按钮；操作后自动刷新表格与推荐数据。
- 页面顶部提供“操作历史”弹窗，记录状态更新、删除、清空和 CSV 导入等操作内容与毫秒级时间点。
- 舰船名称输入后自动模糊查询。
- 页面默认选中“需要练级 = 是”和“已练级完成 = 否”。
- 列表默认按实装日期升序排列。
- 页面顶部提供“少量同步”按钮，点击后可输入一个或多个舰娘名称启动同步。
- 页面顶部提供“全量同步”按钮，点击后需要通过网页内弹窗二次确认才会发起同步。
- 页面顶部提供“清空数据”按钮；确认后清空全部舰船、加成记录、操作历史、推荐舰种配置和数据库自增序列，同步进行中会拒绝清空。
- 表格操作列按钮顺序固定为“已挂机”“已练级”“清除数据”“删除”：未完成时显示对应的已挂机/已练级按钮；“清除数据”仅在存在挂机或练级状态时出现，点击后把两项状态一起恢复为未记录；删除前会通过网页内弹窗二次确认。
- 页面兼容手机与平板访问：窄屏（≤820px）下舰船列表与推荐目标改为带字段名的卡片式布局，工具栏双列排列，按钮与表单项放大到触控尺寸，弹窗可滚动并使用系统安全区留白；更宽屏幕的舰船表格在自身区域内横向滚动，不影响整页布局。

`POST /api/sync` 请求体：

全量同步：

```json
{}
```

少量同步：

```json
{
  "ship_names": ["武藏", "古鹰"]
}
```

`POST /api/ship/status` 请求体：

```json
{
  "id": 1,
  "field": "is_idling",
  "value": true
}
```

`POST /api/ship/delete` 请求体：

```json
{
  "id": 1
}
```

## Docker 部署

首次部署时拉取 Docker Hub 上的预构建镜像并启动：

```bash
docker compose pull
docker compose up -d
```

停止：

```bash
docker compose down
```

查看日志：

```bash
docker compose logs -f
```

数据持久化目录：

```text
./data -> /data
```

SQLite 文件位于：

```text
./data/blhx-training.sqlite3
```

### 自动构建与镜像推送

仓库推送到 `main` 后，GitHub Actions 会自动构建镜像并推送到 Docker Hub，工作流文件为 `.github/workflows/docker-publish.yml`。镜像地址：

```text
akise/azur-lane-level-trace:latest
akise/azur-lane-level-trace:sha-<提交短哈希>
```

首次使用前，在 Docker Hub 创建公开仓库 `azur-lane-level-trace`，并在 GitHub 仓库的 `Settings > Secrets and variables > Actions` 中添加：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名 `akise`。
- `DOCKERHUB_TOKEN`：Docker Hub 账号设置中创建的 Access Token，权限选择 `Read & Write`。

私有 Docker Hub 仓库需要先在服务器登录：

```bash
docker login -u akise
```

后续更新只需重新拉取并重建容器：

```bash
docker compose pull
docker compose up -d
```

`./data` 仍然挂载到容器 `/data`，更新镜像不会影响 SQLite 数据。如果希望服务器全自动更新，可以再部署 Watchtower 定时执行上述拉取动作；默认不启用，便于在更新前自行确认。

### 筛选项顺序

| 筛选项 | 顺序 |
| --- | --- |
| 舰种 | 驱逐、轻巡、重巡、超巡、战巡、战列、航母、轻航、重炮、维修、航战、潜艇、潜母、运输、风帆 |
| 稀有度 | 普通、稀有、精锐、超稀有、海上传奇、最高方案、决战方案 |
| 加成舰种 | 驱逐、轻巡、重巡、超巡、战巡、战列、正航、轻航、重炮、维修、航战、潜艇、潜母、运输、风帆 |
| 加成属性 | 耐久、炮击、雷击、防空、航空、装填、机动、命中、反潜 |

## 本地开发注意事项

- 当前应用没有 `requirements.txt`，因为只使用 Python 标准库。
- 测试必须使用 `tests/support.py` 的 `isolated_app_data()` 隔离数据目录，禁止读写正式 `data/` 目录。
- 修改 Wiki 解析逻辑时，优先补充或调整 `tests/test_app.py` 中的样例数据。
- 修改 CSV 逻辑时，注意保持模板列名完全一致；文件使用 UTF-8 BOM 编码，导入时同时兼容 UTF-8 与 GB18030。
- SQLite 文件和运行数据目录不要提交到 Git。
- `.gitignore` 已覆盖 Python 缓存、虚拟环境、测试缓存、SQLite 数据、日志和常见本地产物。
