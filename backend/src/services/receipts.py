"""Receipt ledger: historical receipts live in monthly partitions of ``receipt_ledger``,
keyed by the month the *trial was started* — not by when the receipt was committed, so a
receipt submitted across a month boundary still lands in its original month.

Three concerns live here:

* ``write_receipt`` is the only path a receipt takes into storage. It runs on the caller's
  connection, so it commits or rolls back with the business transaction. A contender
  whose lock attempt times out therefore never leaves a receipt in either table.
* ``run_batch`` moves historical rows into the ledger with an id cursor. The cursor moves
  in the same transaction as the inserts and the insert is keyed by
  ``(period, trial_id, actor)``, so an interrupted run resumes where it stopped and a
  retried batch cannot double-book a row.
* ``switch`` / ``return_to_legacy`` change the active read ledger. Switching holds an
  exclusive advisory lock that every receipt writer holds shared, so a commit racing the
  final batch is either already visible to the reconcile or is still holding the lock and
  is waited for; rolled-back rows are invisible to the reconcile and cannot leak in.

Every read uses exactly one ledger (the active one), never a UNION: the evidence page for
a trial always comes from one complete account.
"""
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text

from src.config.database import engine

PERIOD_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
LEDGER_LOCK_KEY = 74112601


def utc_now():
    return datetime.now(timezone.utc)


