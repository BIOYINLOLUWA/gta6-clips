"""Entry point.

  python -m pipeline.cli init-db     create tables
  python -m pipeline.cli produce     discover new sources -> detect -> render -> send to Telegram
  python -m pipeline.cli review      read Telegram button presses -> schedule approved clips
  python -m pipeline.cli status      counts by status
"""
from __future__ import annotations

import argparse
import logging
import shutil

from . import approve, db, detect, post, render, settings, source

log = logging.getLogger("pipeline")


def init_db() -> None:
    with db.conn() as c:
        c.execute((settings.ROOT / "db" / "schema.sql").read_text())
    print("tables ready")


def process_source(src: dict, wl: settings.Watchlist) -> int:
    work = settings.WORK_DIR / f"src_{src['id']}"
    try:
        audio = source.download_audio(src["url"], work)
        moments = detect.find_moments(audio, src["channel"], src["title"], src["campaign"],
                                      wl.long_cut_s, wl.clips_per_source)
        made = 0
        for i, m in enumerate(moments):
            section = source.download_section(src["url"], m.start, m.end, work, f"sec{i}")
            words = detect.transcribe(section, words=True)
            long_v, short_v = render.render_pair(section, words, "", src["layout"],
                                                 wl.long_cut_s, wl.short_cut_s, work, f"clip{i}")
            row = db.one(
                "INSERT INTO clips(source_id, start_s, end_s, score, transcript, hook, captions) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (src["id"], m.start, m.end, m.score, m.transcript, "", db.as_json(m.captions)))
            approve.send_for_review(row["id"], long_v, short_v)
            made += 1
        db.q("UPDATE clip_sources SET status='processed', processed_at=now() WHERE id=%s", (src["id"],))
        return made
    except Exception as exc:
        log.error("source %s failed: %r", src["id"], exc)
        log.exception("source %s traceback", src["id"])
        db.q("UPDATE clip_sources SET status='failed', block_reason=%s WHERE id=%s",
             (str(exc)[:500], src["id"]))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def produce(max_sources: int) -> None:
    wl = settings.load_watchlist()
    db.q("UPDATE clip_sources SET status='new', block_reason=NULL "
         "WHERE status='failed' AND discovered_at > now() - interval '3 days'")
    queued = source.discover(wl)
    log.info("%d new sources allowed", queued)
    total = 0
    for src in db.q("SELECT * FROM clip_sources WHERE status='new' ORDER BY discovered_at LIMIT %s",
                    (max_sources,)):
        total += process_source(src, wl)
    if total:
        approve.notify(f"🆕 {total} clips waiting for review. Tap ✅ 1/2/3 to pick a caption or ❌.")


def review() -> None:
    approved = approve.poll()
    log.info("%d newly approved", len(approved))
    n = post.schedule_approved()
    log.info("%d clips scheduled", n)


def hot() -> None:
    from . import hot as hot_mod
    hot_mod.run()


def status() -> None:
    for r in db.q("SELECT status, count(*) AS n FROM clips GROUP BY status ORDER BY status"):
        print(f"clips {r['status']:>15}: {r['n']}")
    for r in db.q("SELECT status, count(*) AS n FROM clip_sources GROUP BY status ORDER BY status"):
        print(f"sources {r['status']:>13}: {r['n']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    pr = sub.add_parser("produce")
    pr.add_argument("--max-sources", type=int, default=3)
    sub.add_parser("review")
    sub.add_parser("status")
    sub.add_parser("hot")
    a = p.parse_args()
    {"init-db": init_db, "review": review, "status": status, "hot": hot,
     "produce": lambda: produce(a.max_sources)}[a.cmd]()


if __name__ == "__main__":
    main()
