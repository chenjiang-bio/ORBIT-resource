#!/usr/bin/env python3
"""Stream a build_kg.py SQLite snapshot into Neo4j.

A full JSON export can exceed 20 GB and OOM a typical workstation if loaded with
``json.load``. This importer reads SQLite with a cursor in batches so memory use
does not scale with graph size.

Usage::

    python3 import_sqlite_to_neo4j.py \\
        --sqlite /path/organoid_kg.sqlite \\
        --uri bolt://localhost:7687 \\
        --user neo4j --password <pwd> \\
        --clear --batch-size 5000

Safety:

- ``--clear`` runs only after the Neo4j driver connects and SQLite passes a
  basic integrity check.
- Clear + import happen in one process; planned node/edge totals are printed
  before writing.
- Omit ``--clear`` for resumable ``MERGE`` imports.
"""

import argparse
import json
import sqlite3
import sys
import time

from neo4j import GraphDatabase


def parse_props(raw):
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, list):
            # Neo4j 属性只接受同类标量数组；复杂元素序列化为字符串
            if all(isinstance(x, (str, int, float, bool)) for x in v):
                out[k] = v
            else:
                out[k] = [json.dumps(x, ensure_ascii=False) for x in v]
        else:
            out[k] = json.dumps(v, ensure_ascii=False)
    return out


def sanitize_label(t):
    """Neo4j 标签不能参数化，这里限制为安全字符集后内联。"""
    s = "".join(ch for ch in str(t) if ch.isalnum() or ch == "_")
    return s or "Unknown"


def batched_nodes(con, batch_size):
    cur = con.cursor()
    cur.execute("SELECT id, type, properties FROM nodes")
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            return
        by_label = {}
        for nid, ntype, props in rows:
            p = parse_props(props)
            p["id"] = nid
            by_label.setdefault(sanitize_label(ntype), []).append(p)
        yield by_label, len(rows)


def batched_edges(con, batch_size):
    """按 (源标签, 目标标签, 关系) 分组产出边。

    两端标签必须带上，否则 Cypher 里的 MATCH (a {id: ...}) 用不到按标签建立的
    唯一约束索引，Neo4j 会退化成 AllNodesScan——680 万条边根本跑不完。
    这里让 SQLite 侧完成 join（nodes.id 是主键，走索引），Python 侧不驻留
    id→type 映射，内存占用与图规模无关。
    """
    cur = con.cursor()
    cur.execute(
        "SELECT e.source, e.target, e.relation, e.properties, "
        "       ns.type AS src_type, nt.type AS tgt_type "
        "FROM edges e "
        "JOIN nodes ns ON ns.id = e.source "
        "JOIN nodes nt ON nt.id = e.target")
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            return
        by_key = {}
        for src, tgt, rel, props, st, tt in rows:
            key = (sanitize_label(st), sanitize_label(tt), sanitize_label(rel))
            by_key.setdefault(key, []).append(
                {"source": src, "target": tgt, "props": parse_props(props)})
        yield by_key, len(rows)


