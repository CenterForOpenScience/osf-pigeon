import logging
import requests
from osf_pigeon import pigeon
from concurrent.futures import ThreadPoolExecutor
from osf_pigeon import settings
from aiohttp import web

import sentry_sdk
from sentry_sdk.integrations.aiohttp import AioHttpIntegration

sentry_sdk.init(
    dsn=settings.SENTRY_DSN,
    release="26.0.1",
    integrations=[AioHttpIntegration()],
)

pigeon_jobs = ThreadPoolExecutor(max_workers=settings.MAX_WORKERS, thread_name_prefix="pigeon_jobs")
app = web.Application()
routes = web.RouteTableDef()
logging.basicConfig(level=logging.DEBUG)


def handle_exception(future):
    exception = future.exception()
    if exception:
        sentry_sdk.capture_exception(exception)
        app.logger.exception(exception)


def archive_task_done(future):
    if future.result() and not future.exception():
        ia_item, guid = future.result()
        resp = requests.post(
            f"{settings.OSF_API_URL}_/ia/{guid}/done/",
            headers={"Authorization": f"Bearer {settings.OSF_BEARER_TOKEN}"},
            json={"ia_url": ia_item.urls.details},
        )
        app.logger.info(f"{ia_item} called back with {resp}")


def metadata_task_done(future):
    if future.result() and not future.exception():
        ia_item, updated_metadata = future.result()
        app.logger.info(f"{ia_item} updated metadata {updated_metadata}")


@routes.get("/")
async def index(request):
    return web.json_response({"🐦": "👍"})


@routes.get("/archive/{guid}")
@routes.post("/archive/{guid}")
async def archive(request):
    """
    This endpoint is called by osf.io to begin the archive process for a registration, downloading,
    copying data and uploading it to IA.
    :param request:
    :return: json_response this just sends a simple message showing the request was recieved
    """
    guid = request.match_info["guid"]
    future = pigeon_jobs.submit(pigeon.run, pigeon.archive(guid))
    future.add_done_callback(handle_exception)
    future.add_done_callback(archive_task_done)
    return web.json_response({guid: future._state})


@routes.post("/metadata/{guid}")
async def set_metadata(request):
    """
    This endpoint recieves json from osf.io when a registration is updated to sync IA item
    metadata with the osf registration.
    :param request:
    :return:
    """
    guid = request.match_info["guid"]
    metadata = await request.json()
    future = pigeon_jobs.submit(pigeon.sync_metadata, guid, metadata)
    future.add_done_callback(handle_exception)
    future.add_done_callback(metadata_task_done)
    return web.json_response({guid: future._state})
