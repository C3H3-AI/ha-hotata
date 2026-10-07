"""Constants for the Hotata Airer integration.

The transport talks to the Aliyun Link IoT API Gateway used by 好太太智联
3.5.8, after exchanging vendor-account credentials for an IoT token. Account
login, failover, and rate-limit handling are this project's own.
"""

from datetime import timedelta

DOMAIN = "hotata"
NAME = "Hotata"

PLATFORMS = [
    "binary_sensor",
    "button",
    "cover",
    "event",
    "light",
    "media_player",
    "number",
    "select",
    "sensor",
    "switch",
]

# ---- Aliyun Link IoT gateway protocol constants (好太太智联 3.5.8) ----

CONF_IOT_TOKEN = "iot_token"
CONF_IOT_REFRESH_TOKEN = "iot_refresh_token"
CONF_IDENTITY_ID = "identity_id"
CONF_REGISTERED_ID = "registered_id"

APP_KEY = "25106490"
APP_SECRET = "2fea71761db578d00fa23ac4a4ccb060"
APP_VERSION = "3.5.8"
API_HOST = "api.link.aliyun.com"
ACCOUNT_HOST = "saas.keyoo.com"
OPEN_ACCOUNT_HOST = "sdk.openaccount.aliyun.com"

# Protocol material taken from version 3.5.8 of the public Android
# application. These are application constants, not user credentials, and they
# live here (rather than beside their only user) so the standalone diagnostic
# tool under tools/ can import this module without pulling in aiohttp or Home
# Assistant.
AES_KEY = b"SnqUuPDWy5wusGG7"
AES_IV = b"tvGjXli9WjpfOmNK"
ACCOUNT_PRIVATE_KEY = (
    "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCXAsmTBgCKxOZ3"
    "okMNkjw9h6X2BD5CJ8sQhNBGBoTEUf3USNbnLiN9gpYLCziK50M5BsOAIADqxbsN"
    "/K7cYwNMtoKFKiTqTajM4tAJ3LKL1MlpEZM7uPjS1EKi9WNXalnfaI+9VrnuHXiA"
    "Zc9idZdx4oxeD4PwKHjKzqIFNHC9WrvoofUabZkzrfSjygiJKUSeWGHtyPB/YC+r"
    "t1lGFGMZcFY5BX4ww1EquWeulzoWQcOsKwjUDmU5KM5HwUt1z7fFtN1XXM4tTAow"
    "Z08mmMorDQso9icMX0jCbraRX0HL9q6eK8jjeFFhcMYDX2rcM2+8X9Zd/56SRGIm"
    "jP+sdCs7AgMBAAECggEAHb4izZ5lBO/7JJ0E7+tZihTpjycOzCDiUgKWsvQduj0b"
    "7W/bQ/VGcDYEL3CqVlFuYBEA+H9VLuh7Cyo1lpq5z6Yy1t+SHcPl91TE/OxHDlt+"
    "v/8CLMUl3QCJj2cdhd4gjWwew4ANZuTPExr6Wb4ncfrZAr2zkt2lzOwd5UCK5ABp"
    "dKNozwC+Gpt7RV5nFw8dqL1ODH7q6zGVEEQWA9WG9LV6zrv1dfuP4X1X6xl1USdc"
    "WZbJql7Zw1acXC7DSpmd4pqRhp0Dn0iL8x3fRMgXMD1aEc7aTRqgkR02y8CdlX9v"
    "CpW6GYSImn3YbngL/GTzZIEaxnM/ejnd57iaEJ56wQKBgQDNKJUdMOSbpQ1SKMyx"
    "iCvophlF40r1kLPQ+JrIM2V7RuTSxUUJhk9xI5l9+RdpvRb4bhMvBsCwy+vHcDk4"
    "bhv55snkF0+R2wz2UsnvgIPOx8Aju4ojkfgOdW0pQh4sQGykbWjEQ767q7vfhHVC"
    "eHHpylT2RVLkcapLiy8RK+VpIwKBgQC8bwlS/E5DiidNBkLTDEpLtbVqYkq+hRME"
    "yAg2ep2OX1kl6sWwC8Vj2sEqCY/9MplZ3DdcHocjNU2IkksUWgeBVk0ushQsIcWj"
    "OQv+GUhjiuAs28CoP64dvzT1xNV8PIF2HpRv+SheHkuSFOtg3UWc7CmC5Ea/uwgy"
    "V1SxoaCzCQKBgQCvG0FSvgWRt2nMQ1ia+tAHbaXKmfrD6DMiXN63m+61LshmAcww"
    "Gfw6ZBlBhVbvgF5XwpQLImdbP2JKQsYEHS8xuEN/tEnNAztoDzeefYGC/8lGdm6s"
    "d41SwfVfLrjUKlTQbzXptqzYP/dGCyeOiYEo+/JSlM7wfvfMLMsKi/3uIwKBgQCP"
    "myfGANc8jdtpzi27XhB5JqB91S8Vh6F48WGg802EJZJxXT0P78idUygHe4Yq9xb7"
    "7uKZ6AIhiQvv214wwnQZ08W6oqjRAWP4Aw/qtSYABuTWCxwGnZF6xi/8Zeg1aH9Z"
    "n/CMbZygLgJ18E96YOgesbTpNkPc9xNGGlxHi+BG0QKBgC5CtDrJrzqBNlxjRBM9"
    "gKF3b/T2HotQEDOB5V6uwgWUq0m2E2XOPMFe7Qw2jp2Ki+a8Utz+6DRfcpAeFM+D"
    "h0nf8Ue1UxPTYHPPN4pfKdODpcTNn0XIhQS6OwmD5sUApF3D1ew1K1cECU1bjlT2"
    "F1Sws4xEH+OMGrhQadNNtG1z"
)