def period_of(moment):
    """Month key of a trial start, always in UTC so a late commit keeps its origin month."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime('%Y-%m')


def _period_expression(alias='t'):
    return (f"COALESCE({alias}.period,to_char({alias}.created_at AT TIME ZONE 'UTC','YYYY-MM'))")


def _partition_name(period):
    if not PERIOD_RE.match(period or ''):
        raise HTTPException(422, '演练月份无效，无法归档回执')
    return 'receipt_ledger_' + period.replace('-', '_')


def _ensure_partition(db, period):
    """Create the monthly partition inside the caller's transaction.

    Used only by the serialised backfill path, which already holds the exclusive ledger
    lock and therefore cannot race another writer or a switch.
    """
    return _create_partition(db, period)


def _create_partition(db, period):
    """Create the partition if missing. The period is regex-validated, never bound in DDL."""
    name = _partition_name(period)
    db.execute(text(f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF receipt_ledger FOR VALUES IN ('{period}')"))
    return name


def ensure_partition_ddl(period):
    """Make sure this month's partition exists before a receipt transaction starts.

    Called ahead of the write path (trial creation, worker setup) so the receipt
    transactions themselves never run partition DDL: ``CREATE TABLE ... PARTITION OF``
    needs a lock on the parent that a concurrent in-flight insert would block, and a
    writer waiting on that while another writer waits on it would stall the trial.

    The DDL runs in its own short transaction, serialised by a per-period advisory lock
    and re-checked after taking it, so two processes racing on a brand-new month cannot
    both try to create it. The existence check is a cheap catalog lookup and is never
    cached, so a rebuilt database is noticed immediately.
    """
    name = _partition_name(period)
    if _partition_exists(name):
        return name
    for attempt in range(4):
        try:
            with engine.begin() as conn:
                conn.execute(text('SELECT pg_advisory_xact_lock(hashtext(:key))'), {'key': 'receipt-partition:' + period})
                if conn.execute(text('SELECT to_regclass(:name)'), {'name': 'public.' + name}).scalar() is None:
                    conn.execute(text("SET LOCAL lock_timeout = '8000ms'"))
                    conn.execute(text(f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF receipt_ledger FOR VALUES IN ('{period}')"))
            return name
        except Exception:
            if _partition_exists(name):
                return name
            if attempt == 3:
                raise
            time.sleep(0.1 * (attempt + 1))
    return name


def _partition_exists(name):
    with engine.connect() as conn:
        return conn.execute(text('SELECT to_regclass(:name)'), {'name': 'public.' + name}).scalar() is not None


def ensure_state(db):
    db.execute(text(
        'INSERT INTO receipt_migrations(id,status,active_ledger,cursor,batch_size,batches,error,updated_at) '
        "VALUES (1,'idle','receipts',0,200,0,'',now()) ON CONFLICT (id) DO NOTHING"
    ))


def state(db, lock=False):
    """Read the migration state; take the ledger advisory lock for the whole transaction.

    ``lock=True`` is exclusive (writer/migration paths), otherwise shared (every receipt
    write and every read that depends on which ledger is active). The read path performs
    no write once startup has seeded the singleton row.
    """
    fn = 'pg_advisory_xact_lock' if lock else 'pg_advisory_xact_lock_shared'
    db.execute(text(f'SELECT {fn}(:key)'), {'key': LEDGER_LOCK_KEY})
    columns = ('id,status,active_ledger,cursor,batch_size,batches,error,started_at,updated_at,'
               'activated_at,returned_at')
    row = db.execute(text(f'SELECT {columns} FROM receipt_migrations WHERE id=1')).mappings().one_or_none()
    if row is None:
        ensure_state(db)
        row = db.execute(text(f'SELECT {columns} FROM receipt_migrations WHERE id=1')).mappings().one()
    return row


def _trial_origin(db, trial_id):
    row = db.execute(text('SELECT period,created_at FROM trials WHERE id=:id'), {'id': trial_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, '演练不存在')
    period = row['period'] or period_of(row['created_at'])
    if not period:
        raise HTTPException(422, '演练缺少发起时间，无法归档回执')
    return period, row['created_at'] or utc_now()


def _upsert_ledger(db, period, trial_id, actor, quantity, origin, source_receipt_id, recorded_at):
    # The caller has already ensured the partition exists (outside this transaction for
    # live writes, under the exclusive ledger lock for backfill).
    return db.execute(text(
        'INSERT INTO receipt_ledger(period,trial_id,actor,quantity,origin,source_receipt_id,recorded_at) '
        'VALUES (:period,:trial_id,:actor,:quantity,:origin,:source,:recorded_at) '
        'ON CONFLICT (period,trial_id,actor) DO UPDATE SET '
        'quantity=EXCLUDED.quantity, '
        'source_receipt_id=COALESCE(receipt_ledger.source_receipt_id,EXCLUDED.source_receipt_id), '
        "origin=CASE WHEN receipt_ledger.origin='live' OR EXCLUDED.origin='live' THEN 'live' ELSE 'backfill' END "
        'RETURNING id'
    ), {'period': period, 'trial_id': trial_id, 'actor': actor, 'quantity': quantity,
        'origin': origin, 'source': source_receipt_id, 'recorded_at': recorded_at}).scalar()


def _insert_legacy(db, trial_id, actor, quantity):
    """Insert into the legacy table and return its id, reusing the row on a replay."""
    ident = db.execute(text(
        'INSERT INTO receipts(trial_id,actor,quantity) VALUES (:trial_id,:actor,:quantity) '
        'ON CONFLICT (trial_id,actor) DO NOTHING RETURNING id'
    ), {'trial_id': trial_id, 'actor': actor, 'quantity': quantity}).scalar()
    if ident is None:
        ident = db.execute(text(
            'SELECT id FROM receipts WHERE trial_id=:trial_id AND actor=:actor'
        ), {'trial_id': trial_id, 'actor': actor}).scalar()
    return ident


def write_receipt(db, trial_id, actor, quantity, idempotent=False):
    """Write one receipt on the caller's connection/transaction.

    The shared advisory lock is held until the caller's transaction ends: a switch
    waiting for the exclusive lock cannot flip the active ledger while this write is in
    flight, and this write cannot land in the wrong ledger after the flip. Shared locks
    do not block sibling writers, so concurrent trials still run side by side.
    """
    # Partition DDL runs on its own connection and must happen before this transaction
    # takes the shared ledger lock — otherwise that lock would block its own DDL.
    period, created_at = _trial_origin(db, trial_id)
    ensure_partition_ddl(period)

    current = state(db)

    if current['active_ledger'] == 'receipt_ledger':
        _upsert_ledger(db, period, trial_id, actor, quantity, 'live', None, utc_now())
        return _insert_legacy(db, trial_id, actor, quantity)
    if idempotent:
        existing = db.execute(text(
            'SELECT id FROM receipts WHERE trial_id=:trial_id AND actor=:actor'
        ), {'trial_id': trial_id, 'actor': actor}).scalar()
        if existing is not None:
            return existing
    ident = _insert_legacy(db, trial_id, actor, quantity)
    # While migrating (and after a return) the ledger is kept in step, so a later switch
    # or re-activation never loses a receipt written outside the new ledger's window.
    if current['status'] != 'idle':
        _upsert_ledger(db, period, trial_id, actor, quantity, 'live', ident, utc_now())
    return ident


def receipts_for_trial(db, trial_id):
    active = state(db)['active_ledger']
    if active == 'receipt_ledger':
        rows = db.execute(text(
            'SELECT id,trial_id,actor,quantity,period,origin,recorded_at FROM receipt_ledger '
            'WHERE trial_id=:trial_id ORDER BY id'
        ), {'trial_id': trial_id}).mappings().all()
    else:
        rows = db.execute(text(
            'SELECT id,trial_id,actor,quantity FROM receipts WHERE trial_id=:trial_id ORDER BY id'
        ), {'trial_id': trial_id}).mappings().all()
    return active, [dict(row) for row in rows]


def verification(db):
    """Compare legacy and ledger as multisets of (period, trial, actor, quantity).

    A legacy receipt whose trial has no start month cannot be placed in any partition;
    ``unplaced_count`` surfaces it so a switch refuses instead of silently dropping it.
    """
    counts = dict(db.execute(text(
        'WITH legacy_all AS ('
        f'  SELECT {_period_expression()} AS period,r.trial_id,r.actor,r.quantity '
        '  FROM receipts r JOIN trials t ON t.id=r.trial_id'
        '), legacy AS (SELECT * FROM legacy_all WHERE period IS NOT NULL), '
        'ledger AS (SELECT period,trial_id,actor,quantity FROM receipt_ledger) '
        'SELECT (SELECT count(*) FROM legacy) AS legacy_count,'
        '(SELECT count(*) FROM ledger) AS ledger_count,'
        '(SELECT count(*) FROM (SELECT * FROM legacy EXCEPT SELECT * FROM ledger) x) AS missing_count,'
        '(SELECT count(*) FROM (SELECT * FROM ledger EXCEPT SELECT * FROM legacy) y) AS extra_count,'
        '(SELECT count(*) FROM legacy_all WHERE period IS NULL) AS unplaced_count'
    )).mappings().one())
    counts['balanced'] = (counts['missing_count'] == 0 and counts['extra_count'] == 0
                          and counts['unplaced_count'] == 0)
    return counts


def summary(db, include_partitions=True):
    current = dict(state(db))
    data = {key: (value.isoformat() if hasattr(value, 'isoformat') else value) for key, value in current.items()}
    data.update(verification(db))
    if include_partitions:
        data['partitions'] = [dict(x) for x in db.execute(text(
            "SELECT c.relname AS name,pg_get_expr(c.relpartbound,c.oid) AS bound "
            'FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid '
            "JOIN pg_class p ON p.oid=i.inhparent WHERE p.relname='receipt_ledger' ORDER BY c.relname"
        )).mappings().all()]
    return data


def brief(db):
    """State only, for request paths that do not need a full ledger comparison."""
    current = dict(state(db))
    return {key: (value.isoformat() if hasattr(value, 'isoformat') else value) for key, value in current.items()}


def _ensure_periods(db):
    periods = db.execute(text(
        f'SELECT DISTINCT {_period_expression()} AS period FROM receipts r '
        'JOIN trials t ON t.id=r.trial_id WHERE t.created_at IS NOT NULL'
    )).scalars().all()
    for period in periods:
        if period:
            _ensure_partition(db, period)
    return periods


def reconcile_all(db):
    """Bring the ledger level with the legacy table; safe to repeat at any time."""
    _ensure_periods(db)
    result = db.execute(text(
        'INSERT INTO receipt_ledger(period,trial_id,actor,quantity,origin,source_receipt_id,recorded_at) '
        f"SELECT {_period_expression()},r.trial_id,r.actor,r.quantity,'backfill',r.id,t.created_at "
        'FROM receipts r JOIN trials t ON t.id=r.trial_id WHERE t.created_at IS NOT NULL '
        'ON CONFLICT (period,trial_id,actor) DO UPDATE SET '
        'quantity=EXCLUDED.quantity, '
        'source_receipt_id=COALESCE(receipt_ledger.source_receipt_id,EXCLUDED.source_receipt_id)'
    ))
    return result.rowcount or 0


def last_receipt_id(db):
    return db.execute(text('SELECT COALESCE(max(id),0) FROM receipts')).scalar()


def _ensure_periods_between(db, cursor, upper):
    periods = db.execute(text(
        f'SELECT DISTINCT {_period_expression()} AS period FROM receipts r '
        'JOIN trials t ON t.id=r.trial_id '
        'WHERE r.id>:cursor AND r.id<=:upper AND t.created_at IS NOT NULL'
    ), {'cursor': cursor, 'upper': upper}).scalars().all()
    for period in periods:
        if period:
            _ensure_partition(db, period)


def _reconcile_between(db, cursor, upper):
    """One set-based insert of a cursor range; keeps the exclusive lock short."""
    _ensure_periods_between(db, cursor, upper)
    return db.execute(text(
        'INSERT INTO receipt_ledger(period,trial_id,actor,quantity,origin,source_receipt_id,recorded_at) '
        f"SELECT {_period_expression()},r.trial_id,r.actor,r.quantity,'backfill',r.id,t.created_at "
        'FROM receipts r JOIN trials t ON t.id=r.trial_id '
        'WHERE r.id>:cursor AND r.id<=:upper AND t.created_at IS NOT NULL '
        'ON CONFLICT (period,trial_id,actor) DO UPDATE SET '
        'quantity=EXCLUDED.quantity, '
        'source_receipt_id=COALESCE(receipt_ledger.source_receipt_id,EXCLUDED.source_receipt_id)'
    ), {'cursor': cursor, 'upper': upper}).rowcount or 0


def _count_after(db, cursor):
    return db.execute(text(
        'SELECT count(*) FROM receipts r JOIN trials t ON t.id=r.trial_id '
        'WHERE r.id>:cursor AND t.created_at IS NOT NULL'
    ), {'cursor': cursor}).scalar()


def _set_cursor(db, cursor, size, status):
    db.execute(text(
        'UPDATE receipt_migrations SET status=:status,cursor=:cursor,batches=batches+1,'
        "batch_size=:size,error='',updated_at=now() WHERE id=1"
    ), {'status': status, 'cursor': cursor, 'size': size})


def run_batch(db, batch_size=None):
    """Move one id-cursor page. Caller commits; progress lands with the rows.

    The exclusive lock covers one page, so a switch is never starved and can never see a
    half-written page.
    """
    current = state(db, lock=True)
    if current['status'] == 'active':
        raise HTTPException(409, '新账本已启用；如需重新回填请先退回旧表')
    if current['status'] in ('idle', 'returned'):
        raise HTTPException(409, '请先发起迁移')
    size = max(1, min(int(batch_size or current['batch_size'] or 200), 2000))
    cursor = current['cursor'] or 0
    upper = db.execute(text(
        'SELECT max(id) FROM (SELECT r.id FROM receipts r JOIN trials t ON t.id=r.trial_id '
        'WHERE r.id>:cursor AND t.created_at IS NOT NULL ORDER BY r.id LIMIT :size) page'
    ), {'cursor': cursor, 'size': size}).scalar()
    moved = 0
    if upper is not None:
        moved = _reconcile_between(db, cursor, upper)
        cursor = upper
    remaining = _count_after(db, cursor)
    status = 'ready' if remaining == 0 else 'migrating'
    _set_cursor(db, cursor, size, status)
    progress = verification(db)
    progress.update({'status': status, 'cursor': cursor, 'remaining': remaining, 'moved': moved})
    return progress


def run_all(db, batch_size=None):
    """Drain the backfill page by page. Each page commits on its own, so an interruption
    leaves a resumable cursor instead of a half-applied batch."""
    total = 0
    result = None
    for _ in range(10000):
        result = run_batch(db, batch_size)
        db.commit()
        total += 1
        if result['status'] == 'ready':
            break
    result['batches_run'] = total
    return result


def start(db, batch_size=None):
    current = state(db, lock=True)
    if current['status'] == 'active':
        raise HTTPException(409, '新账本已启用；如需重做请先退回旧表')
    restart = current['status'] in ('idle', 'returned')
    size = max(1, min(int(batch_size or current['batch_size'] or 200), 2000))
    db.execute(text(
        "UPDATE receipt_migrations SET status='migrating',active_ledger='receipts',"
        'cursor=CASE WHEN :restart THEN 0 ELSE cursor END,batch_size=:size,batches=0,'
        "started_at=COALESCE(started_at,now()),updated_at=now(),error='' WHERE id=1"
    ), {'restart': restart, 'size': size})
    return summary(db)


def switch(db):
    current = state(db, lock=True)
    if current['status'] == 'idle':
        raise HTTPException(409, '请先发起迁移并完成回填')
    if current['status'] in ('active', 'returned'):
        raise HTTPException(409, '新账本当前未处于待切换状态')
    moved = reconcile_all(db)
    check = verification(db)
    if not check['balanced']:
        db.execute(text('UPDATE receipt_migrations SET error=:error,updated_at=now() WHERE id=1'),
                   {'error': (f"核齐未通过：缺 {check['missing_count']} 行，多 {check['extra_count']} 行，"
                              f"无发起月份 {check['unplaced_count']} 行")})
        raise HTTPException(409, '回填尚未核齐，不能切换；请继续回填后重试')
    db.execute(text(
        "UPDATE receipt_migrations SET status='active',active_ledger='receipt_ledger',"
        "error='',activated_at=now(),updated_at=now(),cursor=:cursor WHERE id=1"
    ), {'cursor': last_receipt_id(db)})
    data = summary(db)
    data['reconciled_rows'] = moved
    return data


def return_to_legacy(db):
    current = state(db, lock=True)
    if current['active_ledger'] != 'receipt_ledger':
        raise HTTPException(409, '当前未启用新账本，无需退回')
    # Keep the new ledger complete so a later switch finds no gap.
    reconcile_all(db)
    db.execute(text(
        "UPDATE receipt_migrations SET status='returned',active_ledger='receipts',"
        "returned_at=now(),updated_at=now(),error='' WHERE id=1"
    ))
    return summary(db)


def ensure_schema(db):
    """Apply the pieces SQLAlchemy's ``create_all`` cannot add to an existing database."""
    db.execute(text('ALTER TABLE trials ADD COLUMN IF NOT EXISTS period VARCHAR(7)'))
    db.execute(text(
        "UPDATE trials SET period=to_char(created_at AT TIME ZONE 'UTC','YYYY-MM') "
        'WHERE period IS NULL AND created_at IS NOT NULL'
    ))
    ensure_state(db)
