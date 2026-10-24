# -*- coding: utf-8 -*-
"""
monitor.py - 通用数据监控 + Windows 原生桌面通知核心逻辑

支持多条件触发（条件间可"且"/"或"组合）、自定义通知字段、
任意列比较（数值/时间/字符串），常驻后台低频运行。
"""

import ctypes
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# winotify 依赖 powershell.exe 发送通知；部分环境 PATH 不含该目录，启动时补齐
_PS_DIR = r"C:\Windows\System32\WindowsPowerShell\v1.0"
if os.path.isdir(_PS_DIR) and _PS_DIR.lower() not in os.environ.get("PATH", "").lower():
    os.environ["PATH"] = _PS_DIR + os.pathsep + os.environ.get("PATH", "")

import sqlparse
from sqlparse.tokens import DML, Keyword
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from winotify import Notification, audio

# 打包为 exe 后，__file__ 指向临时解压目录，需改用 exe 所在目录定位 config.json
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
LOG_FILE = BASE_DIR / "monitor.log"

# 除 SELECT 外禁止出现的 SQL 关键字（防止写操作/DDL 注入）
FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE",
    "REPLACE", "MERGE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL",
    "SET", "USE", "INTO",
}


def validate_select_only(sql):
    """校验 SQL 只允许单条 SELECT 查询，否则抛出 ValueError。"""
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        raise ValueError("SQL 不能为空")
    statements = [s for s in sqlparse.parse(sql) if str(s).strip()]
    if len(statements) != 1:
        raise ValueError("只允许输入一条 SQL 语句")
    stmt = statements[0]
    first = stmt.token_first(skip_cm=True)
    if first is None or first.ttype not in DML or first.value.upper() != "SELECT":
        raise ValueError("只允许 SELECT 查询")
    for token in stmt.flatten():
        if token.ttype in Keyword and token.value.upper() in FORBIDDEN_KEYWORDS:
            raise ValueError(f"SQL 中包含被禁止的关键字: {token.value.upper()}")
    return sql


def minimize_console():
    """最小化控制台窗口（若存在）。无控制台时自动跳过。"""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
    except Exception:
        pass


