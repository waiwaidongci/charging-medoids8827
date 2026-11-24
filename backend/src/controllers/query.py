from fastapi import HTTPException
from sqlalchemy import select

from src.models.database import engine, fetch_one
from src.models.tables import audit, datasets, deliveries, meters, runs, schedules


def list_rows(table, fields=None, limit=100):
    statement = select(*fields) if fields else select(table)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement.order_by(table.c.id.desc()).limit(limit)).mappings()]


def revisions():
    columns = [field for field in datasets.c if field.name != 'records']
    return list_rows(datasets, columns)


def require_row(table, key, label):
    with engine.connect() as connection:
        row = fetch_one(connection, table, key)
    if row is None:
        raise HTTPException(404, label + '不存在。')
    return dict(row)


def jobs():
    return list_rows(runs, [field for field in runs.c if field.name != 'result'])


def delivery_status():
    return {'schedules': list_rows(schedules), 'deliveries': list_rows(deliveries)}


def meter_status():
    rows = list_rows(meters)
    current = {}
    for row in rows:
        key = (row['batch'], row['depot'])
        held = current.get(key)
        if held is None or (row['issued_at'], row['id']) > (held['issued_at'], held['id']):
            current[key] = row
    live = {row['id'] for row in current.values()}
    return [{**row, 'current': row['id'] in live} for row in rows]


def recent_activity():
    return list_rows(audit, limit=20)
