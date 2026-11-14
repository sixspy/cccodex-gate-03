import json
import os
import time

import psycopg
from psycopg.rows import dict_row

from build import clean_scan
from daycut import compute_day, scan_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS build_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    built_by TEXT NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES build_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daycut_settings (
    id SMALLINT PRIMARY KEY CHECK (id = 1),
    rule TEXT NOT NULL,
    cutoff_time TIME,
    pinned_by TEXT NOT NULL,
    pinned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pinned_date DATE NOT NULL
);

CREATE TABLE IF NOT EXISTS daycut_contention (
    id BIGSERIAL PRIMARY KEY,
    scan_id BIGINT NOT NULL REFERENCES ods_scan(id),
    wanted_date DATE NOT NULL,
    expected_date DATE NOT NULL,
    attempted_by TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note TEXT NOT NULL DEFAULT '日切争用'
);

ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ok';
"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _land(conn, biz_date, terminal, payload):
    conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, raw_payload)
        VALUES (%s, %s, %s::jsonb)
        """,
        (biz_date, terminal, json.dumps(payload, ensure_ascii=False)),
    )


def _seed(conn):
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1"},
    ]
    waiting = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2"},
        # 跨日：时刻落在 11-13，闸口记在了 11-12
        {"scan": "OUT", "box": "TCLU5566778", "at": "2026-11-13T00:18:00", "gate": "T2"},
        # 过 23:00 日切线，规则是过线改记次日时要进跨日队列
        {"scan": "IN", "box": "MSKU9081726", "at": "2026-11-12T23:40:00", "gate": "T1"},
        # 时刻空着，不进跨日队列
        {"scan": "RS", "box": "CSNU1029384", "at": "", "gate": "T1"},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload)
    for payload in waiting:
        _land(conn, "2026-11-12", payload["gate"], payload)
    rebuild_day(conn, "2026-11-11", "admin")


def rebuild_day(conn, biz_date: str, username: str):
    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    rows = conn.execute(
        "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    run = conn.execute(
        """
        INSERT INTO build_runs (biz_date, accepted_count, rejected_count, built_by)
        VALUES (%s, 0, 0, %s)
        RETURNING id
        """,
        (biz_date, username),
    ).fetchone()
    accepted = 0
    rejected = 0
    for row in rows:
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        cleaned, reason = clean_scan(payload)
        if reason:
            conn.execute(
                "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
                (run["id"], row["id"], reason),
            )
            rejected += 1
            continue
        conn.execute(
            """
            INSERT INTO dwd_move (biz_date, source_id, terminal_code, container_no, event_kind, event_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                biz_date,
                row["id"],
                row["terminal_code"],
                cleaned["container_no"],
                cleaned["event_kind"],
                cleaned["event_at"],
            ),
        )
        accepted += 1
    conn.execute(
        "UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s",
        (accepted, rejected, run["id"]),
    )
    return run["id"]


def get_settings(conn):
    return conn.execute(
        "SELECT rule, cutoff_time, pinned_by, pinned_at, pinned_date FROM daycut_settings WHERE id = 1"
    ).fetchone()


def pin_settings(conn, rule, cutoff, username, today):
    """钉死日切规则。咨询锁排队，写进设置后当天不许再改。"""
    conn.execute("SELECT pg_advisory_xact_lock(821203)")
    row = conn.execute("SELECT pinned_date FROM daycut_settings WHERE id = 1").fetchone()
    if row and row["pinned_date"] == today:
        return "规则写进日切设置后，当天不许再改"
    conn.execute(
        """
        INSERT INTO daycut_settings (id, rule, cutoff_time, pinned_by, pinned_at, pinned_date)
        VALUES (1, %s, %s, %s, NOW(), %s)
        ON CONFLICT (id) DO UPDATE SET
            rule = EXCLUDED.rule,
            cutoff_time = EXCLUDED.cutoff_time,
            pinned_by = EXCLUDED.pinned_by,
            pinned_at = NOW(),
            pinned_date = EXCLUDED.pinned_date
        """,
        (rule, cutoff, username, today),
    )
    return None


def daycut_rows(conn, settings, biz_date=None):
    """日切列表：每条扫描的落入日、改记日、记录日。时刻空着的不算改记日。"""
    sql = "SELECT id, biz_date, terminal_code, raw_payload FROM ods_scan"
    params = ()
    if biz_date is not None:
        sql += " WHERE biz_date = %s"
        params = (biz_date,)
    sql += " ORDER BY id"
    items = []
    for row in conn.execute(sql, params).fetchall():
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        stamp, why = scan_time(payload)
        cut = compute_day(settings, stamp) if settings and stamp else None
        items.append(
            {
                "id": row["id"],
                "terminal_code": row["terminal_code"],
                "payload": payload,
                "biz_date": row["biz_date"],
                "stamp": stamp,
                "stamp_note": why,
                "natural_date": stamp.date() if stamp else None,
                "cut_date": cut,
                "cross": bool(cut and cut != row["biz_date"]),
            }
        )
    return items


def settle_scan(conn, scan_id, wanted, username):
    """给跨日扫描定业务日。行锁排队，只放行与日切设置一致的那次。"""
    settings = get_settings(conn)
    if not settings:
        return "日切尚未钉住，不许开始定业务日", 409
    row = conn.execute(
        "SELECT id, biz_date, raw_payload FROM ods_scan WHERE id = %s FOR UPDATE",
        (scan_id,),
    ).fetchone()
    if not row:
        return "这条扫描不存在", 404
    payload = row["raw_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    stamp, why = scan_time(payload)
    if not stamp:
        return f"{why}，不进跨日队列", 400
    expected = compute_day(settings, stamp)
    if wanted != expected or row["biz_date"] == expected:
        conn.execute(
            """
            INSERT INTO daycut_contention (scan_id, wanted_date, expected_date, attempted_by)
            VALUES (%s, %s, %s, %s)
            """,
            (scan_id, wanted, expected, username),
        )
        return "日切争用", 409
    conn.execute("UPDATE ods_scan SET biz_date = %s WHERE id = %s", (expected, scan_id))
    return None, 200


def record_rule_mismatch(conn, biz_date, username):
    """构建携带的规则和日切设置不一致：整批记日切规则不符，不入明细。"""
    rows = conn.execute(
        "SELECT id FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    run = conn.execute(
        """
        INSERT INTO build_runs (biz_date, accepted_count, rejected_count, built_by, status)
        VALUES (%s, 0, %s, %s, 'rule_mismatch')
        RETURNING id
        """,
        (biz_date, len(rows), username),
    ).fetchone()
    for row in rows:
        conn.execute(
            "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, '日切规则不符')",
            (run["id"], row["id"]),
        )