# ---- polling / rate limiting (this project's own) ----

# Dynamic polling: poll fast only while a motor runs or right after a control
# command; the cloud answers 403 (操作过于频繁) when polled too hard.
POLL_INTERVAL_FAST = 5
POLL_INTERVAL_SLOW = 30
# While any known device is offline, poll only its connectivity at this
# interval and skip the (pointless) property/TSL reads it would reject.
POLL_INTERVAL_OFFLINE = 30
POLL_ACTIVE_WINDOW = 70
# After a 403 (操作过于频繁) the server keeps rejecting for a long window, and
# every request during the penalty may extend it — go fully silent for 24h.
RATE_LIMIT_BACKOFF = 86400
# Backup-account failover tuning.
FAILOVER_SETTLE_DELAY = 60
SWITCH_BACK_PROBE_DELAY = 600
SWITCH_BACK_PROBE_MAX = 3600

# ---- config entry keys (this project's own) ----

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_BACKUP_USERNAME = "backup_username"
CONF_BACKUP_PASSWORD = "backup_password"
CONF_DESCENT_TIME = "descent_time"

DEFAULT_NAME = "好太太晾衣机"
DEFAULT_DESCENT_TIME = 10  # 从顶降到底的秒数，0=禁用模拟

# Airer capability resolution (ModelFunctionList bit table, then
# DeviceModelType) lives in capabilities.py so the table stays in one place.

# Product keys classify a device into a product line. Classification is
# intentionally independent of the translated product names.

