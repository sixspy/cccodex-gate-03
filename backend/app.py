import os
from datetime import date

from flask import Flask, jsonify, request

from auth import authenticate, issue_token, read_user
from daycut import RULE_CHOICES, RULE_CUTOFF, parse_cutoff, rule_label
from db import (
    connect,
    daycut_rows,
    get_settings,
    init_db,
    pin_settings,
    rebuild_day,
    record_rule_mismatch,
    settle_scan,
)

app = Flask(__name__)
app.json.ensure_ascii = False


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _user():
    return read_user(request.headers.get("Authorization"))


def _settings_json(row):
    if not row:
        return {"pinned": False}
    return {
        "pinned": True,
        "rule": row["rule"],
        "rule_name": rule_label(row),
        "cutoff": row["cutoff_time"].strftime("%H:%M") if row["cutoff_time"] else None,
        "pinned_by": row["pinned_by"],
        "pinned_at": _stamp(row["pinned_at"]),
        "pinned_date": row["pinned_date"].isoformat(),
        "locked_today": row["pinned_date"] == date.today(),
    }


def _daycut_json(item):
    return {
        "id": item["id"],
        "terminal_code": item["terminal_code"],
        "raw_payload": item["payload"],
        "recorded_date": item["biz_date"].isoformat(),
        "scan_at": _stamp(item["stamp"]),
        "scan_note": item["stamp_note"],
        "natural_date": item["natural_date"].isoformat() if item["natural_date"] else None,
        "cut_date": item["cut_date"].isoformat() if item["cut_date"] else None,
        "cross": item["cross"],
    }


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.biz_date,
                   (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
                   (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
                   EXISTS(SELECT 1 FROM build_runs r
                          WHERE r.biz_date = d.biz_date AND r.status = 'ok') AS built
            FROM (SELECT DISTINCT biz_date FROM ods_scan) d
            ORDER BY d.biz_date
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
    return jsonify(rows)


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        settings = get_settings(conn)
        ods = [_daycut_json(item) for item in daycut_rows(conn, settings, biz_date)]
        moves = conn.execute(
            """
            SELECT source_id, terminal_code, container_no, event_kind, event_at
            FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id
            """,
            (biz_date,),
        ).fetchall()
        run = conn.execute(
            """
            SELECT id, accepted_count, rejected_count, built_by, built_at, status
            FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
            """,
            (biz_date,),
        ).fetchone()
        rejects = []
        if run:
            rejects = conn.execute(
                """
                SELECT source_id, reason FROM build_rejects
                WHERE run_id = %s ORDER BY source_id
                """,
                (run["id"],),
            ).fetchall()
    if not ods:
        return jsonify({"detail": "这一天没有贴源扫描"}), 404
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
    if run:
        run["built_at"] = _stamp(run["built_at"])
    return jsonify(
        {
            "biz_date": biz_date,
            "settings": _settings_json(settings),
            "ods": ods,
            "moves": moves,
            "run": run,
            "rejects": rejects,
        }
    )


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user = _user()
    if not user:
        return jsonify({"detail": "未登录"}), 401
    if user["role"] != "writer":
        return jsonify({"detail": "核对员不能构建明细层"}), 403
    body = request.get_json(silent=True) or {}
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        settings = get_settings(conn)
        if not settings:
            return jsonify({"detail": "日切尚未钉住，不许开始构建"}), 409
        carried_rule = str(body.get("rule") or "").strip()
        carried_cutoff = parse_cutoff(body.get("cutoff"))
        same_rule = carried_rule == settings["rule"] and (
            settings["rule"] != RULE_CUTOFF or carried_cutoff == settings["cutoff_time"]
        )
        if not same_rule:
            record_rule_mismatch(conn, biz_date, user["username"])
            conn.commit()
            return jsonify({"detail": "日切规则不符，这批不入库"}), 409
        rebuild_day(conn, biz_date, user["username"])
        conn.commit()
    return date_detail(biz_date)


@app.get("/api/daycut/overview")
def daycut_overview():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        settings = get_settings(conn)
        rows = [_daycut_json(item) for item in daycut_rows(conn, settings)]
        contentions = conn.execute(
            """
            SELECT c.id, c.scan_id, c.wanted_date, c.expected_date,
                   c.attempted_by, c.attempted_at, c.note, o.terminal_code
            FROM daycut_contention c
            JOIN ods_scan o ON o.id = c.scan_id
            ORDER BY c.id DESC
            LIMIT 50
            """
        ).fetchall()
    for row in contentions:
        row["wanted_date"] = row["wanted_date"].isoformat()
        row["expected_date"] = row["expected_date"].isoformat()
        row["attempted_at"] = _stamp(row["attempted_at"])
    return jsonify(
        {
            "settings": _settings_json(settings),
            "rows": rows,
            "contentions": contentions,
        }
    )


@app.put("/api/daycut/settings")
def put_daycut_settings():
    user = _user()
    if not user:
        return jsonify({"detail": "未登录"}), 401
    if user["role"] != "writer":
        return jsonify({"detail": "核对员不能钉日切规则"}), 403
    body = request.get_json(silent=True) or {}
    rule = str(body.get("rule") or "").strip()
    if rule not in RULE_CHOICES:
        return jsonify({"detail": "日切规则只能二选一：落入自然日或过线改记次日"}), 400
    cutoff = None
    if rule == RULE_CUTOFF:
        cutoff = parse_cutoff(body.get("cutoff"))
        if cutoff is None:
            return jsonify({"detail": "日切线要按 HH:MM 给出"}), 400
    with connect() as conn:
        error = pin_settings(conn, rule, cutoff, user["username"], date.today())
        if error:
            return jsonify({"detail": error}), 409
        conn.commit()
        settings = get_settings(conn)
    return jsonify(_settings_json(settings))


@app.post("/api/daycut/queue/<int:scan_id>/settle")
def settle_queue_item(scan_id):
    user = _user()
    if not user:
        return jsonify({"detail": "未登录"}), 401
    if user["role"] != "writer":
        return jsonify({"detail": "核对员不能定业务日"}), 403
    body = request.get_json(silent=True) or {}
    try:
        wanted = date.fromisoformat(str(body.get("biz_date") or ""))
    except ValueError:
        return jsonify({"detail": "业务日要按 YYYY-MM-DD 给出"}), 400
    with connect() as conn:
        error, status = settle_scan(conn, scan_id, wanted, user["username"])
        conn.commit()
        if error:
            return jsonify({"detail": error}), status
    return daycut_overview()


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
