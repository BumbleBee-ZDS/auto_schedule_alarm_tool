# DataMonitor - 数据监控告警工具

一个运行在 Windows 上的轻量级数据监控工具：定时查询关系型数据库，当查询结果中的数据满足用户自定义的触发条件时，弹出 Windows 原生桌面通知（Toast）。

提供**可视化配置界面**，支持系统托盘常驻后台运行，可打包为独立 exe（无需安装 Python 环境）。

---

## 功能特性

- **多种数据库支持**：MySQL / PostgreSQL / SQL Server / SQLite / Oracle（基于 SQLAlchemy）
- **SQL 安全限制**：仅允许单条 SELECT 查询，基于 sqlparse 词法分析拦截 INSERT / UPDATE / DELETE / CREATE / DROP 等所有写操作与 DDL
- **灵活的多条件触发**：
  - 支持操作符：`=` `!=` `>` `<` `>=` `<=` `包含` `与当前时间差(分钟)在区间内`
  - 条件可任意新增 / 删除
  - 多条件之间支持「且（AND，全部满足）」或「或（OR，任一满足）」
  - 值比较自动适配类型：数值 → 时间 → 字符串
- **自定义通知内容**：可指定任意列作为通知字段（可增删）；留空则通知整行数据
- **后台常驻 + 低资源占用**：内部定时循环，空闲时处于 sleep 状态几乎零 CPU 占用；可最小化到系统托盘
- **内存优化**：数据过滤在 SQL 层完成（WHERE 子句），不做全表加载
- **日志双通道**：界面实时显示 + 落盘 `monitor.log`，后台运行可排查

---

## 文件结构

```
auto_schedule_alarm_tool/
├── monitor.py        # 核心引擎：配置读取、SQL 校验、条件评估、通知发送、定时循环
├── monitor_gui.py    # 可视化界面（tkinter）：配置编辑、条件管理、托盘、监控控制
├── config.json       # 配置文件（GUI 保存时自动读写，也可手工编辑）
└── dist/
    ├── DataMonitor.exe   # 打包产物（单文件、无窗口）
    └── config.json       # exe 同目录配置（exe 启动时读取）
```

---

## 快速开始（使用打包好的 exe）

1. 将 `dist\DataMonitor.exe` 和 `dist\config.json` 拷贝到同一目录
2. 双击 `DataMonitor.exe` 打开配置界面
3. 按下方「界面说明」填写配置，点击 **测试查询** 验证 SQL 与数据
4. 点击 **启动监控**，然后 **最小化到托盘** 进入后台
5. 满足条件时自动弹出 Windows 通知；托盘图标右键可「打开主界面」或「退出」

### 开机自启

方式一（推荐）：把 exe 快捷方式放入启动文件夹（`Win+R` 输入 `shell:startup`），并在快捷方式目标的末尾加上 ` --auto-start`，开机即自动进入监控状态。

方式二：先手动打开软件点「启动监控」。

---

## 界面说明

### 数据库连接

| 字段 | 说明 |
|---|---|
| 驱动 | 下拉选择：`mysql+pymysql` / `postgresql+psycopg2` / `mssql+pyodbc` / `sqlite` / `oracle+oracledb` |
| 主机 / 端口 / 用户名 / 密码 / 数据库名 | 常规连接信息；选 `sqlite` 时「数据库名」填 db 文件的绝对路径（用 `/` 分隔），其余留空 |

### SQL 查询

- 仅允许 **一条 SELECT** 语句，其他语句或关键字会被拦截并报错
- **必须在 SQL 中用 WHERE 过滤数据**（如最近 24 小时），避免全表加载到内存：

```sql
-- MySQL
SELECT id, description, data_time FROM events WHERE data_time >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
-- PostgreSQL
SELECT id, description, data_time FROM events WHERE data_time >= NOW() - INTERVAL '24 hours'
-- SQL Server
SELECT id, description, data_time FROM events WHERE data_time >= DATEADD(HOUR, -24, GETDATE())
-- SQLite
SELECT id, description, data_time FROM events WHERE data_time >= datetime('now', 'localtime', '-24 hours')
```

### 触发条件

每条条件由四部分组成：**字段 + 条件 + 值 + 值2**（值2 仅时间区间条件使用）。

| 条件 | 说明 | 示例 |
|---|---|---|
| 等于 / 不等于 | 数值、时间或字符串相等性比较（自动适配类型） | `id 等于 123` |
| 大于 / 小于 / 大于等于 / 小于等于 | 数值或时间比较 | `level 大于等于 3` |
| 包含 | 字符串子串匹配 | `description 包含 超时` |
| 与当前时间差(分钟)在区间内 | 时间列与当前系统时间的绝对差值落在 `[值, 值2]` 分钟区间内 | `data_time 时间差在 55 ~ 65` |