# Airer generations. Verified 2026-10-07 against the vendor's own dispatch
# table (mini-program libs/hotata/smartlock.js -> exports.DEVICE, which keys
# every cloud device by productKey) and against live cloud probes; V1..V5 are
# exactly the generations that table lists as 智能晾衣机. a1H7OfeWFTS is a
# legacy key this integration has always accepted and is kept deliberately.
AIRER_V1_PRODUCT_KEYS = {"a1kM9JAZ7aQ"}
AIRER_V2_PRODUCT_KEYS = {"a1abYBCSVlV"}
AIRER_V3_PRODUCT_KEYS = {"a1WWvhXa6HQ"}
AIRER_V4_PRODUCT_KEYS = {"a1VoPdoApAu"}
AIRER_V5_PRODUCT_KEYS = {"p11wW9"}
AIRER_LEGACY_PRODUCT_KEYS = {"a1H7OfeWFTS"}
AIRER_PRODUCT_KEYS = (
    AIRER_V1_PRODUCT_KEYS
    | AIRER_V2_PRODUCT_KEYS
    | AIRER_V3_PRODUCT_KEYS
    | AIRER_V4_PRODUCT_KEYS
    | AIRER_V5_PRODUCT_KEYS
    | AIRER_LEGACY_PRODUCT_KEYS
)
# Behaviour tier rather than family membership: these are the airers observed
# to publish no DeviceModelType, so they need the report-based fallback.
ADVANCED_AIRER_PRODUCT_KEYS = AIRER_V2_PRODUCT_KEYS | AIRER_V3_PRODUCT_KEYS
CURTAIN_V1_PRODUCT_KEYS = {"a17WHir5dlN"}
CURTAIN_V2_PRODUCT_KEYS = {"a1E5ITXkQw6"}
SOCKET_PRODUCT_KEYS = {"a1xi9Nj7jUH", "a1rsEIoiUZD", "a1FRkk35VSi"}
WALL_SWITCH_PRODUCT_KEYS = {"a1W0CebUJX4", "a1WgY08kU9y", "a1xxF0aaqBv"}
LIGHT_BAND_WHITE_PRODUCT_KEYS = {"a1nKK73koGo", "a1nKK73koGo_group"}
LIGHT_BAND_COLOR_PRODUCT_KEYS = {"a1o5g4N0EOr", "a1o5g4N0EOr_group"}
LIGHT_BAND_PRODUCT_KEYS = (
    LIGHT_BAND_WHITE_PRODUCT_KEYS | LIGHT_BAND_COLOR_PRODUCT_KEYS
)
MOTION_SENSOR_PRODUCT_KEYS = {"a15KqYh4n2f", "a1L7Pstvrhj"}
CONTACT_SENSOR_PRODUCT_KEYS = {"a1cITmQuOai"}
SMOKE_SENSOR_PRODUCT_KEYS = {"a1zdsVisxqY"}
GAS_SENSOR_PRODUCT_KEYS = {"a12sQMkI5Dh"}
WATER_SENSOR_PRODUCT_KEYS = {"a1DAiDOYprL"}
AIR_QUALITY_PRODUCT_KEYS = {"a1ojaGmf3va"}
INDOOR_ALARM_PRODUCT_KEYS = {"a1sHZqr1IiR"}
EVENT_BUTTON_PRODUCT_KEYS = {"a1KyglAQqvG", "a1dhQgoXjr1"}
TOWEL_RACK_PRODUCT_KEYS = {"a1tNp9QZYEL", "a1nDeCURxQp", "a1zN4tJD2gg"}
CLOTHES_CARE_PRODUCT_KEYS = {"a1ogWpmeueH"}
PLANT_PRODUCT_KEYS = {"a1pIbDFWFgQ"}
MUSIC_PRODUCT_KEYS = {"a1rWuGK2aSl"}
BROADCAST_PRODUCT_KEYS = {"a1NW6w7RDJC"}
GATEWAY_PRODUCT_KEYS = {"a1yXG371Sef", "a17eoHyvkWF", "a1NilHk1AlO"}
CAMERA_PRODUCT_KEYS = {"a1I9kGdhmMX"}
LOCK_PRODUCT_KEYS = {
    "a1r3HsGSRlJ",
    "a1P8QYv0ZfY",
    "d3dr3slkb2moukaa",
    "10ku5njsp8xsytid",
    "ggiwj0my6n6kld4l",
    "a15MITOvKJs",
    "a1dousih28N",
    "a14P0bNnVGK",
    "a1We1IBKmhg",
    "9CVBLHNPYE",
    "S0YEYJPNF2",
    "6GHRSFPHF2",
    "8TBLTNQ7Y4",
    "a1fPIoHEXPZ",
    "a1wpv5P6ruV",
    "a1DuSMZVeGy",
    "a1F7hA3J6TI",
    "a1z6OuFHnCS",
    "a1vym9itSi8",
    "a1ICeu22OF6",
    "a1iiL3cvM33",
    "a1Rf7G2N1oK",
    "98D3WJ6WGP",
    "GX784V2PCO",
    "3JAWFEBH12",
    "7BCANSHA33",
    "S0L3SUM7N6",
    "a15O5BgUcG2",
}

