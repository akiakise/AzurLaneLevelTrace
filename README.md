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
- 外部数据源：碧蓝航线 Wiki 实装日期页与单舰 Wiki 页。
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
| `stage` | 科技点阶段：解锁图鉴、突破至满星、达到120级 |
| `attribute` | 对应属性，例如耐久、炮击、装填、命中、机动、雷击、防空、航空 |
| `points` | 科技点数值 |

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
| `USER_AGENT` | `BLHX-Training-Tracker/1.0` | 请求 Wiki 时的 UA |

数据库路径由 `DATA_DIR` 派生为：

```text
DATA_DIR/blhx-training.sqlite3
```

它不是独立的环境变量。

## 功能细节

### 1. Wiki 数据同步

数据来源：

- 实装日期页：<https://wiki.biligame.com/blhx/%E5%AE%9E%E8%A3%85%E6%97%A5%E6%9C%9F>
- 单舰页：由实装日期页中的 `/blhx/{舰船名称}` 链接推导。

同步逻辑：

1. 请求实装日期页，解析表格中的舰船链接和实装日期。
2. 从链接路径取 `/blhx/` 后面的片段并 URL 解码，作为数据库中的 `name`，避免假名或页面显示名影响主数据。
3. 如果数据库中已存在同名舰船，直接跳过。
4. 对新舰船请求 `action=raw` 的 Wiki 源码。
5. 解析舰种、稀有度和科技点相关字段。
6. 将舰船与多条科技点记录写入 SQLite。
7. `is_idling`、`is_trained` 初始为空，留给 Excel 导入或后续功能更新。

科技点解析自以下 Wiki 字段：

- `解锁图鉴科技点`
- `突破至满星科技点`
- `达到120级科技点`
- `解锁图鉴属性加成`
- `达到120级属性加成`

同步是异步执行的，前端通过 `/api/sync/status` 轮询进度。

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
| `tech_point` | 匹配科技点阶段或属性 |
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
          "attribute": "耐久",
          "points": 26
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
