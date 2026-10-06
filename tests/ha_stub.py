"""Stubs for the homeassistant/aiohttp modules the integration imports.

Lets the account-layer failover state machine run in bare CPython without HA.
"""
import dataclasses
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

# ---- voluptuous stub (service schemas are only built at import time) ----
vol = mod('voluptuous')
class Invalid(Exception):
    pass
class Schema:
    def __init__(self, schema, **k):
        self.schema = schema
    def __call__(self, data):
        return data
class Marker:
    """Stands in for Required/Optional keys; hashable like the real markers."""
    def __init__(self, key, default=None, description=None):
        self.key = key
        self.default = default
    def __hash__(self):
        return hash(self.key)
    def __eq__(self, other):
        return self.key == getattr(other, 'key', other)
vol.Invalid = Invalid
vol.Schema = Schema
vol.Required = Marker
vol.Optional = Marker
vol.Any = lambda *validators: validators
vol.In = lambda container: container

# ---- cryptography stub (api.py imports it; the stub never signs anything) ----
mod('cryptography')
mod('cryptography.hazmat')
primitives = mod('cryptography.hazmat.primitives')

# hashes: only the SHA256 *selector* is passed around, never computed with.
_hashes = mod('cryptography.hazmat.primitives.hashes')
_hashes.SHA256 = lambda: ('SHA256',)
primitives.hashes = _hashes

# serialization: load_der_private_key returns a key object whose .sign() is
# only used to build the `sign` field of the login body. Deterministic stand-in
# keeps the payload shape identical without real crypto.
_serialization = mod('cryptography.hazmat.primitives.serialization')
class _FakeKey:
    def sign(self, data, padding, algorithm):
        import hashlib
        return hashlib.sha256(data).digest()
_serialization.load_der_private_key = lambda data, password=None: _FakeKey()
primitives.serialization = _serialization

# padding.PKCS7 is used for real by api.HotataAccount._encrypt_password, so the
# stub must actually pad: test_v4_account drives the login path with a real
# password. A bare module with no PKCS7 raises AttributeError there.
_crypto_padding = mod('cryptography.hazmat.primitives.padding')
class _PKCS7:
    """Minimal PKCS7 padder/unpadder over the byte values the tests use."""
    def __init__(self, block_size):
        self.block_size = block_size // 8
    def padder(self):
        return _PKCS7Padder(self.block_size)
    def unpadder(self):
        return _PKCS7Unpadder(self.block_size)
class _PKCS7Padder:
    def __init__(self, block_size):
        self._block_size = block_size
    def update(self, data):
        pad_len = self._block_size - (len(data) % self._block_size)
        return data + bytes([pad_len]) * pad_len
    def finalize(self):
        return b''
class _PKCS7Unpadder:
    def __init__(self, block_size):
        self._block_size = block_size
    def update(self, data):
        if not data:
            return b''
        pad_len = data[-1]
        return data[:-pad_len]
    def finalize(self):
        return b''
_crypto_padding.PKCS7 = _PKCS7
primitives.padding = _crypto_padding

asymmetric = mod('cryptography.hazmat.primitives.asymmetric')
_asym_padding = mod('cryptography.hazmat.primitives.asymmetric.padding')
_asym_padding.PKCS1v15 = lambda: ('PKCS1v15',)
asymmetric.padding = _asym_padding
primitives.asymmetric = asymmetric

# Cipher/algorithms/modes are used for real by
# api.HotataAccount._encrypt_password (AES-CBC + PKCS7). The stub implements a
# reversible stand-in instead of a no-op, so test_v4_account's login path
# exercises the same code shape without pulling in `cryptography`.
ciphers = mod('cryptography.hazmat.primitives.ciphers')

class _Algorithms:
    def AES(self, key):
        return ('AES', key)

class _Modes:
    def CBC(self, iv):
        return ('CBC', iv)

class _Encryptor:
    def __init__(self, key, iv):
        self._key = key
        self._iv = iv
    def update(self, data):
        return bytes(b ^ self._key[i % len(self._key)] for i, b in enumerate(data))
    def finalize(self):
        return b''

class _Decryptor:
    def __init__(self, key, iv):
        self._key = key
        self._iv = iv
    def update(self, data):
        return bytes(b ^ self._key[i % len(self._key)] for i, b in enumerate(data))
    def finalize(self):
        return b''

class Cipher:
    def __init__(self, algorithm, mode):
        self._key = algorithm[1]
        self._iv = mode[1]
    def encryptor(self):
        return _Encryptor(self._key, self._iv)
    def decryptor(self):
        return _Decryptor(self._key, self._iv)

