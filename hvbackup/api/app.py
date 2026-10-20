"""FastAPI application: REST API under /api/v1, agent API, and the web UI."""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import APP_VERSION, config, db
from ..core import ops, scheduler, tasks
from ..store.chunks import repo
from . import agent_routes, pve_routes, routes, vsphere_plugin_routes

try:
    from . import ui
except ImportError:  # UI not present in this build
    ui = None

log = logging.getLogger('hvbackup')


@asynccontextmanager
async def lifespan(app):
    db.init()
    rep = repo()
    log.info('hvbackup %s: data %s, repository %s (%s)', APP_VERSION, config.DATA_DIR, rep.path, rep.id)
    ops.register_tasks()
    tasks.recover_orphans()
    scheduler.start(ops.scheduler_run_job, every_tick=ops.maybe_gc)
    yield
    scheduler.stop()


def create_app():
    app = FastAPI(title='hvbackup', version=APP_VERSION, lifespan=lifespan, docs_url='/api/docs', openapi_url='/api/openapi.json')
    app.include_router(routes.r)
    app.include_router(agent_routes.r)
    app.include_router(pve_routes.r)
    app.include_router(vsphere_plugin_routes.r)
    if ui is not None:
        app.include_router(ui.r)
    static = os.path.join(os.path.dirname(__file__), '..', 'web', 'static')
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.exception_handler(RuntimeError)
    async def runtime_error(request: Request, exc: RuntimeError):
        return JSONResponse(status_code=400, content={'detail': str(exc)})

    return app
