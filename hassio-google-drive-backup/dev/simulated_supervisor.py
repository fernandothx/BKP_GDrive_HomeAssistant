import asyncio
import random
import io

from backup.config import Config
from backup.time import Time
from aiohttp.web import (HTTPBadRequest, HTTPNotFound,
                         HTTPUnauthorized, Request, Response, get,
                         json_response, post)
from aiohttp import hdrs, web, ClientSession
from injector import inject, singleton
from .base_server import BaseServer
from .ports import Ports
from typing import Any, Dict
from tests.helpers import all_addons, createSnapshotTar, parseSnapshotInfo

URL_MATCH_SNAPSHOT_FULL = "^/snapshots/new/full$"
URL_MATCH_SNAPSHOT_DELETE = "^/snapshots/.*/remove$"
URL_MATCH_SNAPSHOT_DOWNLOAD = "^/snapshots/.*/download$"
URL_MATCH_MISC_INFO = "^/info$"
URL_MATCH_CORE_API = "^/core/api.*$"
URL_MATCH_START_ADDON = "^/addons/.*/start$"
URL_MATCH_STOP_ADDON = "^/addons/.*/stop$"
URL_MATCH_ADDON_INFO = "^/addons/.*/info$"


@singleton
class SimulatedSupervisor(BaseServer):
    @inject
    def __init__(self, config: Config, ports: Ports, time: Time):
        self._config = config
        self._time = time
        self._ports = ports
        self._auth_token = "test_header"
        self._snapshots: Dict[str, Any] = {}
        self._snapshot_data: Dict[str, bytearray] = {}
        self._snapshot_lock = asyncio.Lock()
        self._snapshot_inner_lock = asyncio.Lock()
        self._entities = {}
        self._events = []
        self._attributes = {}
        self._notification = None
        self._min_snapshot_size = 1024 * 1024 * 3
        self._max_snapshot_size = 1024 * 1024 * 5
        self._addon_slug = "self_slug"
        self._options = self.defaultOptions()
        self._username = "user"
        self._password = "pass"
        self._addons = all_addons.copy()

        self.installAddon(self._addon_slug, "Home Assistant Google drive Backup")
        self.installAddon("42", "The answer")
        self.installAddon("sgadg", "sdgsagsdgsggsd")

    def defaultOptions(self):
        return {
            "max_snapshots_in_hassio": 4,
            "max_snapshots_in_google_drive": 4,
            "days_between_snapshots": 3,
            "use_ssl": False
        }

    def routes(self):
        return [
            post('/addons/{slug}/options', self._updateOptions),
            post("/core/api/services/persistent_notification/dismiss", self._dismissNotification),
            post("/core/api/services/persistent_notification/create", self._createNotification),
            post("/core/api/events/{name}", self._haEventUpdate),
            post("/core/api/states/{entity}", self._haStateUpdate),
            post('/auth', self._authenticate),
            get('/auth', self._authenticate),
            get('/info', self._miscInfo),
            get('/addons/self/info', self._selfInfo),
            get('/addons/{slug}/info', self._addonInfo),
            get('/snapshots/{slug}/download', self._snapshotDownload),
            get('/snapshots/{slug}/info', self._snapshotDetail),
            post('/addons/{slug}/start', self._startAddon),
            post('/addons/{slug}/stop', self._stopAddon),
            post('/snapshots/{slug}/remove', self._deleteSnapshot),
            post('/snapshots/new/upload', self._uploadSnapshot),
            get('/snapshots/new/upload', self._uploadSnapshot),
            post('/snapshots/new/partial', self._newSnapshot),
            post('/snapshots/new/full', self._newSnapshot),
            get('/snapshots/new/full', self._newSnapshot),
            get('/core/info', self._coreInfo),
            get('/supervisor/info', self._supervisorInfo),
            get('/supervisor/logs', self._supervisorLogs),
            get('/core/logs', self._coreLogs),
            get('/snapshots', self._getSnapshots)
        ]

    def getEvents(self):
        return self._events.copy()

    def getEntity(self, entity):
        return self._entities.get(entity)

    def clearEntities(self):
        self._entities = {}

    def addon(self, slug):
        for addon in self._addons:
            if addon["slug"] == slug:
                return addon
        return None

    def getAttributes(self, attribute):
        return self._attributes.get(attribute)

    def getNotification(self):
        return self._notification

    def _formatErrorResponse(self, error: str) -> str:
        return json_response({'result': error})

    def _formatDataResponse(self, data: Any) -> str:
        return json_response({'result': 'ok', 'data': data})

    async def toggleBlockSnapshot(self):
        if self._snapshot_lock.locked():
            self._snapshot_lock.release()
        else:
            await self._snapshot_lock.acquire()

    async def _verifyHeader(self, request) -> bool:
        if request.headers.get("X-Supervisor-Token", None) == self._auth_token:
            return
        if request.headers.get("Authorization", None) == "Bearer " + self._auth_token:
            return
        raise HTTPUnauthorized()

    async def _getSnapshots(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse({'snapshots': list(self._snapshots.values())})

    async def _stopAddon(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        for addon in self._addons:
            if addon.get("slug", "") == slug:
                if addon.get("state") == "started":
                    addon["state"] = "stopped"
                    return self._formatDataResponse({})
        raise HTTPBadRequest()

    async def _startAddon(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        for addon in self._addons:
            if addon.get("slug", "") == slug:
                if addon.get("state") == "stopped":
                    addon["state"] = "started"
                    return self._formatDataResponse({})
        raise HTTPBadRequest()

    async def _addonInfo(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        for addon in self._addons:
            if addon.get("slug", "") == slug:
                return self._formatDataResponse({
                    'boot': addon.get("boot"),
                    'watchdog': addon.get("watchdog"),
                    'state': addon.get("state"),
                })
        raise HTTPBadRequest()

    async def _supervisorInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse(
            {
                "addons": list(self._addons).copy()
            }
        )

    async def _supervisorLogs(self, request: Request):
        await self._verifyHeader(request)
        return Response(body="Supervisor Log line 1\nSupervisor Log Line 2")

    async def _coreLogs(self, request: Request):
        await self._verifyHeader(request)
        return Response(body="Core Log line 1\nCore Log Line 2")

    async def _coreInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse(
            {
                "version": "1.3.3.7",
                "last_version": "1.3.3.8",
                "machine": "VS Dev",
                "ip_address": "127.0.0.1",
                "arch": "x86",
                "image": "image",
                "custom": "false",
                "boot": "true",
                "port": self._ports.server,
                "ssl": "false",
                "watchdog": "what is this",
                "wait_boot": "so many arguments"
            }
        )

    async def _newSnapshot(self, request: Request):
        if self._snapshot_lock.locked():
            raise HTTPBadRequest()
        input_json = await request.json()
        async with self._snapshot_lock:
            async with self._snapshot_inner_lock:
                await self._verifyHeader(request)
                slug = self.generateId(8)
                password = input_json.get('password', None)
                data = createSnapshotTar(
                    slug,
                    input_json.get('name', "Default name"),
                    date=self._time.now(),
                    padSize=int(random.uniform(self._min_snapshot_size, self._max_snapshot_size)),
                    included_folders=input_json.get('folders', None),
                    included_addons=input_json.get('addons', None),
                    password=password)
                snapshot_info = parseSnapshotInfo(data)
                self._snapshots[slug] = snapshot_info
                self._snapshot_data[slug] = bytearray(data.getbuffer())
                return self._formatDataResponse({"slug": slug})

    async def _uploadSnapshot(self, request: Request):
        await self._verifyHeader(request)
        try:
            reader = await request.multipart()
            contents = await reader.next()
            received_bytes = bytearray()
            while True:
                chunk = await contents.read_chunk()
                if not chunk:
                    break
                received_bytes.extend(chunk)
            info = parseSnapshotInfo(io.BytesIO(received_bytes))
            self._snapshots[info['slug']] = info
            self._snapshot_data[info['slug']] = received_bytes
            return self._formatDataResponse({"slug": info['slug']})
        except Exception as e:
            print(str(e))
            return self._formatErrorResponse("Bad snapshot")

    async def _deleteSnapshot(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        if slug not in self._snapshots:
            raise HTTPNotFound()
        del self._snapshots[slug]
        del self._snapshot_data[slug]
        return self._formatDataResponse("deleted")

    async def _snapshotDetail(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        if slug not in self._snapshots:
            raise HTTPNotFound()
        return self._formatDataResponse(self._snapshots[slug])

    async def _snapshotDownload(self, request: Request):
        await self._verifyHeader(request)
        slug = request.match_info.get('slug')
        if slug not in self._snapshot_data:
            raise HTTPNotFound()
        return self.serve_bytes(request, self._snapshot_data[slug])

    async def _selfInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse({
            "webui": "http://some/address",
            'ingress_url': "fill me in later",
            "slug": self._addon_slug,
            "options": self._options
        })

    async def _miscInfo(self, request: Request):
        await self._verifyHeader(request)
        return self._formatDataResponse({
            "supervisor": "super version",
            "homeassistant": "ha version",
            "hassos": "hassos version",
            "hostname": "hostname",
            "machine": "machine",
            "arch": "Arch",
            "supported_arch": "supported arch",
            "channel": "channel"
        })

    def installAddon(self, slug, name, version="v1.0", boot=True, started=True):
        self._addons.append({
            "name": 'Name for ' + name,
            "slug": slug,
            "description": slug + " description",
            "version": version,
            "watchdog": False,
            "boot": "auto" if boot else "manual",
            "ingress_entry": "/api/hassio_ingress/" + slug,
            "state": "started" if started else "stopped"
        })

    async def _authenticate(self, request: Request):
        await self._verifyHeader(request)
        input_json = await request.json()
        if input_json.get("username") != self._username or input_json.get("password") != self._password:
            raise HTTPBadRequest()
        return self._formatDataResponse({})

    async def _updateOptions(self, request: Request):
        slug = request.match_info.get('slug')

        if slug == "self":
            await self._verifyHeader(request)
            self._options = (await request.json())['options'].copy()
        else:
            self.addon(slug).update(await request.json())
        return self._formatDataResponse({})

    async def _haStateUpdate(self, request: Request):
        await self._verifyHeader(request)
        entity = request.match_info.get('entity')
        json = await request.json()
        self._entities[entity] = json['state']
        self._attributes[entity] = json['attributes']
        return Response()

    async def _haEventUpdate(self, request: Request):
        await self._verifyHeader(request)
        name = request.match_info.get('name')
        self._events.append((name, await request.json()))
        return Response()

    async def _createNotification(self, request: Request):
        await self._verifyHeader(request)
        notification = await request.json()
        print("Created notification with: {}".format(notification))
        self._notification = notification.copy()
        return Response()

    async def _dismissNotification(self, request: Request):
        await self._verifyHeader(request)
        print("Dismissed notification with: {}".format(await request.json()))
        self._notification = None
        return Response()
