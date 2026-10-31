# 碧蓝航线练级数据记录

一个用于记录碧蓝航线舰船练级状态的小型 Web 应用。当前实现刻意保持为“单文件 Python 应用 + 标准库 + SQLite”，便于部署和继续二次开发。

## 技术架构

### 运行形态

```text
浏览器前端
    ↓ HTTP / JSON
Python 标准库 HTTP Server
    ↓
业务规则层（Wiki 同步、Excel 导入、推荐排序）
    ↓
SQLite 数据库
```

- 后端框架：Python `http.server.ThreadingHTTPServer`，由 `app.py` 内的 `TrackerHandler` 统一处理路由。
- 数据库：SQLite，默认位于 `DATA_DIR/blhx-training.sqlite3`。
- 前端：内嵌在 `app.py` 的 HTML/CSS/JavaScript 页面，没有 Node.js 构建流程。
- 外部数据源：碧蓝航线 Wiki 后端接口（MediaWiki revisions API 与 Semantic MediaWiki ask API）。
- Excel：通过标准库 `zipfile` 和 `xml.etree.ElementTree` 读写 `.xlsx`，没有引入 `openpyxl`、`pandas` 等依赖。
- 部署：单容器 Docker 部署，宿主机 `./data` 目录挂载到容器 `/data`。

### 项目结构

```text
.
├── app.py                     # 后端、前端、数据库、Wiki 抓取、Excel 处理
├── tests/
│   └── test_app.py            # Wiki 解析、Excel 读写、导入流程测试
├── Dockerfile                 # Python 3.12-slim 镜像
├── docker-compose.yml         # 本地部署配置
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
| `ship_type` | 舰种，例如战列、航母、重巡、轻巡、驱逐、潜艇 |
| `rarity` | 稀有度，例如海上传奇、决战方案、普通、稀有、精锐、超稀有、最高方案 |
| `release_date` | 实装日期，格式 `YYYY-MM-DD` |
| `needs_training` | 是否需要练级；Wiki 同步时根据是否有科技点自动写入 |
| `is_idling` | 是否已挂机；同步后为空，Excel 导入后写入 `0/1` |
| `is_trained` | 是否已练级完成；同步后为空，Excel 导入后写入 `0/1` |
| `wiki_url` | 舰船 Wiki 页面地址 |
| `created_at` / `updated_at` | UTC 时间戳 |

#### `tech_points`

| 字段 | 说明 |
| --- | --- |
| `id` | 自增主键 |
| `ship_id` | 关联 `ships.id` |
| `stage` | 科技点阶段：解锁图鉴、达到120级 |
| `attribute` | 对应属性加成描述，例如 `战巡/战列/航战 耐久` |
| `points` | 保留字段，当前不再用于展示和筛选，固定为 `0` |

一艘舰船可以有多条 `tech_points` 记录。若某舰船没有任何有效科技点，同步时会将 `needs_training` 写为 `否`；否则写为 `是`。

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
| `DATA_DIR` | `./data` | SQLite 数据目录 |
| `HOST` | `0.0.0.0` | HTTP 服务监听地址 |
| `PORT` | `8000` | HTTP 服务端口 |
| `WIKI_BASE_URL` | `https://wiki.biligame.com` | Wiki 站点地址 |
| `WIKI_REQUEST_INTERVAL` | `0.12` | 相邻 Wiki 请求之间的等待秒数 |
| `WIKI_CONCURRENCY` | `4` | 单舰 Wiki 页面并发请求数 |
| `WIKI_RETRY_COUNT` | `4` | Wiki 请求失败后的最大尝试次数 |
| `WIKI_RETRY_DELAY` | `1.5` | Wiki 请求重试的基础等待秒数，实际按指数退避 |
| `LOG_LEVEL` | `INFO` | 日志级别，例如 `INFO`、`WARNING`、`ERROR` |
| `USER_AGENT` | `BLHX-Training-Tracker/1.0` | 请求 Wiki 时的 UA |

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
5. 解析舰种、稀有度，以及解锁图鉴和达到120级对应的属性加成。
6. 将舰船与多条科技点记录写入 SQLite。
7. `is_idling`、`is_trained` 初始为空，留给 Excel 导入或后续功能更新。

科技点解析自以下 Wiki 字段：

- `解锁图鉴属性加成`
- `达到120级属性加成`

突破至满星科技点不解析；启动时会清理历史数据中的对应记录。

同步是异步执行的，前端通过 `/api/sync/status` 轮询进度，并在弹窗中实时展示总进度、新增、跳过和错误数量。同步期间页面会被 loading 遮罩锁定。

