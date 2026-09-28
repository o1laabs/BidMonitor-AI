"""
数据存储模块 - 使用 SQLite 存储招标信息

优化说明（v1.1.1）：
- 使用线程本地存储复用数据库连接，提升性能
- 所有公开方法签名保持不变，完全向后兼容

v1.2.0 新增（本 fork）：
- **快照表 bid_snapshots**：每次抓取都留原文，解决"只存 URL 不存原文"的问题
- **内容感知去重**：unique_id 从 md5(url) 升级为 md5(url + content_hash)，
  使更正公告 / 延期公告能被识别为"内容变更"而非"重复"
- 完全向后兼容：旧库自动迁移，旧调用方式不变
"""
import sqlite3
import hashlib
import os
import threading
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass


# 快照触发原因
SNAPSHOT_NEW = "new"            # 首次抓取
SNAPSHOT_CHANGED = "changed"    # 内容发生变化（更正/延期等）
SNAPSHOT_UNCHANGED = "unchanged"  # 主动复查，内容未变


def content_hash(text: str) -> str:
    """对正文做归一化哈希：忽略空白差异，避免排版变化被误判为内容更新。"""
    if not text:
        return ""
    normalized = "".join(text.split())
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


@dataclass
class BidInfo:
    """招标信息数据类"""
    title: str
    url: str
    publish_date: str
    source: str
    content: str = ""
    purchaser: str = ""

    @property
    def unique_id(self) -> str:
        """生成唯一标识

        兼容说明：
        - content 为空时，退化为 md5(url)，与旧库行为完全一致；
        - content 非空时，使用 md5(url + content_hash)，
          使正文变化能被识别为独立记录（更正/延期公告不再被跳过）。
        """
        ch = content_hash(self.content)
        if not ch:
            return hashlib.md5(self.url.encode()).hexdigest()
        return hashlib.md5(f"{self.url}|{ch}".encode()).hexdigest()

    @property
    def url_id(self) -> str:
        """仅基于 URL 的标识（用于把同一 URL 的历史版本串起来）"""
        return hashlib.md5(self.url.encode()).hexdigest()


