import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from sqlalchemy import inspect, text

from scripts.seed_demo import seed
from src.config.settings import SEED
from src.middlewares.http import install
from src.models.database import cache, engine
from src.models.tables import metadata
from src.routes.api import router


def ensure_columns():
    """Add columns introduced after the initial schema to existing databases."""
    inspector = inspect(engine)
    if 'analysis_runs' not in inspector.get_table_names():
        return
    existing = {column['name'] for column in inspector.get_columns('analysis_runs')}
    with engine.begin() as connection:
        if 'meter_refs' not in existing:
            connection.execute(text('ALTER TABLE analysis_runs ADD COLUMN meter_refs JSON NULL'))
        if 'invalidated' not in existing:
            connection.execute(text('ALTER TABLE analysis_runs ADD COLUMN invalidated DOUBLE NULL'))


@asynccontextmanager
async def lifespan(app):
    metadata.create_all(engine)
    ensure_columns()
    if SEED:
        try:
            logging.info('Demo seed: %s', seed())
        except Exception:
            logging.exception('Demo seed failed. Run python -m scripts.seed_demo to retry.')
    yield


app = FastAPI(title='业务分析工作台', lifespan=lifespan)
install(app)
app.include_router(router)


@app.get('/health')
def health():
    with engine.connect() as connection:
        connection.execute(text('SELECT 1'))
    cache.ping()
    return {'status': 'ok'}


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)