def main():
    ap = argparse.ArgumentParser(description="Stream a KG SQLite export into Neo4j")
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--uri", default="bolt://192.168.10.125:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", required=True)
    ap.add_argument("--database", default="neo4j")
    ap.add_argument("--batch-size", type=int, default=2000,
                    help="每个写入事务的行数（线上事务内存限额较小，默认 2000）")
    ap.add_argument("--skip-nodes", type=int, default=0,
                    help="跳过前 N 个节点（断点续传用；MERGE 幂等，跳过只为省时间）")
    ap.add_argument("--skip-edges", type=int, default=0,
                    help="跳过前 N 条边（断点续传用）")
    ap.add_argument("--clear", action="store_true",
                    help="清空目标库后再导入（不可逆，需显式指定）")
    ap.add_argument("--clear-batch", type=int, default=10000,
                    help="清库时每个事务删除的节点数。线上 "
                         "dbms.memory.transaction.total.max 仅 716.8MiB，"
                         "DETACH DELETE 高度节点时批次过大会撞破限额（默认 10000）")
    args = ap.parse_args()

    con = sqlite3.connect("file:%s?mode=ro" % args.sqlite, uri=True)
    cur = con.cursor()
    n_nodes = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    n_edges = cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    labels = [r[0] for r in cur.execute("SELECT DISTINCT type FROM nodes")]
    rels = [r[0] for r in cur.execute("SELECT DISTINCT relation FROM edges")]
    print("SQLite 快照: %s" % args.sqlite)
    print("  节点 %d（%d 类） / 边 %d（%d 类）" % (n_nodes, len(labels), n_edges, len(rels)))
    if n_nodes == 0 or n_edges == 0:
        sys.exit("快照为空，中止（不会清库）。")

    # max_transaction_retry_time 决定托管事务在瞬时错误/连接断开后的重试窗口。
    # 实测该链路会丢弃长连接（ServiceUnavailable: Failed to write data to
    # connection），托管事务配合较长窗口可自动重连续跑，而不是整个导入失败。
    drv = GraphDatabase.driver(args.uri, auth=(args.user, args.password),
                               connection_timeout=30,
                               max_transaction_retry_time=180,
                               keep_alive=True)
    drv.verify_connectivity()
    print("  已连接 Neo4j: %s" % args.uri)

    with drv.session(database=args.database) as s:
        before = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        print("  目标库当前节点数: %d" % before)

        if args.clear:
            print("\n清空目标库…（批次 %d）" % args.clear_batch)
            t0 = time.time()
            removed = 0
            batch = args.clear_batch
            while True:
                try:
                    res = s.run(
                        "MATCH (n) WITH n LIMIT $lim DETACH DELETE n "
                        "RETURN count(n) AS c", lim=batch).single()
                except Exception as exc:
                    # 撞破事务内存限额时对半减小批次重试，而不是整个导入失败
                    if batch > 500:
                        batch = max(500, batch // 2)
                        print("  [WARN] 删除批次过大（%s），降为 %d 重试"
                              % (str(exc)[:70], batch))
                        continue
                    raise
                if not res or res["c"] == 0:
                    break
                removed += res["c"]
                if removed % 200000 < batch:
                    print("  已删除 %d 节点  %.0f 秒" % (removed, time.time() - t0))
            print("  已清空 %d 节点，耗时 %.1f 秒" % (removed, time.time() - t0))

        # 稳定 id 唯一约束，兼作 MERGE 的索引
        for lab in labels:
            try:
                s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (n:%s) REQUIRE n.id IS UNIQUE"
                      % sanitize_label(lab))
            except Exception as e:
                print("  [WARN] 约束创建失败 %s: %s" % (lab, str(e)[:80]))

        print("\n导入节点…")
        done = t_last = 0
        t0 = time.time()
        for by_label, n in batched_nodes(con, args.batch_size):
            if done + n <= args.skip_nodes:
                done += n
                continue
            for lab, items in by_label.items():
                cy = ("UNWIND $rows AS row MERGE (n:%s {id: row.id}) "
                      "SET n += row" % lab)
                s.execute_write(lambda tx, c=cy, it=items: tx.run(c, rows=it).consume())
            done += n
            if done - t_last >= 100000 or done == n_nodes:
                t_last = done
                print("  %d/%d 节点  %.0f 秒" % (done, n_nodes, time.time() - t0))

        print("\n导入边…")
        done = t_last = 0
        t0 = time.time()
        for by_key, n in batched_edges(con, args.batch_size):
            if done + n <= args.skip_edges:
                done += n
                continue
            for (st, tt, rel), items in by_key.items():
                cy = ("UNWIND $rows AS row "
                      "MATCH (a:%s {id: row.source}) "
                      "MATCH (b:%s {id: row.target}) "
                      "MERGE (a)-[r:%s]->(b) SET r += row.props" % (st, tt, rel))
                s.execute_write(lambda tx, c=cy, it=items: tx.run(c, rows=it).consume())
            done += n
            if done - t_last >= 100000 or done == n_edges:
                t_last = done
                print("  %d/%d 边  %.0f 秒" % (done, n_edges, time.time() - t0))

        after_n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        after_r = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print("\n导入完成：Neo4j 现有 %d 节点 / %d 关系" % (after_n, after_r))
        print("快照期望：      %d 节点 / %d 关系" % (n_nodes, n_edges))
        ok = (after_n == n_nodes and after_r == n_edges)
        print("一致性: %s" % ("OK" if ok else "不一致，需排查"))

    drv.close()
    con.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