# ---- device identification ----
# Source gateway a product key is registered on. Taken from the vendor's own
# dispatch table (mini-program libs/hotata/smartlock.js: `SourceType` and the
# per-key `from` field), i.e. this is how the real app tells one device family
# from another. The family decides which gateway endpoints serve a device.
SOURCE_PLATFORM_ALI = 1
SOURCE_PLATFORM_LANSHENG = 2
SOURCE_PLATFORM_TUYA = 3
SOURCE_PLATFORM_TENCENT = 4
SOURCE_PLATFORM_INFRARED = 5
SOURCE_PLATFORM_BLE = 6
SOURCE_PLATFORM_GROUP = 7
SOURCE_PLATFORM_YIYUAN = 8

SOURCE_PLATFORM_NAMES: dict[int, str] = {
    SOURCE_PLATFORM_ALI: "ali",
    SOURCE_PLATFORM_LANSHENG: "lansheng",
    SOURCE_PLATFORM_TUYA: "tuya",
    SOURCE_PLATFORM_TENCENT: "tencent",
    SOURCE_PLATFORM_INFRARED: "infrared",
    SOURCE_PLATFORM_BLE: "ble",
    SOURCE_PLATFORM_GROUP: "group",
    SOURCE_PLATFORM_YIYUAN: "yiyuan",
}

SOURCE_PLATFORM_BY_PRODUCT_KEY: dict[str, int] = {
    # airers
    "a1kM9JAZ7aQ": SOURCE_PLATFORM_ALI,
    "a1abYBCSVlV": SOURCE_PLATFORM_ALI,
    "a1WWvhXa6HQ": SOURCE_PLATFORM_ALI,
    "a1VoPdoApAu": SOURCE_PLATFORM_BLE,
    "p11wW9": SOURCE_PLATFORM_YIYUAN,
    # locks
    "8TBLTNQ7Y4": SOURCE_PLATFORM_TENCENT,
    "S0YEYJPNF2": SOURCE_PLATFORM_TENCENT,
    "6GHRSFPHF2": SOURCE_PLATFORM_TENCENT,
    "9CVBLHNPYE": SOURCE_PLATFORM_TENCENT,
    "GX784V2PCO": SOURCE_PLATFORM_TENCENT,
    "3JAWFEBH12": SOURCE_PLATFORM_TENCENT,
    "98D3WJ6WGP": SOURCE_PLATFORM_TENCENT,
    "S0L3SUM7N6": SOURCE_PLATFORM_TENCENT,
    "7BCANSHA33": SOURCE_PLATFORM_TENCENT,
    "d3dr3slkb2moukaa": SOURCE_PLATFORM_TUYA,
    "10ku5njsp8xsytid": SOURCE_PLATFORM_TUYA,
}

FAMILY_AIRER = "airer"
FAMILY_LOCK = "lock"
FAMILY_CURTAIN = "curtain"
FAMILY_SOCKET = "socket"
FAMILY_WALL_SWITCH = "wall_switch"
FAMILY_LIGHT_BAND = "light_band"
FAMILY_SENSOR = "sensor"
FAMILY_TOWEL_RACK = "towel_rack"
FAMILY_EVENT_BUTTON = "event_button"
FAMILY_GATEWAY = "gateway"
FAMILY_CAMERA = "camera"
FAMILY_OTHER = "other"

