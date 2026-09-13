"""Stubs for the homeassistant/aiohttp modules the integration imports.

Lets the account-layer failover state machine run in bare CPython without HA.
"""
import sys, types

def mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

# ---- aiohttp stub (tests inject their own session) ----
aiohttp = mod('aiohttp')
class ClientError(Exception):
    pass
class ClientSession:
    pass
aiohttp.ClientError = ClientError
aiohttp.ClientSession = ClientSession

# ---- homeassistant core ----
mod('homeassistant')
config_entries = mod('homeassistant.config_entries')
class ConfigEntry:
    pass
config_entries.ConfigEntry = ConfigEntry

core = mod('homeassistant.core')
class HomeAssistant:
    pass
def callback(f):
    return f
core.HomeAssistant = HomeAssistant
core.callback = callback
core.SupportsResponse = types.SimpleNamespace(ONLY='only')

ce = config_entries  # same module object, do not recreate
class _ConfigEntries:
    def async_update_entry(self, entry, data=None, version=None):
        if data is not None:
            entry.data = dict(data)
        if version is not None:
            entry.version = version
ce.config_entries = _ConfigEntries()
ce.ConfigEntry = ConfigEntry
ce.ConfigFlow = type('ConfigFlow', (), {})
ce.ConfigFlowResult = dict

helpers = mod('homeassistant.helpers')

aiohttp_client = mod('homeassistant.helpers.aiohttp_client')
_HOLDING = {'session': None}
def async_get_clientsession(hass):
    return _HOLDING['session']
aiohttp_client.async_get_clientsession = async_get_clientsession
helpers.aiohttp_client = aiohttp_client

dr = mod('homeassistant.helpers.device_registry')
class DeviceInfo(dict):
    pass
dr.DeviceInfo = DeviceInfo
helpers.device_registry = dr

event = mod('homeassistant.helpers.event')
TIMERS = []
def async_call_later(hass, delay, action):
    handle = {'action': action, 'cancelled': False, 'delay': delay}
    def unsub():
        handle['cancelled'] = True
    handle['unsub'] = unsub
    TIMERS.append(handle)
    return unsub
event.async_call_later = async_call_later
helpers.event = event

storage = mod('homeassistant.helpers.storage')
class Store:
    """Shared-key in-memory store, like real disk-backed HA Store."""
    _disks: dict = {}
    def __init__(self, hass, version, key, **k):
        self.key = key
        self.data = None
    async def async_load(self):
        return Store._disks.get(self.key)
    async def async_save(self, data):
        Store._disks[self.key] = data
        return None
storage.Store = Store
helpers.storage = storage

util_mod = mod('homeassistant.util')
def slugify(v):
    return v.lower().replace(' ', '_')
util_mod.slugify = slugify

ce_mod = mod('homeassistant.exceptions')
class ConfigEntryAuthFailed(Exception):
    pass
ce_mod.ConfigEntryAuthFailed = ConfigEntryAuthFailed

cv = mod('homeassistant.helpers.config_validation')
cv.string = str
cv.make_entity_service_schema = lambda x: x
helpers.config_validation = cv

svc = mod('homeassistant.helpers.service')
def async_register_admin_service(hass, domain, service, func, schema=None, supports_response=None):
    pass
svc.async_register_admin_service = async_register_admin_service
helpers.service = svc

comp = mod('homeassistant.components')
pn = mod('homeassistant.components.persistent_notification')
NOTIFS = {}
def pn_create(hass, message='', title='', notification_id=None):
    NOTIFS[notification_id] = (title, message)
def pn_dismiss(hass, notification_id):
    NOTIFS.pop(notification_id, None)
pn.async_create = pn_create
pn.async_dismiss = pn_dismiss
comp.persistent_notification = pn

uc = mod('homeassistant.helpers.update_coordinator')
class DataUpdateCoordinator:
    def __init__(self, hass, logger, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.config_entry = config_entry
        self.update_interval = update_interval
        self._listeners = set()
        self.data = None
    def async_add_listener(self, cb):
        self._listeners.add(cb)
        return lambda: self._listeners.discard(cb)
    def async_update_listeners(self):
        for cb in list(self._listeners):
            cb()
    async def async_request_refresh(self):
        pass
    async def async_config_entry_first_refresh(self):
        self.data = await self._async_update_data()
        return self.data
class UpdateFailed(Exception):
    pass
uc.DataUpdateCoordinator = DataUpdateCoordinator
uc.UpdateFailed = UpdateFailed
helpers.update_coordinator = uc
