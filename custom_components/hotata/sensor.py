"""Sensor entities for Hotata devices (all product lines)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTemperature,
    UnitOfTime,
)

try:  # HA >= 2025.1 renamed the density constants under UnitOfDensity.
    from homeassistant.const import UnitOfDensity

    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER = (
        UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
    )
    CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER = (
        UnitOfDensity.MILLIGRAMS_PER_CUBIC_METER
    )
except ImportError:  # pragma: no cover - older HA keeps the legacy names
    from homeassistant.const import (
        CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER,
    )
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    LOCK_PRODUCT_KEYS,
    MODEL_AIR_DRYING,
    MODEL_DISINFECTION,
    MODEL_HOT_DRYING,
)
from .coordinator import HotataCoordinator
from .entity import (
    HotataEntity,
    async_setup_dynamic_entities,
    entity_identity,
    has_property,
    property_value,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HotataSensorDescription(SensorEntityDescription):
    """Describe one reported property."""

    value_map: dict[int, str] | None = None
    supported_models: frozenset[int] | None = None


def _sensor(
    key: str,
    name: str,
    *,
    unit: str | None = None,
    device_class: SensorDeviceClass | None = None,
    icon: str | None = None,
    value_map: dict[int, str] | None = None,
    diagnostic: bool = False,
    supported_models: frozenset[int] | None = None,
) -> HotataSensorDescription:
    return HotataSensorDescription(
        key=key,
        name=name,
        native_unit_of_measurement=unit,
        device_class=device_class,
        state_class=(
            SensorStateClass.MEASUREMENT
            if unit is not None and value_map is None
            else None
        ),
        icon=icon,
        value_map=value_map,
        entity_category=EntityCategory.DIAGNOSTIC if diagnostic else None,
        supported_models=supported_models,
    )


SENSORS: tuple[HotataSensorDescription, ...] = (
    _sensor(
        "Position",
        "衣杆位置",
        icon="mdi:arrow-up-down",
        value_map={0: "无此功能", 1: "顶部", 2: "中间", 3: "底部"},
    ),
    _sensor(
        "DeviceModelType",
        "设备型号",
        value_map={
            0: "照明、消毒、风干、烘干",
            1: "照明",
            2: "照明、消毒",
            3: "照明、消毒、风干",
        },
        diagnostic=True,
    ),
    _sensor("Brand", "品牌", value_map={0: "好太太", 1: "科徕尼"}, diagnostic=True),
    *(
        _sensor(
            key,
            name,
            unit=UnitOfTime.MINUTES,
            icon="mdi:timer-outline",
            supported_models=models,
        )
        for key, name, models in (
            ("LightRemainingTime", "照明剩余时间", None),
            ("DisinfectionRemainingTime", "消毒剩余时间", MODEL_DISINFECTION),
            ("AirDryingRemainingTime", "风干剩余时间", MODEL_AIR_DRYING),
            ("DryingRemainingTime", "烘干剩余时间", MODEL_HOT_DRYING),
            ("IonsRemainingTime", "负离子剩余时间", None),
            ("RemainingWorkTime", "剩余工作时间", None),
            ("TargetRunningTime", "目标运行时间", None),
            ("WorkTime", "工作时长", None),
            ("WorkRemainingTime", "剩余工作时间", None),
        )
    ),
    *(
        _sensor(
            key,
            name,
            unit=UnitOfTemperature.CELSIUS,
            device_class=SensorDeviceClass.TEMPERATURE,
        )
        for key, name in (
            ("mtemp", "温度"),
            ("Temperature", "温度"),
            ("CurrentTemperature", "当前温度"),
        )
    ),
    _sensor(
        "mhumi",
        "湿度",
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    _sensor(
        "Humidity",
        "湿度",
        unit=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    _sensor(
        "PM25",
        "PM2.5",
        unit=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        device_class=SensorDeviceClass.PM25,
    ),
    _sensor(
        "HCHO",
        "甲醛",
        unit=CONCENTRATION_MILLIGRAMS_PER_CUBIC_METER,
        icon="mdi:molecule",
    ),
    *(
        _sensor(
            key,
            name,
            unit=PERCENTAGE,
            device_class=SensorDeviceClass.BATTERY,
            diagnostic=True,
        )
        for key, name in (
            ("BatteryPercentage", "电量"),
            ("BatteryLevel", "电量"),
        )
    ),
    *(
        _sensor(
            key,
            "信号强度",
            unit=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
            device_class=SensorDeviceClass.SIGNAL_STRENGTH,
            diagnostic=True,
        )
        for key in ("RSSI", "WiFi_RSSI")
    ),
    _sensor("WaterLevel", "水位", icon="mdi:waves"),
    _sensor("PH", "pH", icon="mdi:ph"),
    _sensor("Nitrogen", "氮含量", icon="mdi:leaf"),
    _sensor("Phosphorus", "磷含量", icon="mdi:leaf"),
    _sensor("Potassium", "钾含量", icon="mdi:leaf"),
    _sensor("RunningState", "运行状态"),
    _sensor("PlantName", "植物名称"),
    _sensor("PlantType", "植物类型"),
    _sensor("FunctionStatus", "功能状态"),
    _sensor("ModelFunctionList", "功能列表", diagnostic=True),
    _sensor("CurrentPositionPoint", "当前位置点"),
    _sensor("SlavePosition", "副杆位置"),
    _sensor("BestPickUpPosition", "最佳取衣位置"),
    _sensor("BestSunCurePosition", "最佳晾晒位置"),
    _sensor("ClothesWeight", "衣物重量"),
    _sensor("DryingMode", "烘干模式"),
    _sensor("LightMode", "照明模式"),
    _sensor("DayLightColour", "日光颜色"),
    _sensor("RiseTime", "上升时间", unit=UnitOfTime.SECONDS),
    _sensor("FallTime", "下降时间", unit=UnitOfTime.SECONDS),
    _sensor("McuHardwareVersion", "MCU 硬件版本", diagnostic=True),
    _sensor("McuSoftwareVersion", "MCU 软件版本", diagnostic=True),
    _sensor("ErrorCode", "故障代码", diagnostic=True),
    _sensor("WorkMode", "工作模式"),
    _sensor("ClothingType", "衣物类型"),
    _sensor("OperationControl", "运行控制"),
    _sensor("Error_Code", "故障代码", diagnostic=True),
    _sensor("playMode", "播放模式"),
    _sensor("bluetooth", "蓝牙状态"),
    _sensor("sceneMusic", "场景音乐"),
    _sensor("ArmMode", "布防模式"),
    _sensor("AlarmSoundID", "报警声音"),
    _sensor("AlarmSoundNames", "报警声音列表", diagnostic=True),
    _sensor("DoorBellSoundID", "门铃声音"),
    _sensor("DoorBellSoundNames", "门铃声音列表", diagnostic=True),
    _sensor("StreamVideoQuality", "视频清晰度"),
    _sensor("MotionDetectSensitivity", "移动侦测灵敏度"),
)

LOCK_SENSORS: tuple[HotataSensorDescription, ...] = tuple(
    _sensor(key, name, diagnostic=diagnostic)
    for key, name, diagnostic in (
        ("LockBodyState", "锁体状态", False),
        ("LockState", "门锁状态", False),
        ("InnerLockState", "反锁状态", False),
        ("AutoLockState", "自动上锁状态", False),
        ("ActiveDefenseState", "主动防御状态", False),
        ("ActiveDefenseTime", "主动防御时长", False),
        ("DNDMode", "免打扰模式", False),
        ("HolidayMode", "假期模式", False),
        ("HolidayModeState", "假期模式状态", False),
        ("HolidayStartTime", "假期开始时间", False),
        ("HolidayEndTime", "假期结束时间", False),
        ("MuteState", "静音状态", False),
        ("DoorOpenWay", "开门方式", False),
        ("FirmwareVersion", "固件版本", True),
        ("DeviceModel", "设备型号", True),
        ("FingerPrintCount", "指纹数量", True),
        ("PasswordCount", "密码数量", True),
        ("CardCount", "门卡数量", True),
        ("Volume", "音量", True),
    )
)


def _model_type(device):
    value = property_value(device, "DeviceModelType")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entities(coordinator: HotataCoordinator, device):
    yield HotataIntegrationStatusSensor(coordinator, device)
    yield HotataDeviceSensor(coordinator, device)
    descriptions = SENSORS + (
        LOCK_SENSORS if device.product_key in LOCK_PRODUCT_KEYS else ()
    )
    model = _model_type(device)
    for description in descriptions:
        if not has_property(device, description.key):
            continue
        if (
            description.supported_models is not None
            and model is not None
            and model not in description.supported_models
        ):
            _LOGGER.debug(
                "Skipping sensor %s: model type %s lacks it",
                description.key,
                model,
            )
            continue
        yield HotataPropertySensor(coordinator, device, description)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create and dynamically discover sensors."""
    coordinator: HotataCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_setup_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda device: _entities(coordinator, device),
    )