class Storage:
    """SQLite 数据存储类

    使用线程本地存储管理数据库连接，每个线程复用同一个连接，
    避免频繁创建和关闭连接带来的性能开销。
    """

    def __init__(self, db_path: str = "data/bids.db", snapshot_enabled: bool = True):
        self.db_path = db_path
        self.snapshot_enabled = snapshot_enabled
        # 线程本地存储，用于复用数据库连接
        self._local = threading.local()
        # 确保目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir:  # 处理相对路径情况
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接（复用机制）"""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        return self._local.conn

    def close(self):
        """关闭当前线程的数据库连接（用于清理资源）"""
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            try:
                self._local.conn.close()
            except:
                pass
            self._local.conn = None

    def _init_db(self):
        """初始化数据库表（含迁移）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    publish_date TEXT,
                    source TEXT,
                    content TEXT,
                    purchaser TEXT,
                    notified INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_unique_id ON bids(unique_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_notified ON bids(notified)
            """)

            # ---- 迁移：老库补 url_id 列，把历史数据关联到 URL 维度 ----
            cols = {row[1] for row in cursor.execute("PRAGMA table_info(bids)")}
            if "url_id" not in cols:
                cursor.execute("ALTER TABLE bids ADD COLUMN url_id TEXT")
                cursor.execute("""
                    UPDATE bids SET url_id = unique_id WHERE url_id IS NULL
                """)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_url_id ON bids(url_id)"
                )

            # ---- 快照表 ----
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bid_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    unique_id TEXT,
                    title TEXT,
                    content TEXT,
                    content_hash TEXT,
                    http_status INTEGER,
                    raw_html TEXT,
                    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    fetch_reason TEXT
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_snap_url_id
                ON bid_snapshots(url_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_snap_hash
                ON bid_snapshots(content_hash)
            """)
            conn.commit()

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def save_snapshot(self, bid: BidInfo, reason: str = SNAPSHOT_NEW,
                      raw_html: Optional[str] = None,
                      http_status: Optional[int] = None) -> int:
        """为一条招标信息存一份快照，返回快照 id。

        每次都写入（不做去重），这样才能构成完整的时间序列；
        去重逻辑由调用方通过 last_snapshot_hash() 判断。
        """
        if not self.snapshot_enabled:
            return -1
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bid_snapshots
                (url_id, url, unique_id, title, content, content_hash,
                 http_status, raw_html, fetch_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bid.url_id, bid.url, bid.unique_id, bid.title, bid.content,
            content_hash(bid.content), http_status, raw_html, reason
        ))
        conn.commit()
        return cursor.lastrowid

    def last_snapshot_hash(self, url: str) -> Optional[str]:
        """取该 URL 最近一次快照的 content_hash（无则 None）"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT content_hash FROM bid_snapshots
            WHERE url_id = ? ORDER BY id DESC LIMIT 1
        """, (hashlib.md5(url.encode()).hexdigest(),))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_snapshots(self, url: str, limit: int = 50) -> List[Dict[str, Any]]:
        """取某 URL 的快照历史（倒序），用于查看公告的历次变更"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, title, content_hash, fetched_at, fetch_reason, url
            FROM bid_snapshots WHERE url_id = ?
            ORDER BY id DESC LIMIT ?
        """, (hashlib.md5(url.encode()).hexdigest(), limit))
        keys = ("id", "title", "content_hash", "fetched_at", "fetch_reason", "url")
        return [dict(zip(keys, row)) for row in cursor.fetchall()]

    def snapshot_count(self) -> int:
        """快照总数"""
        conn = self._get_connection()
        return conn.cursor().execute(
            "SELECT COUNT(*) FROM bid_snapshots").fetchone()[0]

    # ------------------------------------------------------------------
    # 原有接口（签名不变）
    # ------------------------------------------------------------------

    def exists(self, bid: BidInfo) -> bool:
        """检查招标信息是否已存在"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM bids WHERE unique_id = ?",
            (bid.unique_id,)
        )
        return cursor.fetchone() is not None

    def url_exists(self, url: str) -> bool:
        """该 URL 是否已经抓过（任意版本）"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM bids WHERE url_id = ?",
            (hashlib.md5(url.encode()).hexdigest(),)
        )
        return cursor.fetchone() is not None

    def save(self, bid: BidInfo, notified: bool = False,
             snapshot: bool = True) -> bool:
        """保存招标信息，返回是否成功（新记录为True，重复为False）

        Args:
            bid: 招标信息
            notified: 是否已通知
            snapshot: 是否同时写入快照（默认 True）
        """
        if self.exists(bid):
            # 已存在同版本内容，仍然记一份快照（便于留痕），返回 False
            if snapshot:
                self.save_snapshot(bid, reason=SNAPSHOT_UNCHANGED)
            return False

        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bids
                (unique_id, url_id, title, url, publish_date, source,
                 content, purchaser, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bid.unique_id,
            bid.url_id,
            bid.title,
            bid.url,
            bid.publish_date,
            bid.source,
            bid.content,
            bid.purchaser,
            1 if notified else 0
        ))
        conn.commit()

        if snapshot:
            self.save_snapshot(bid, reason=SNAPSHOT_NEW)
        return True

    def save_or_update(self, bid: BidInfo, notified: bool = False) -> str:
        """保存并判断是新增还是内容变更。

        Returns:
            "new"       首次出现
            "changed"   同一 URL，但正文内容变了（更正/延期/补充）
            "unchanged" 同一 URL 且内容未变

        这是给 monitor_core 用的推荐入口：它把"是否已存在"的语义
        从"URL 重复"细化为"内容是否真的变了"。
        """
        prev = self.last_snapshot_hash(bid.url)

        if not self.url_exists(bid.url):
            self.save(bid, notified=notified, snapshot=False)
            self.save_snapshot(bid, reason=SNAPSHOT_NEW)
            return "new"

        cur = content_hash(bid.content)
        if prev is None or cur != prev:
            # 内容变了：作为新版本入库（unique_id 含 content_hash，不会冲突）
            if not self.exists(bid):
                conn = self._get_connection()
                conn.cursor().execute("""
                    INSERT INTO bids
                        (unique_id, url_id, title, url, publish_date, source,
                         content, purchaser, notified)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (bid.unique_id, bid.url_id, bid.title, bid.url,
                      bid.publish_date, bid.source, bid.content,
                      bid.purchaser, 1 if notified else 0))
                conn.commit()
            self.save_snapshot(bid, reason=SNAPSHOT_CHANGED)
            return "changed"

        self.save_snapshot(bid, reason=SNAPSHOT_UNCHANGED)
        return "unchanged"

    def mark_notified(self, bids):
        """标记招标信息已发送通知

        Args:
            bids: 可以是单个BidInfo、BidInfo列表、或URL列表
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # 处理不同输入类型
        if isinstance(bids, BidInfo):
            # 单个BidInfo对象
            cursor.execute(
                "UPDATE bids SET notified = 1 WHERE unique_id = ?",
                (bids.unique_id,)
            )
        elif isinstance(bids, list) and len(bids) > 0:
            if isinstance(bids[0], BidInfo):
                # BidInfo列表
                for bid in bids:
                    cursor.execute(
                        "UPDATE bids SET notified = 1 WHERE unique_id = ?",
                        (bid.unique_id,)
                    )
            elif isinstance(bids[0], str):
                # URL列表：只按 URL 匹配最新一条，避免误标历史版本
                for url in bids:
                    unique_id = hashlib.md5(url.encode()).hexdigest()
                    cursor.execute(
                        """UPDATE bids SET notified = 1
                           WHERE unique_id = ? OR url_id = ?""",
                        (unique_id, unique_id)
                    )

        conn.commit()

    def _row_to_bid(self, row) -> BidInfo:
        return BidInfo(
            title=row[0], url=row[1], publish_date=row[2],
            source=row[3], content=row[4], purchaser=row[5]
        )

    def get_unnotified(self) -> List[BidInfo]:
        """获取未通知的招标信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title, url, publish_date, source, content, purchaser
            FROM bids WHERE notified = 0
        """)
        return [self._row_to_bid(r) for r in cursor.fetchall()]

    def get_recent(self, days: int = 7) -> List[BidInfo]:
        """获取最近几天的招标信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title, url, publish_date, source, content, purchaser
            FROM bids
            WHERE datetime(created_at) > datetime('now', ?)
            ORDER BY created_at DESC
        """, (f'-{days} days',))
        return [self._row_to_bid(r) for r in cursor.fetchall()]

    def get_all(self) -> List[BidInfo]:
        """获取所有招标信息"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT title, url, publish_date, source, content, purchaser
            FROM bids
            ORDER BY created_at DESC
        """)
        return [self._row_to_bid(r) for r in cursor.fetchall()]

    def count_all(self) -> int:
        """获取总记录数"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM bids")
        return cursor.fetchone()[0]

    def clear_all(self):
        """清空所有数据"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bids")
        conn.commit()