**多条件关系**：
- **且（AND）**：所有条件都满足才触发
- **或（OR）**：任一条件满足即触发

> 时间区间条件的典型用途是防重复告警：例如每 5 分钟检查一次，只有数据时间与当前时间差值落在 55~65 分钟窗口内才通知，同一条数据只会命中一次。

### 通知字段

- 添加要在通知正文中展示的列名，可增删
- **留空则通知整行数据**的所有列

### 调度

- 检查间隔（分钟）：默认 5 分钟。监控运行期间修改配置并保存后，下一轮自动生效，无需重启

---

## 配置文件格式（config.json）

GUI 保存时自动生成，也可手工编辑：

```json
{
  "database": {
    "driver": "mysql+pymysql",
    "host": "127.0.0.1",
    "port": "3306",
    "user": "your_username",
    "password": "your_password",
    "database": "your_database"
  },
  "query": {
    "sql": "SELECT id, description, data_time FROM events WHERE data_time >= DATE_SUB(NOW(), INTERVAL 24 HOUR)"
  },
  "schedule": { "interval_minutes": 5 },
  "combinator": "AND",
  "conditions": [
    { "column": "data_time", "op": "time_diff_between", "value": "55", "value2": "65" },
    { "column": "level", "op": ">=", "value": "3" }
  ],
  "notify_fields": ["id", "description"]
}
```

| 字段 | 说明 |
|---|---|
| `combinator` | `"AND"`（且）或 `"OR"`（或） |
| `conditions[].op` | `=` `!=` `>` `<` `>=` `<=` `contains` `time_diff_between` |
| `conditions[].value2` | 仅 `time_diff_between` 需要（区间上限，分钟） |
| `notify_fields` | 通知展示的列名数组；`[]` 表示整行通知 |

---

## 从源码运行

```powershell
pip install sqlalchemy winotify sqlparse pystray pillow
# 按数据库类型再装对应驱动：
pip install pymysql            # MySQL
pip install psycopg2-binary    # PostgreSQL
pip install pyodbc             # SQL Server

python monitor_gui.py              # GUI 界面
python monitor_gui.py --auto-start # 启动即进入监控
python monitor.py                  # 无界面纯后台模式（读取 config.json）
```

---

## 打包为 exe

```powershell
pip install pyinstaller
pyinstaller --onefile --noconsole --name DataMonitor monitor_gui.py
```

> **注意（conda 版 Python）**：conda 的核心 DLL 位于 `Library\bin`，PyInstaller 可能收集不全导致 exe 闪退（如 `_tkinter` / `_ctypes` 报错），需显式补充：
>
> ```powershell
> $bin = "<conda路径>\Library\bin"
> $dlls = @("tcl86t.dll","tk86t.dll","ffi-8.dll","ffi.dll","libcrypto-3-x64.dll",
>           "libssl-3-x64.dll","libmpdec-4.dll","libexpat.dll","sqlite3.dll",
>           "liblzma.dll","libbz2.dll","zlib.dll")
> $args = @("-m","PyInstaller","--onefile","--noconsole","--name","DataMonitor","--clean","--noconfirm")
> foreach ($d in $dlls) { $args += @("--add-binary", "$bin\$d;.") }
> $args += "monitor_gui.py"
> python @args
> ```

---

## 故障排查

| 问题 | 排查方式 |
|---|---|
| 双击 exe 无反应 / 闪退 | 查看 exe 同目录的 `monitor.log`；或用控制台模式打包（`--console`）查看报错 |
| 没有弹出通知 | 确认 Windows 通知未被系统「勿扰模式」屏蔽；检查 `monitor.log` 中是否有「发送通知失败」 |
| 数据库连接失败 | 先用界面的「测试查询」按钮验证连接和 SQL；确认对应数据库驱动已打包/安装 |
| 条件不触发 | 检查条件中字段名是否与查询结果的列名完全一致；时间列需为 `YYYY-MM-DD HH:MM:SS` 可解析格式 |
| 想完全退出程序 | 托盘图标右键 → 退出；或任务管理器结束 `DataMonitor.exe` |

所有运行记录都写入 exe（或脚本）同目录的 `monitor.log`。

---

## 设计说明

- **条件引擎**：对每行数据评估所有条件，按 AND/OR 组合；比较时依次尝试数值、时间、字符串类型，无需用户指定类型
- **SQL 注入防护**：sqlparse 词法分析，只允许单条 SELECT；字符串字面量中的同名词汇（如 `'delete'`）不会误判
- **时区处理**：带时区的时间戳统一转换为本地 naive datetime 后再比较，避免 aware/naive 混比报错
- **低资源**：无检查任务时线程处于可中断等待状态（`Event.wait`），停止监控立即响应