class HotataPropertySensor(HotataEntity, SensorEntity):
    """Expose one reported device property."""

    entity_description: HotataSensorDescription

    def __init__(
        self, coordinator: HotataCoordinator, device, description
    ) -> None:
        super().__init__(coordinator, device)
        self.entity_description = description
        self._attr_unique_id = entity_identity(device, description.key)

    @property
    def native_value(self) -> Any:
        value = self.property_value(self.entity_description.key)
        value_map = self.entity_description.value_map
        if value_map is None:
            return value
        try:
            return value_map.get(int(value), str(value))
        except (TypeError, ValueError):
            return value


class HotataIntegrationStatusSensor(HotataEntity, SensorEntity):
    """Integration health: auth/rate-limit/connection state + failover."""

    _attr_translation_key = "integration_status"
    _attr_icon = "mdi:alert-circle"

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "integration_status")

    @property
    def available(self) -> bool:
        """Always available so the user can see the error state."""
        return True

    @property
    def native_value(self) -> str:
        """Return the machine-readable error key (translated by HA)."""
        issues = self.coordinator.account.active_issues
        if "rate_limited" in issues:
            return "rate_limited"
        if "connection_error" in issues:
            return "connection_error"
        return "normal"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose failover state for diagnostics."""
        account = self.coordinator.account
        return {
            "failover_active": account.using_backup,
            "active_issues": account.active_issues or None,
        }


class HotataDeviceSensor(HotataEntity, SensorEntity):
    """Diagnostic sensor dumping the raw cloud-reported properties."""

    _attr_name = "状态"
    _attr_icon = "mdi:cloud-search"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # The raw dump changes on every poll and is purely for debugging — keep
    # it out of the recorder to avoid database bloat.
    _attr_recorder_exclude = True

    def __init__(self, coordinator: HotataCoordinator, device) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = entity_identity(device, "status")

    @property
    def native_value(self) -> int:
        """Return the number of raw properties the cloud reports."""
        return len(self.current_device.properties)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device = self.current_device
        return {
            "iot_id": device.iot_id,
            "product_key": device.product_key,
            "device_name": device.device_name,
            "parent_iot_id": device.parent_iot_id,
            "properties": device.properties,
        }