ciphers.Cipher = Cipher
_algorithms = mod('cryptography.hazmat.primitives.ciphers.algorithms')
_algorithms.AES = _Algorithms().AES
ciphers.algorithms = _algorithms
_modes = mod('cryptography.hazmat.primitives.ciphers.modes')
_modes.CBC = _Modes().CBC
ciphers.modes = _modes
primitives.ciphers = ciphers

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
class ServiceCall:
    def __init__(self, data=None):
        self.data = data or {}
core.HomeAssistant = HomeAssistant
core.ServiceCall = ServiceCall
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

# ---- cover platform support (minimum needed to drive HotataAirerCover) ----

class _StateWriter:
    """Counts async_write_ha_state() calls so tests can assert on them.

    Shared by the CoverEntity and CoordinatorEntity stubs so the count is the
    same no matter which one wins the MRO. Tests may also override the method
    (class or instance level) for their own bookkeeping.
    """
    ha_state_writes = 0
    def async_write_ha_state(self):
        self.ha_state_writes = self.ha_state_writes + 1

cover = mod('homeassistant.components.cover')
ATTR_POSITION = 'position'
class CoverDeviceClass:
    SHADE = 'shade'
    CURTAIN = 'curtain'
class CoverEntityFeature:
    """Real HA bit values, so `|` composition matches the integration."""
    OPEN = 1
    CLOSE = 2
    SET_POSITION = 4
    STOP = 8
class CoverEntity(_StateWriter):
    """Position-aware cover base: only the attributes the integration reads."""
    _attr_device_class = None
    _attr_translation_key = None
    _attr_supported_features = 0
    # Unlike _attr_is_closed, real HA gives these two a default.
    _attr_is_closing: bool | None = False
    _attr_is_opening: bool | None = False
    _attr_current_cover_position: int | None = None
    @property
    def current_cover_position(self):
        return self._attr_current_cover_position
    @property
    def is_closed(self):
        return self._attr_is_closed
    @property
    def is_closing(self):
        return self._attr_is_closing
    @property
    def is_opening(self):
        return self._attr_is_opening
cover.ATTR_POSITION = ATTR_POSITION
cover.CoverDeviceClass = CoverDeviceClass
cover.CoverEntityFeature = CoverEntityFeature
cover.CoverEntity = CoverEntity
comp.cover = cover

ep = mod('homeassistant.helpers.entity_platform')
class AddEntitiesCallback:
    pass
ep.AddEntitiesCallback = AddEntitiesCallback
helpers.entity_platform = ep

class CoordinatorEntity(_StateWriter):
    """Coordinator-backed entity: subscribe + state write, nothing else."""
    def __class_getitem__(cls, item):
        return cls
    def __init__(self, coordinator, context=None):
        self.coordinator = coordinator
        self.coordinator_context = context
        self.hass = getattr(coordinator, 'hass', None)
    async def async_added_to_hass(self):
        self.coordinator.async_add_listener(self._handle_coordinator_update)
    def _handle_coordinator_update(self):
        self.async_write_ha_state()
uc.CoordinatorEntity = CoordinatorEntity
# HotataCoordinator subscripts the base class: DataUpdateCoordinator[...].
DataUpdateCoordinator.__class_getitem__ = classmethod(lambda cls, item: cls)

# ---- switch platform support (capability-gating tests) ----

sw = mod('homeassistant.components.switch')
class SwitchDeviceClass:
    SWITCH = 'switch'
    OUTLET = 'outlet'

@dataclasses.dataclass(frozen=True)
class EntityDescription:
    """The EntityDescription base the real HA platform descriptions inherit.

    The integration's descriptions are `@dataclass(frozen=True, kw_only=True)`
    subclasses, and generated dataclass __init__s only accept fields that are
    themselves dataclass fields — so this base must be one too, and must
    declare the common fields the integration passes.
    """
    key: str | None = None
    name: str | None = None
    icon: str | None = None
    device_class: object = None
    translation_key: str | None = None
    entity_registry_enabled_default: bool = True

class SwitchEntityDescription(EntityDescription):
    pass
class SwitchEntity(_StateWriter):
    _attr_has_entity_name = True
sw.SwitchDeviceClass = SwitchDeviceClass
sw.SwitchEntityDescription = SwitchEntityDescription
sw.SwitchEntity = SwitchEntity
comp.switch = sw
