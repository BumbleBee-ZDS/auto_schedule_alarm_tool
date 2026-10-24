# -*- coding: utf-8 -*-
"""
monitor_gui.py - 数据监控告警工具（可视化界面版）

功能：
- 可视化配置数据库连接、SQL 查询（仅允许 SELECT）
- 多触发条件管理：可新增/删除条件，条件间支持"且"(AND)/"或"(OR)组合
- 通知字段管理：可新增/删除要在通知中展示的列，留空则通知整行
- 点击"启动监控"后在后台线程中定时检查，窗口可最小化到系统托盘
"""

import json
import logging
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from sqlalchemy import text

from monitor import (
    CONFIG_FILE,
    LOG_FILE,
    build_engine,
    check_once,
    load_config,
    validate_select_only,
)

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

DRIVERS = [
    "mysql+pymysql",
    "postgresql+psycopg2",
    "mssql+pyodbc",
    "sqlite",
    "oracle+oracledb",
]

# 条件操作符: (界面显示名, 内部key)
OP_CHOICES = [
    ("等于（模糊匹配）", "eq"),
    ("不等于（模糊匹配）", "ne"),
    ("包含（模糊匹配）", "contains"),
    ("时间等于（精准）", "time_eq"),
    ("时间早于（精准）", "time_before"),
    ("时间晚于（精准）", "time_after"),
    ("与当前时间差≥X分钟", "tdiff_gte"),
    ("与当前时间差≤X分钟", "tdiff_lte"),
    ("与当前时间差≈X分钟", "tdiff_eq"),
]
OP_LABEL2KEY = {label: key for label, key in OP_CHOICES}
OP_KEY2LABEL = {key: label for label, key in OP_CHOICES}


class QueueHandler(logging.Handler):
    """把日志转发到队列，由 GUI 线程安全地显示。"""

    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        self.log_queue.put(self.format(record))


