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

# ---- polling / rate limiting (this project's own) ----

# Dynamic polling: poll fast only while a motor runs or right after a control
# command; the cloud answers 403 (操作过于频繁) when polled too hard.
POLL_INTERVAL_FAST = 5
POLL_INTERVAL_SLOW = 30
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

# ---- device model capability gating ----
# DeviceModelType semantics from the Hotata product line:
#   0 = 照明+消毒+风干+烘干, 1 = 照明, 2 = 照明+消毒, 3 = 照明+消毒+风干
MODEL_HOT_DRYING = frozenset({0})
MODEL_AIR_DRYING = frozenset({0, 3})
MODEL_DISINFECTION = frozenset({0, 2, 3})

# Product keys classify a device into a product line. Classification is
# intentionally independent of the translated product names.

AIRER_PRODUCT_KEYS = {"a1kM9JAZ7aQ", "a1H7OfeWFTS"}
ADVANCED_AIRER_PRODUCT_KEYS = {"a1abYBCSVlV", "a1WWvhXa6HQ"}
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

SERVICE_SET_PROPERTY = "set_property"
SERVICE_INVOKE_SERVICE = "invoke_service"
SERVICE_QUERY = "query"

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
