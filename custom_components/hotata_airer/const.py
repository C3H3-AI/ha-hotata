"""Constants for Hotata Airer integration."""

# Dynamic polling: poll fast only while the motor runs or right after a
# control command, slow otherwise. The server answers 403 (操作过于频繁) when
# polled too hard, so steady-state request volume must stay low.
POLL_INTERVAL_FAST = 5
POLL_INTERVAL_SLOW = 30
# Re-send an active window after each control command so the resulting motor
# movement is tracked at fast rate until it settles.
POLL_ACTIVE_WINDOW = 70
# Online status changes rarely; checking it every poll doubles the request
# volume for no benefit.
ONLINE_CHECK_INTERVAL = 120
# After a 403 (操作过于频繁) the server keeps rejecting for a long window, and
# every request during the penalty may extend it — go fully silent for 24h.
RATE_LIMIT_BACKOFF = 86400
API_BASE = "https://saas.keyoo.com/app-api/v2.0"
API_LOGIN_PASSWORD = f"{API_BASE}/login/password"
API_REFRESH_TOKEN = f"{API_BASE}/login/spLogin/refreshToken"
API_DEVICE_LIST = f"{API_BASE}/sp/device/getSpDeviceList"
API_PROPERTY_GET = f"{API_BASE}/device/property/get"
API_PROPERTY_SET = f"{API_BASE}/device/property/set2"
API_INVOKE2 = f"{API_BASE}/device/service/invoke2"
API_ONLINE_STATUS = f"{API_BASE}/device/synOnlineStatus"

# Mini-program parameters (for device control & token refresh)
APP_KEY = "miniapp-hotata-prod"
APP_SECRET = "B322B40A-DBD2-26A2-F935-6E760917CB73"
APP_VERSION = "miniapp_4.4.6.1"
IMEI = "Windows Unknown x64_w4.1.10.53_s3.16.1"
PHONE_MODEL = "microsoft"
SYS_VERSION = "Windows Unknown x64"

# App login parameters (AES key, IV, and RSA private key for login signing).
# Obtained from third-party reverse engineering work of the Hotata App;
# not extracted by this project's authors.
APP_VERSION_APP = "3.5.8"
AES_KEY = b"SnqUuPDWy5wusGG7"
AES_IV = b"tvGjXli9WjpfOmNK"
# RSA private key (DER, base64-encoded) — source: third-party RE work
ACCOUNT_PRIVATE_KEY = (
    "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCXAsmTBgCKxOZ3"
    "okMNkjw9h6X2BD5CJ8sQhNBGBoTEUf3USNbnLiN9gpYLCziK50M5BsOAIADqxbsN"
    "/K7cYwNMtoKFKiTqTajM4tAJ3LKL1MlpEZM7uPjS1EKi9WNXalnfaI+9VrnuHXi"
    "AZc9idZdx4oxeD4PwKHjKzqIFNHC9WrvoofUabZkzrfSjygiJKUSeWGHtyPB/YC+"
    "rt1lGFGMZcFY5BX4ww1EquWeulzoWQcOsKwjUDmU5KM5HwUt1z7fFtN1XXM4tTA"
    "owZ08mmMorDQso9icMX0jCbraRX0HL9q6eK8jjeFFhcMYDX2rcM2+8X9Zd/56SR"
    "GImjP+sdCs7AgMBAAECggEAHb4izZ5lBO/7JJ0E7+tZihTpjycOzCDiUgKWsvQdu"
    "j0b7W/bQ/VGcDYEL3CqVlFuYBEA+H9VLuh7Cyo1lpq5z6Yy1t+SHcPl91TE/OxH"
    "Dlt+v/8CLMUl3QCJj2cdhd4gjWwew4ANZuTPExr6Wb4ncfrZAr2zkt2lzOwd5UC"
    "K5ABpdKNozwC+Gpt7RV5nFw8dqL1ODH7q6zGVEEQWA9WG9LV6zrv1dfuP4X1X6x"
    "l1USdcWZbJql7Zw1acXC7DSpmd4pqRhp0Dn0iL8x3fRMgXMD1aEc7aTRqgkR02y"
    "8CdlX9vCpW6GYSImn3YbngL/GTzZIEaxnM/ejnd57iaEJ56wQKBgQDNKJUdMOSb"
    "pQ1SKMyxiCvophlF40r1kLPQ+JrIM2V7RuTSxUUJhk9xI5l9+RdpvRb4bhMvBsC"
    "wy+vHcDk4bhv55snkF0+R2wz2UsnvgIPOx8Aju4ojkfgOdW0pQh4sQGykbWjEQ"
    "767q7vfhHVCeHHpylT2RVLkcapLiy8RK+VpIwKBgQC8bwlS/E5DiidNBkLTDEpL"
    "tbVqYkq+hRMEyAg2ep2OX1kl6sWwC8Vj2sEqCY/9MplZ3DdcHocjNU2IkksUWgeB"
    "Vk0ushQsIcWjOQv+GUhjiuAs28CoP64dvzT1xNV8PIF2HpRv+SheHkuSFOtg3UW"
    "c7CmC5Ea/uwgyV1SxoaCzCQKBgQCvG0FSvgWRt2nMQ1ia+tAHbaXKmfrD6DMiXN"
    "63m+61LshmAcwwGfw6ZBlBhVbvgF5XwpQLImdbP2JKQsYEHS8xuEN/tEnNAztoD"
    "zeefYGC/8lGdm6sd41SwfVfLrjUKlTQbzXptqzYP/dGCyeOiYEo+/JSlM7wfvfM"
    "LMsKi/3uIwKBgQCPmyfGANc8jdtpzi27XhB5JqB91S8Vh6F48WGg802EJZJxXT0P"
    "78idUygHe4Yq9xb77uKZ6AIhiQvv214wwnQZ08W6oqjRAWP4Aw/qtSYABuTWCxw"
    "GnZF6xi/8Zeg1aH9Zn/CMbZygLgJ18E96YOgesbTpNkPc9xNGGlxHi+BG0QKBgC"
    "5CtDrJrzqBNlxjRBM9gKF3b/T2HotQEDOB5V6uwgWUq0m2E2XOPMFe7Qw2jp2Ki"
    "+a8Utz+6DRfcpAeFM+Dh0nf8Ue1UxPTYHPPN4pfKdODpcTNn0XIhQS6OwmD5sUA"
    "pF3D1ew1K1cECU1bjlT2F1Sws4xEH+OMGrhQadNNtG1z"
)

DEFAULT_NAME = "好太太晾衣机"
DEFAULT_DESCENT_TIME = 10  # 从顶降到底的秒数，0=禁用模拟

# Config entry keys
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCESS_TOKEN = "access_token"
CONF_USER_ID = "userId"
CONF_IOT_ID = "iotId"
CONF_NAME = "name"
CONF_DESCENT_TIME = "descent_time"

DOMAIN = "hotata_airer"