class MonitorApp:
    def __init__(self, root):
        self.root = root
        root.title("数据监控告警工具")
        root.geometry("760x860")

        self.log_queue = queue.Queue()
        self.stop_event = None
        self.monitor_thread = None
        self.tray_icon = None
        self.conditions = []      # [{"column","op","value","value2"}, ...]
        self.notify_fields = []   # ["col1", "col2", ...]

        self._build_widgets()
        self._load_config_to_fields()
        self._setup_logging()
        self._setup_tray()

        # 关闭窗口时最小化到托盘而不是退出
        root.protocol("WM_DELETE_WINDOW", self.minimize_to_tray)
        root.after(200, self._poll_log_queue)

        # 带 --auto-start 参数启动时自动进入监控（便于开机自启/自动化验证）
        if "--auto-start" in sys.argv:
            root.after(500, self.on_start)

    # ---------------- UI 构建 ----------------

    def _build_widgets(self):
        pad = {"padx": 8, "pady": 4}

        # 数据库连接
        db_frame = ttk.LabelFrame(self.root, text="数据库连接")
        db_frame.pack(fill="x", **pad)
        self.vars = {}
        fields = [
            ("driver", "驱动", 0, 0), ("host", "主机", 0, 2), ("port", "端口", 0, 4),
            ("user", "用户名", 1, 0), ("password", "密码", 1, 2),
            ("database", "数据库名", 1, 4),
        ]
        for key, label, r, c in fields:
            ttk.Label(db_frame, text=label).grid(row=r, column=c, sticky="w", padx=4, pady=2)
            if key == "driver":
                var = tk.StringVar()
                ttk.Combobox(db_frame, textvariable=var, values=DRIVERS,
                             width=17, state="readonly").grid(row=r, column=c + 1, padx=4, pady=2)
            else:
                var = tk.StringVar()
                show = "*" if key == "password" else ""
                ttk.Entry(db_frame, textvariable=var, width=19, show=show).grid(
                    row=r, column=c + 1, padx=4, pady=2)
            self.vars[key] = var

        # SQL 查询
        sql_frame = ttk.LabelFrame(self.root, text="SQL 查询（仅允许 SELECT；请在 SQL 中用 WHERE 过滤数据，避免全表加载）")
        sql_frame.pack(fill="both", expand=True, **pad)
        self.sql_text = scrolledtext.ScrolledText(sql_frame, height=5, font=("Consolas", 10))
        self.sql_text.pack(fill="both", expand=True, padx=4, pady=4)

        # 触发条件编辑器
        cond_frame = ttk.LabelFrame(self.root, text="触发条件（可多条，新增/删除）")
        cond_frame.pack(fill="x", **pad)

        editor = ttk.Frame(cond_frame)
        editor.pack(fill="x", padx=4, pady=2)
        ttk.Label(editor, text="字段").pack(side="left")
        self.cond_col = ttk.Entry(editor, width=14)
        self.cond_col.pack(side="left", padx=2)
        ttk.Label(editor, text="条件").pack(side="left")
        self.cond_op = ttk.Combobox(editor, values=[l for l, _ in OP_CHOICES],
                                    width=24, state="readonly")
        self.cond_op.current(0)
        self.cond_op.pack(side="left", padx=2)
        ttk.Label(editor, text="值").pack(side="left")
        self.cond_val = ttk.Entry(editor, width=22)
        self.cond_val.pack(side="left", padx=2)
        ttk.Button(editor, text="添加条件", command=self._add_condition).pack(side="left", padx=6)

        self.cond_tree = ttk.Treeview(cond_frame, columns=("column", "op", "value"),
                                      show="headings", height=4)
        for col, text_, w in (("column", "字段", 180), ("op", "条件", 240),
                              ("value", "值", 200)):
            self.cond_tree.heading(col, text=text_)
            self.cond_tree.column(col, width=w)
        self.cond_tree.pack(fill="x", padx=4, pady=2)

        cond_btns = ttk.Frame(cond_frame)
        cond_btns.pack(fill="x", padx=4, pady=2)
        ttk.Button(cond_btns, text="删除选中条件", command=self._del_condition).pack(side="left")
        ttk.Label(cond_btns, text="   多条件关系:").pack(side="left")
        self.combinator_var = tk.StringVar(value="AND")
        ttk.Radiobutton(cond_btns, text="且（全部满足）", variable=self.combinator_var,
                        value="AND").pack(side="left")
        ttk.Radiobutton(cond_btns, text="或（任一满足）", variable=self.combinator_var,
                        value="OR").pack(side="left")

        # 通知字段
        nf_frame = ttk.LabelFrame(self.root, text="通知字段（留空则通知整行数据）")
        nf_frame.pack(fill="x", **pad)
        nf_editor = ttk.Frame(nf_frame)
        nf_editor.pack(fill="x", padx=4, pady=2)
        ttk.Label(nf_editor, text="字段名").pack(side="left")
        self.nf_entry = ttk.Entry(nf_editor, width=16)
        self.nf_entry.pack(side="left", padx=2)
        ttk.Button(nf_editor, text="添加字段", command=self._add_field).pack(side="left", padx=4)
        ttk.Button(nf_editor, text="删除选中字段", command=self._del_field).pack(side="left", padx=4)
        self.nf_list = tk.Listbox(nf_frame, height=3)
        self.nf_list.pack(fill="x", padx=4, pady=2)

        # 调度
        sched_frame = ttk.LabelFrame(self.root, text="调度")
        sched_frame.pack(fill="x", **pad)
        ttk.Label(sched_frame, text="检查间隔(分钟)").pack(side="left", padx=4)
        self.interval_var = tk.StringVar(value="5")
        ttk.Entry(sched_frame, textvariable=self.interval_var, width=8).pack(side="left")

        # 按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", **pad)
        ttk.Button(btn_frame, text="测试查询", command=self.on_test).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="保存配置", command=self.on_save).pack(side="left", padx=4)
        self.start_btn = ttk.Button(btn_frame, text="启动监控", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_frame, text="停止监控", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btn_frame, text="最小化到托盘", command=self.minimize_to_tray).pack(side="right", padx=4)

        # 状态与日志
        self.status_var = tk.StringVar(value="状态: 未启动")
        ttk.Label(self.root, textvariable=self.status_var, foreground="#005a9e").pack(
            fill="x", padx=10, anchor="w")
        log_frame = ttk.LabelFrame(self.root, text="运行日志")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, height=8, state="disabled",
                                                  font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=4, pady=4)

    # ---------------- 条件与字段管理 ----------------

    def _add_condition(self):
        col = self.cond_col.get().strip()
        op = OP_LABEL2KEY[self.cond_op.get()]
        val = self.cond_val.get().strip()
        if not col or not val:
            messagebox.showerror("输入有误", "条件的字段和值不能为空")
            return
        if op.startswith("tdiff_"):
            try:
                if float(val) < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("输入有误", "时间差条件的值必须为非负数字（分钟）")
                return
        if op.startswith("time_"):
            from monitor import parse_time
            try:
                parse_time(val)
            except ValueError as e:
                messagebox.showerror("输入有误", str(e))
                return
        cond = {"column": col, "op": op, "value": val}
        self.conditions.append(cond)
        self.cond_tree.insert("", "end", values=(col, OP_KEY2LABEL[op], val))
        self.cond_col.delete(0, "end")
        self.cond_val.delete(0, "end")

    def _del_condition(self):
        for item in self.cond_tree.selection():
            idx = self.cond_tree.index(item)
            del self.conditions[idx]
            self.cond_tree.delete(item)

    def _add_field(self):
        name = self.nf_entry.get().strip()
        if name and name not in self.notify_fields:
            self.notify_fields.append(name)
            self.nf_list.insert("end", name)
        self.nf_entry.delete(0, "end")

    def _del_field(self):
        for i in reversed(self.nf_list.curselection()):
            self.nf_list.delete(i)
            del self.notify_fields[i]

    # ---------------- 配置读写 ----------------

    def _load_config_to_fields(self):
        try:
            cfg = load_config(CONFIG_FILE)
        except Exception:
            return
        db = cfg.get("database", {})
        for key in ("driver", "host", "port", "user", "password", "database"):
            if db.get(key) is not None:
                self.vars[key].set(str(db.get(key, "")))
        self.sql_text.delete("1.0", "end")
        self.sql_text.insert("1.0", cfg.get("query", {}).get("sql", ""))
        self.interval_var.set(str(cfg.get("schedule", {}).get("interval_minutes", 5)))
        self.combinator_var.set(cfg.get("combinator", "AND"))
        for cond in cfg.get("conditions", []):
            self.conditions.append(cond)
            self.cond_tree.insert("", "end", values=(
                cond["column"], OP_KEY2LABEL.get(cond["op"], cond["op"]),
                cond.get("value", "")))
        for f in cfg.get("notify_fields", []):
            self.notify_fields.append(f)
            self.nf_list.insert("end", f)

    def _collect_and_validate(self):
        """收集界面输入并校验，返回配置 dict；校验失败弹窗并返回 None。"""
        sql = self.sql_text.get("1.0", "end").strip()
        try:
            sql = validate_select_only(sql)
            interval = int(self.interval_var.get())
            if interval <= 0:
                raise ValueError("检查间隔必须大于 0")
            if not self.conditions:
                raise ValueError("请至少添加一条触发条件")
        except ValueError as e:
            messagebox.showerror("输入有误", str(e))
            return None

        return {
            "database": {
                "driver": self.vars["driver"].get() or "mysql+pymysql",
                "host": self.vars["host"].get(),
                "port": self.vars["port"].get(),
                "user": self.vars["user"].get(),
                "password": self.vars["password"].get(),
                "database": self.vars["database"].get(),
            },
            "query": {"sql": sql},
            "schedule": {"interval_minutes": interval},
            "combinator": self.combinator_var.get(),
            "conditions": self.conditions,
            "notify_fields": self.notify_fields,
        }

    def on_save(self):
        cfg = self._collect_and_validate()
        if cfg is None:
            return
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("保存成功", f"配置已保存到 {CONFIG_FILE}")

    def on_test(self):
        cfg = self._collect_and_validate()
        if cfg is None:
            return
        try:
            engine = build_engine(cfg["database"])
            with engine.connect() as conn:
                result = conn.execute(text(cfg["query"]["sql"]))
                rows = result.fetchmany(10)
                columns = list(result.keys())
        except Exception as e:
            messagebox.showerror("查询失败", str(e))
            return
        self._show_result_popup(columns, rows)

    def _show_result_popup(self, columns, rows):
        win = tk.Toplevel(self.root)
        win.title(f"测试查询结果（前 {len(rows)} 行）")
        win.geometry("640x300")
        tree = ttk.Treeview(win, columns=columns, show="headings")
        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=120)
        for row in rows:
            tree.insert("", "end", values=[str(v) for v in row])
        tree.pack(fill="both", expand=True)

    # ---------------- 监控控制 ----------------

    def on_start(self):
        cfg = self._collect_and_validate()
        if cfg is None:
            return
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self.stop_event = threading.Event()
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_var.set("状态: 监控运行中")
        logging.info("监控已启动")

    def on_stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_var.set("状态: 已停止")
        logging.info("监控已停止")

    def _monitor_loop(self):
        while not self.stop_event.is_set():
            try:
                cfg = load_config(CONFIG_FILE)  # 每轮重新读取，配置修改即时生效
                interval = int(cfg.get("schedule", {}).get("interval_minutes", 5))
                n = check_once(cfg)
                self.log_queue.put(f"__STATUS__本轮检查完成，触发 {n} 条通知，{interval} 分钟后再次检查")
            except Exception as e:
                interval = 5
                self.log_queue.put(f"__STATUS__本轮检查出错: {e}")
            self.stop_event.wait(interval * 60)  # 可被打断的等待，停止时立即响应

    # ---------------- 托盘与日志 ----------------

    def _setup_logging(self):
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                datefmt="%H:%M:%S")
        # 界面日志
        queue_handler = QueueHandler(self.log_queue)
        queue_handler.setFormatter(fmt)
        # 文件日志（后台运行时的排查依据）
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root_logger = logging.getLogger()
        root_logger.addHandler(queue_handler)
        root_logger.addHandler(file_handler)
        root_logger.setLevel(logging.INFO)

    def _poll_log_queue(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if msg.startswith("__STATUS__"):
                self.status_var.set("状态: " + msg[len("__STATUS__"):])
            else:
                self.log_text.config(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        self.root.after(200, self._poll_log_queue)

    def _make_tray_image(self):
        """程序生成托盘图标：红底白色时钟，无需外部图片文件。"""
        img = Image.new("RGB", (64, 64), "#c0392b")
        d = ImageDraw.Draw(img)
        d.ellipse((10, 10, 54, 54), outline="white", width=4)
        d.line((32, 32, 32, 18), fill="white", width=4)
        d.line((32, 32, 44, 38), fill="white", width=4)
        return img

    def _setup_tray(self):
        if not HAS_TRAY:
            return
        menu = pystray.Menu(
            pystray.MenuItem("打开主界面", lambda: self.root.after(0, self.restore_window)),
            pystray.MenuItem("退出", lambda: self.root.after(0, self.quit_app)),
        )
        self.tray_icon = pystray.Icon("DataMonitor", self._make_tray_image(),
                                      "数据监控告警工具", menu)
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def minimize_to_tray(self):
        if HAS_TRAY:
            self.root.withdraw()
        else:
            self.root.iconify()  # 无托盘库时退化为普通最小化

    def restore_window(self):
        self.root.deiconify()
        self.root.lift()

    def quit_app(self):
        if self.stop_event:
            self.stop_event.set()
        if self.tray_icon:
            self.tray_icon.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = MonitorApp(root)
    root.mainloop()