def setup_logging():
    """日志同时写入文件；有控制台时才输出到控制台（pythonw 下 sys.stdout 为 None）。"""
    handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def load_config(path):
    """读取 JSON 配置文件，返回 dict。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ("database", "query"):
        if key not in cfg:
            raise ValueError(f"配置文件缺少 {key} 配置")
    return cfg


def build_engine(db_cfg):
    """用 URL.create 构造连接，避免密码中特殊字符需要手动 URL 编码的问题。"""
    driver = db_cfg.get("driver") or "mysql+pymysql"
    if driver.startswith("sqlite"):
        return create_engine(f"sqlite:///{db_cfg['database']}")
    port_raw = str(db_cfg.get("port") or "").strip()
    url = URL.create(
        drivername=driver,
        username=db_cfg.get("user") or None,
        password=db_cfg.get("password") or None,
        host=db_cfg.get("host") or None,
        port=int(port_raw) if port_raw else None,
        database=db_cfg.get("database") or None,
    )
    return create_engine(url, pool_pre_ping=True)


def to_local_naive(dt_value):
    """统一转换为本地 naive datetime，确保与 datetime.now() 可直接比较。"""
    if isinstance(dt_value, str):
        dt_value = datetime.fromisoformat(dt_value)
    if not isinstance(dt_value, datetime):
        raise TypeError(f"无法解析的时间戳类型: {type(dt_value)!r} 值: {dt_value!r}")
    if dt_value.tzinfo is not None:
        dt_value = dt_value.astimezone().replace(tzinfo=None)
    return dt_value


# ---------------- 条件引擎 ----------------

# 支持的时间格式（fromisoformat 之外的补充格式）
TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
]


def parse_time(value):
    """精准时间解析：支持 ISO 及常见格式，失败抛 ValueError。"""
    if isinstance(value, datetime):
        return to_local_naive(value)
    s = str(value).strip()
    try:
        return to_local_naive(datetime.fromisoformat(s))
    except ValueError:
        pass
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"无法解析时间值: {value!r}")


def eval_condition(row, cond, now=None):
    """评估单条条件。条件格式: {"column":.., "op":.., "value":..}（三元组，无值2）

    操作符:
      字符串模糊比对（大小写不敏感、忽略首尾空白）: eq / ne / contains
      时间精准比对（双方都解析为 datetime）: time_eq / time_before / time_after
      与当前时间差（分钟）: tdiff_gte / tdiff_lte / tdiff_eq
    """
    col = cond["column"]
    if col not in row:
        raise KeyError(col)
    cell = "" if row[col] is None else str(row[col]).strip()
    val = str(cond["value"]).strip()
    op = cond["op"]

    # 字符串模糊比对
    if op in ("eq", "ne"):
        try:  # 双方都解析为数字时按数值比较，增强鲁棒性（如 "5" 与 "5.0"）
            res = float(cell) == float(val)
        except ValueError:
            res = cell.lower() == val.lower()
        return res if op == "eq" else not res
    if op == "contains":
        return val.lower() in cell.lower()

    # 时间精准比对
    if op in ("time_eq", "time_before", "time_after"):
        t1 = parse_time(row[col])
        t2 = parse_time(val)
        return {"time_eq": t1 == t2, "time_before": t1 < t2,
                "time_after": t1 > t2}[op]

    # 与当前时间的绝对差值（分钟）
    if op in ("tdiff_gte", "tdiff_lte", "tdiff_eq"):
        t = parse_time(row[col])
        diff = abs(((now or datetime.now()) - t).total_seconds()) / 60
        target = float(val)
        if op == "tdiff_gte":
            return diff >= target
        if op == "tdiff_lte":
            return diff <= target
        return abs(diff - target) < 1  # tdiff_eq: 1 分钟容差

    raise ValueError(f"未知条件操作符: {op}")


def row_matches(row, conditions, combinator):
    """combinator 为 AND(且) 或 OR(或)。"""
    if not conditions:
        return False
    results = [eval_condition(row, c) for c in conditions]
    return all(results) if combinator == "AND" else any(results)


def send_notification(fields):
    """fields: 要在通知中展示的 {列名: 值} 字典。"""
    msg = "\n".join(f"{k}: {v}" for k, v in fields.items())
    toast = Notification(
        app_id="Data Monitor",
        title="⚠️ Data Alert",
        msg=msg[:250],  # toast 正文长度有限，截断保护
        duration="short",
    )
    toast.set_audio(audio.Default, loop=False)
    toast.show()


def check_once(cfg):
    """执行一次检查，返回本次触发的通知条数。"""
    sql = (cfg["query"].get("sql") or "").strip()
    if not sql:
        logging.error("配置文件缺少 query.sql")
        return 0
    conditions = cfg.get("conditions") or []
    if not conditions:
        logging.error("未配置任何触发条件")
        return 0
    combinator = cfg.get("combinator", "AND")
    notify_fields = cfg.get("notify_fields") or []

    # 数据库连接与查询
    try:
        engine = build_engine(cfg["database"])
        with engine.connect() as conn:
            rows = conn.execute(text(sql)).mappings().all()
        logging.info("查询返回 %d 行", len(rows))
    except Exception:
        logging.exception("数据库连接或查询失败")
        return 0

    alerted = 0
    missing_cols_logged = set()
    for row in rows:
        try:
            matched = row_matches(row, conditions, combinator)
        except KeyError as e:
            col = e.args[0]
            if col not in missing_cols_logged:
                logging.error("查询结果中不存在条件列 '%s'，请检查条件配置", col)
                missing_cols_logged.add(col)
            continue
        except (TypeError, ValueError):
            logging.warning("跳过无法评估的行: %s", dict(row))
            continue

        if matched:
            if notify_fields:
                fields = {k: row[k] for k in notify_fields if k in row} or dict(row)
            else:
                fields = dict(row)  # 未指定字段时通知整行
            try:
                send_notification(fields)
                alerted += 1
                logging.info("已通知: %s", fields)
            except Exception:
                logging.exception("发送通知失败")

    logging.info("本次检查结束，共触发 %d 条通知", alerted)
    return alerted


def main():
    minimize_console()
    setup_logging()
    logging.info("监控程序启动 (BASE_DIR=%s)", BASE_DIR)

    try:
        cfg = load_config(CONFIG_FILE)
    except Exception:
        logging.exception("读取配置失败")
        return 1

    interval = int(cfg.get("schedule", {}).get("interval_minutes", 5))
    logging.info("进入常驻循环，每 %d 分钟检查一次", interval)

    # 常驻后台循环：检查完即 sleep，空闲时几乎零 CPU 占用
    while True:
        try:
            cfg = load_config(CONFIG_FILE)  # 每轮重读，配置修改即时生效
            interval = int(cfg.get("schedule", {}).get("interval_minutes", 5))
            check_once(cfg)
        except Exception:
            logging.exception("本轮检查出现未预期错误")
        try:
            time.sleep(interval * 60)
        except KeyboardInterrupt:
            logging.info("收到退出信号，程序结束")
            return 0


if __name__ == "__main__":
    sys.exit(main())
