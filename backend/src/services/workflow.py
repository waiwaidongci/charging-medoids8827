import time
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, deliveries, meters, runs, schedules
from src.services.data import parse_options


def load_centers(connection, base_id):
    run = fetch_one(connection, runs, base_id)
    if run is None:
        raise ValueError(f'中心来源任务 #{base_id} 不存在。')
    if run['decision'] != 'approved':
        raise ValueError(f'中心来源任务 #{base_id} 尚未复核批准，不能沿用其中心。')
    if run['invalidated'] is not None:
        raise ValueError(f'中心来源任务 #{base_id} 已因表计更正失效，请改用其他已批准任务。')
    centers = (run['result'] or {}).get('centers')
    if not centers:
        raise ValueError(f'中心来源任务 #{base_id} 缺少实测中心曲线，请重新运行该任务后再对账。')
    return centers, run['options']


def current_meters(connection):
    rows = connection.execute(select(meters)).mappings().all()
    current = {}
    for row in rows:
        key = (row['batch'], row['depot'])
        held = current.get(key)
        if held is None or (row['issued_at'], row['id']) > (held['issued_at'], held['id']):
            current[key] = dict(row)
    return current


def add_run(body, actor):
    options = parse_options(body.options)
    base_id = options.get('base_run_id')
    now = time.time()
    with engine.begin() as connection:
        if fetch_one(connection, datasets, body.dataset_id) is None:
            raise HTTPException(404, '数据版本不存在。')
        if base_id is not None:
            try:
                load_centers(connection, base_id)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
        key = connection.execute(insert(runs).values(dataset_id=body.dataset_id, options=options,
                    state=State.queued.value, owner=actor, created=now)).inserted_primary_key[0]
        add_event(connection, actor, 'queue', key, now)
    return {'id': key, 'state': State.queued.value}


def claim_run():
    now = time.time()
    claimable = or_(runs.c.state == State.queued.value, and_(runs.c.state == State.running.value, runs.c.started < now - 120))
    with engine.begin() as connection:
        run = connection.execute(select(runs).where(claimable).order_by(runs.c.id).limit(1)).mappings().one_or_none()
        if run is None:
            return None
        key = run['id']
        claim = connection.execute(update(runs).where(runs.c.id == key, claimable)
                                   .values(state=State.running.value, started=now))
        if claim.rowcount != 1:
            return None
        data = fetch_one(connection, datasets, run['dataset_id'])
        return key, now, data['records'], run['options']


def _refs_stale(connection, refs):
    """True when any referenced meter record is no longer the current one of its group."""
    rows = connection.execute(select(meters).where(meters.c.id.in_(refs))).mappings().all()
    used = {(row['batch'], row['depot']): row['id'] for row in rows}
    for (batch, depot), ref_id in used.items():
        group = connection.execute(select(meters.c.id, meters.c.issued_at)
                                   .where(meters.c.batch == batch, meters.c.depot == depot)
                                   .with_for_update()).mappings().all()
        current = max(group, key=lambda row: (row['issued_at'], row['id']))
        if current['id'] != ref_id:
            return True
    return False


def finish_run(key, claim, result, error):
    now = time.time()
    state = State.failed.value if error else State.completed.value
    refs = (result or {}).get('meter_refs') or None
    with engine.begin() as connection:
        invalidated = now if refs and _refs_stale(connection, refs) else None
        saved = connection.execute(update(runs).where(runs.c.id == key, runs.c.started == claim,
                                   runs.c.state == State.running.value).values(state=state, result=result,
                                   error=error, finished=now, meter_refs=refs, invalidated=invalidated))
        if saved.rowcount:
            add_event(connection, 'worker', state, key, now)


def review_run(key, body, actor):
    now = time.time()
    note = body.note.strip()
    if len(note) < 2:
        raise ValueError('请输入至少两个字符的复核意见。')
    with engine.begin() as connection:
        run = connection.execute(select(runs).where(runs.c.id == key).with_for_update()).mappings().one_or_none()
        if run is None:
            raise HTTPException(404, '分析任务不存在。')
        if run['state'] != State.completed.value:
            raise HTTPException(409, '分析完成后才能复核。')
        if run['invalidated'] is not None:
            raise HTTPException(409, '该结果基于的表计记录已被更正，本版已失效，请重新运行分析。')
        if body.decision == 'approved' and run['result'].get('can_publish') is False:
            raise HTTPException(409, '结果未达到本项目的发布门槛。')
        updated = connection.execute(update(runs).where(runs.c.id == key, runs.c.decision.is_(None),
                                     runs.c.invalidated.is_(None))
                                     .values(decision=body.decision, reviewer=actor, review_note=note, reviewed=now))
        if updated.rowcount != 1:
            raise HTTPException(409, '已完成复核或结果已失效。请重新运行分析以提交新的复核。')
        add_event(connection, actor, body.decision, key, now)
    return {'decision': body.decision}


def schedule_report(key, body, actor):
    now = time.time()
    recipient = body.recipient.strip()
    if len(recipient) < 2:
        raise ValueError('请填写站内收件组。')
    if body.scheduled_at and body.scheduled_at.utcoffset() is None:
        raise ValueError('投递时间须含时区。')
    timestamp = body.scheduled_at.timestamp() if body.scheduled_at else now
    if body.scheduled_at and timestamp < now - 60:
        raise ValueError('投递时间已过，请调整时间。')
    with engine.begin() as connection:
        run = connection.execute(select(runs).where(runs.c.id == key).with_for_update()).mappings().one_or_none()
        if run is None or run['decision'] != 'approved':
            raise HTTPException(409, '仅已批准的结果可分发。')
        if run['invalidated'] is not None:
            raise HTTPException(409, '该结果已因表计更正失效，不能建立投递计划。')
        required_recipient = run['result'].get('dispatch_recipient')
        if required_recipient and recipient != required_recipient:
            raise HTTPException(422, f'该报告应投递给{required_recipient}。')
        schedule_id = connection.execute(insert(schedules).values(run_id=key, recipient=recipient, cadence=body.cadence,
                                      next_at=timestamp, active=1, owner=actor)).inserted_primary_key[0]
        add_event(connection, actor, 'schedule', schedule_id, now)
    return {'id': schedule_id, 'next_at': timestamp}