_PRODUCT_KEY_FAMILIES: tuple[tuple[str, set[str]], ...] = (
    (FAMILY_AIRER, AIRER_PRODUCT_KEYS),
    (FAMILY_LOCK, LOCK_PRODUCT_KEYS),
    (FAMILY_CURTAIN, CURTAIN_V1_PRODUCT_KEYS | CURTAIN_V2_PRODUCT_KEYS),
    (FAMILY_SOCKET, SOCKET_PRODUCT_KEYS),
    (FAMILY_WALL_SWITCH, WALL_SWITCH_PRODUCT_KEYS),
    (FAMILY_LIGHT_BAND, LIGHT_BAND_PRODUCT_KEYS),
    (
        FAMILY_SENSOR,
        MOTION_SENSOR_PRODUCT_KEYS
        | CONTACT_SENSOR_PRODUCT_KEYS
        | SMOKE_SENSOR_PRODUCT_KEYS
        | GAS_SENSOR_PRODUCT_KEYS
        | WATER_SENSOR_PRODUCT_KEYS
        | AIR_QUALITY_PRODUCT_KEYS
        | INDOOR_ALARM_PRODUCT_KEYS,
    ),
    (FAMILY_TOWEL_RACK, TOWEL_RACK_PRODUCT_KEYS),
    (FAMILY_EVENT_BUTTON, EVENT_BUTTON_PRODUCT_KEYS),
    (FAMILY_GATEWAY, GATEWAY_PRODUCT_KEYS),
    (FAMILY_CAMERA, CAMERA_PRODUCT_KEYS),
    (
        FAMILY_OTHER,
        CLOTHES_CARE_PRODUCT_KEYS
        | PLANT_PRODUCT_KEYS
        | MUSIC_PRODUCT_KEYS
        | BROADCAST_PRODUCT_KEYS,
    ),
)

PRODUCT_FAMILY_BY_PRODUCT_KEY: dict[str, str] = {
    product_key: family
    for family, product_keys in _PRODUCT_KEY_FAMILIES
    for product_key in product_keys
}


def product_family(product_key: str | None) -> str | None:
    """Return the device family for a product key, or None when unknown.

    Unknown keys deliberately return None instead of a guess: the caller keeps
    its legacy behaviour rather than suppressing entities for a device the
    integration has simply never seen.
    """
    if not product_key:
        return None
    return PRODUCT_FAMILY_BY_PRODUCT_KEY.get(product_key)


def source_platform(product_key: str | None) -> int | None:
    """Return the gateway family a product key is registered on, if known."""
    if not product_key:
        return None
    return SOURCE_PLATFORM_BY_PRODUCT_KEY.get(product_key)


SERVICE_SET_PROPERTY = "set_property"
SERVICE_INVOKE_SERVICE = "invoke_service"
SERVICE_QUERY = "query"
SERVICE_EXPORT_CAPABILITIES = "export_capabilities"

# Read-only API calls confirmed in FYApi/FYSDK from 好太太智联 3.5.8.
# Values are (path, API version). The generic query service accepts keys only
# from this mapping so it cannot be used to reach mutation endpoints.
READ_ONLY_QUERIES: dict[str, tuple[str, str]] = {
    "device_info": ("/thing/info/get", "1.0.2"),
    "device_properties": ("/thing/properties/get", "1.0.2"),
    "device_status": ("/thing/status/get", "1.0.2"),
    "thing_model": ("/thing/tsl/get", "1.0.2"),
    "bindings_by_device": ("/uc/listBindingByDev", "1.0.2"),
    "account_device_binding": ("/uc/getByAccountAndDev", "1.0.2"),
    "subdevices": ("/subdevices/list", "1.0.2"),
    "scene_list": ("/scene/list/get", "1.0.2"),
    "scene_info": ("/scene/info/get", "1.0.2"),
    "scene_logs": ("/scene/log/list/get", "1.0.2"),
    "property_timeline": ("/thing/property/timeline/get", "1.0.2"),
    "event_timeline": ("/thing/event/timeline/get", "1.0.2"),
    "lock_event_history": ("/lock/event/history/query", "1.0.0"),
    "ota_info": ("/thing/ota/info/queryByUser", "1.0.2"),
    "device_notices": ("/message/center/device/notice/list", "1.0.5"),
    "home_list": ("/living/home/query", "1.1.0"),
    "control_groups": ("/living/home/controlgroup/query", "1.0.1"),
    "control_group_devices": (
        "/living/home/controlgroup/device/query",
        "1.0.3",
    ),
    "control_group_properties": (
        "/living/controlgroup/properties/get",
        "1.0.1",
    ),
}