少量同步复用上述抓取和解析流程。点击“少量同步”后输入一个或多个舰娘名称；同步前会通过 Wiki 后端接口精确匹配名称，因此也支持数据库中尚不存在的舰娘。匹配成功后只同步这些舰娘，并覆盖对应的 Wiki 字段和科技点加成。

### 2. Excel 批量导入

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
- 处理完成后返回一个 `.xlsx` 结果文件，包含原值、处理状态和处理说明。
- 服务端不会把导入文件或结果文件永久写入磁盘，结果通过 HTTP 响应下载。

模板下载接口：

```text
GET /api/template
```

导入接口：

```text
POST /api/import
```

### 3. 推荐下一个目标

推荐接口：

```text
GET /api/next?mode=idle&ship_type=战列
GET /api/next?mode=train&ship_type=重巡
```

`mode` 含义：

| 模式 | 筛选条件 |
| --- | --- |
| `idle` | `needs_training = 是` 且 `is_idling = 否` |
| `train` | `needs_training = 是` 且 `is_trained = 否` |

排序规则：

1. 舰种科技点属性优先级。
2. 稀有度优先级。
3. 舰船名称。

稀有度优先级从高到低：

```text
海上传奇 > 决战方案 > 普通 > 稀有 > 精锐 > 超稀有 > 最高方案
```

舰种科技点属性优先级：

| 舰种 | 优先级从高到低 |
| --- | --- |
| 战列 | 装填 > 命中 > 炮击 > 防空 > 耐久 > 其他 |
| 航母 | 装填 > 命中 > 航空 > 防空 > 其他 |
| 重巡 | 机动 > 炮击 > 装填 > 命中 > 雷击 > 防空 > 耐久 > 其他 |
| 轻巡 | 机动 > 炮击 > 命中 > 装填 > 雷击 > 防空 > 耐久 > 其他 |
| 驱逐 | 机动 > 炮击 > 装填 > 雷击 > 命中 > 其他 |
| 潜艇 | 雷击 > 其他 |

### 4. 查询与筛选

查询接口：

```text
GET /api/ships
```

支持的查询参数：

| 参数 | 说明 |
| --- | --- |
| `name` | 舰船名称模糊查询 |
| `ship_type` | 舰种精确匹配 |
| `rarity` | 稀有度精确匹配 |
| `tech_attribute` | 匹配解锁图鉴或120级属性加成中的属性 |
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
      "release_date": "2023-08-17",
      "needs_training": true,
      "is_idling": null,
      "is_trained": null,
      "wiki_url": "https://wiki.biligame.com/blhx/%E6%AD%A6%E8%97%8F",
      "tech_points": [
        {
          "stage": "解锁图鉴",
          "attribute": "战巡/战列/航战 耐久",
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
| `GET` | `/api/next` | 获取下一个挂机或练级目标 |
| `POST` | `/api/sync` | 启动 Wiki 同步 |
| `GET` | `/api/sync/status` | 查询同步进度 |
| `GET` | `/api/template` | 下载 Excel 导入模板 |
| `POST` | `/api/import` | 上传 Excel 并返回处理结果 Excel |
| `POST` | `/api/ship/status` | 更新单舰已挂机或已练级状态 |
| `POST` | `/api/ship/delete` | 删除单舰及其科技点数据 |
| `POST` | `/api/ships/clear` | 清空所有舰娘及其科技点数据 |

Web 页面筛选：

- 舰种、稀有度、科技属性和三个状态筛选均为平铺按钮，点选后立即重新查询。
- 舰船名称输入后自动模糊查询。
- 页面默认选中“已练级完成 = 否”。
- 列表默认按实装日期升序排列。
- 页面顶部提供“少量同步”按钮，点击后可输入一个或多个舰娘名称启动同步。
- 页面顶部提供“全量同步”按钮，点击后需要通过网页内弹窗二次确认才会发起同步。
- 页面顶部提供“清空数据”按钮；确认后删除全部舰娘及其科技点数据，同步进行中会拒绝清空。
- 表格操作列提供状态完成按钮和删除按钮；状态未完成时显示对应完成按钮，删除前会通过网页内弹窗二次确认。

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

启动：

```bash
docker compose up -d --build
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

## 本地开发注意事项

- 当前应用没有 `requirements.txt`，因为只使用 Python 标准库。
- 修改 Wiki 解析逻辑时，优先补充或调整 `tests/test_app.py` 中的样例数据。
- 修改 Excel 逻辑时，注意保持模板列名完全一致。
- SQLite 文件和运行数据目录不要提交到 Git。
- `.gitignore` 已覆盖 Python 缓存、虚拟环境、测试缓存、SQLite 数据、日志和常见本地产物。