def cancel_schedule(key, actor):
    with engine.begin() as connection:
        updated = connection.execute(update(schedules).where(schedules.c.id == key, schedules.c.active == 1)
                                     .values(active=0))
        if not updated.rowcount:
            raise HTTPException(409, '该计划已停止或已完成。')
        add_event(connection, actor, 'cancel', key, time.time())
    return {'id': key, 'active': False}


def _meter_view(connection, depot, batch, energy, tolerance, issued):
    same = connection.execute(select(meters).where(meters.c.depot == depot, meters.c.batch == batch,
                              meters.c.energy_kwh == energy, meters.c.tolerance_kwh == tolerance,
                              meters.c.issued_at == issued)).mappings().one_or_none()
    if same is None:
        return None
    group = connection.execute(select(meters).where(meters.c.depot == depot, meters.c.batch == batch)).mappings().all()
    current = max(group, key=lambda row: (row['issued_at'], row['id']))
    return {'id': same['id'], 'current': current['id'] == same['id'], 'invalidated': 0, 'duplicated': True}


def record_meter(body, actor):
    depot = body.depot.strip()
    if not depot:
        raise ValueError('请填写场站。')
    try:
        datetime.strptime(body.batch, '%Y-%m-%d')
    except ValueError:
        raise ValueError('批次日期须为 YYYY-MM-DD。') from None
    if body.issued_at and body.issued_at.utcoffset() is None:
        raise ValueError('出具时间须含时区。')
    now = time.time()
    issued = body.issued_at.timestamp() if body.issued_at else now
    invalidated = 0
    try:
        with engine.begin() as connection:
            existing = _meter_view(connection, depot, body.batch, body.energy_kwh, body.tolerance_kwh, issued)
            if existing is not None:
                return existing
            key = connection.execute(insert(meters).values(depot=depot, batch=body.batch, energy_kwh=body.energy_kwh,
                                                           tolerance_kwh=body.tolerance_kwh, issued_at=issued,
                                                           recorded_at=now, owner=actor)).inserted_primary_key[0]
            add_event(connection, actor, 'meter', key, now)
            group = connection.execute(select(meters).where(meters.c.depot == depot, meters.c.batch == body.batch)
                                       .with_for_update()).mappings().all()
            current = max(group, key=lambda row: (row['issued_at'], row['id']))
            if current['id'] == key and len(group) > 1:
                stale = {row['id'] for row in group} - {key}
                candidates = connection.execute(select(runs).where(runs.c.meter_refs.isnot(None),
                                                runs.c.invalidated.is_(None)).with_for_update()).mappings().all()
                for run in candidates:
                    if stale.isdisjoint(run['meter_refs'] or []):
                        continue
                    connection.execute(update(runs).where(runs.c.id == run['id'], runs.c.invalidated.is_(None))
                                       .values(invalidated=now))
                    add_event(connection, actor, 'invalidate', run['id'], now)
                    invalidated += 1
                    pending = connection.execute(select(schedules.c.id).where(schedules.c.run_id == run['id'],
                                                 schedules.c.active == 1)).scalars().all()
                    for schedule_id in pending:
                        connection.execute(update(schedules).where(schedules.c.id == schedule_id).values(active=0))
                        add_event(connection, actor, 'cancel', schedule_id, now)
        return {'id': key, 'current': current['id'] == key, 'invalidated': invalidated}
    except IntegrityError:
        with engine.connect() as connection:
            existing = _meter_view(connection, depot, body.batch, body.energy_kwh, body.tolerance_kwh, issued)
        if existing is None:
            raise
        return existing


def publish_due():
    now = time.time()
    with engine.begin() as connection:
        pending = connection.execute(select(schedules).where(schedules.c.active == 1, schedules.c.next_at <= now)
                                     .order_by(schedules.c.next_at, schedules.c.id).limit(20)).mappings().all()
        for item in pending:
            previous = item['next_at']
            daily = item['cadence'] == 'daily'
            future = previous + (int((now - previous) / 86400) + 1) * 86400 if daily else previous
            run = connection.execute(select(runs.c.invalidated).where(runs.c.id == item['run_id'])
                                     .with_for_update()).mappings().one_or_none()
            if run is None or run['invalidated'] is not None:
                stopped = connection.execute(update(schedules).where(schedules.c.id == item['id'],
                                             schedules.c.active == 1, schedules.c.next_at == previous).values(active=0))
                if stopped.rowcount:
                    add_event(connection, 'worker', 'cancel', item['id'], now)
                continue
            changed = connection.execute(update(schedules).where(schedules.c.id == item['id'], schedules.c.active == 1,
                              schedules.c.next_at == previous).values(active=int(daily), next_at=future))
            if changed.rowcount:
                delivery_id = connection.execute(insert(deliveries).values(schedule_id=item['id'], run_id=item['run_id'],
                                               recipient=item['recipient'], scheduled=previous, delivered=now)).inserted_primary_key[0]
                add_event(connection, 'worker', 'delivered', delivery_id, now)
