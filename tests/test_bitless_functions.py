"""Airer functions the vendor table has no bit for (issue #15 follow-up).

夜灯/人体感应/轻抬上升 have no FUN_INDEX bit, so the bit string cannot speak
for them. The TSL is a per-product-line *template*: the D-3072S declares
NightLightFunctionSwitch / BodyInductionLightSwitch / StokeUpSwitch as writable
while its own ModelFunctionList says 夜灯 = 0. Creating on the declaration
alone therefore put a night light on a machine that has none — the only
remaining evidence is the device's own property report, where a device that
really has the function publishes it (the same D-3072S reports
BestPickUpPositionSwitch = 1).

Run from anywhere:  python3 tests/test_bitless_functions.py
"""
import sys
import types
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

import ha_stub  # noqa: E402
import cover_harness  # noqa: E402

PKG = REPO_ROOT / "custom_components"
sys.path.insert(0, str(PKG))
_pkg = types.ModuleType("hotata")
_pkg.__path__ = [str(PKG / "hotata")]
sys.modules.setdefault("hotata", _pkg)

HotataDevice = __import__("hotata.models", fromlist=["HotataDevice"]).HotataDevice
switch_mod = __import__("hotata.switch", fromlist=["_switches_for_device"])
select_mod = __import__("hotata.select", fromlist=["_entities"])
capabilities = __import__("hotata.capabilities", fromlist=["declared_without_bit"])

R = cover_harness.Results()

# D-3072S 的真实位表（从设备上报抓的）：夜灯相关位 17/18/22 全为 0
D3072S_BITS = "00000000000000000011111100110000"


def bit_string(*bits_on: int, width: int = 32) -> str:
    """Build a ModelFunctionList the way the vendor numbers it.

    ``ifSupportFun`` counts from the RIGHT and is 1-based, so bit n lives at
    index ``width - n``.
    """
    chars = ["0"] * width
    for bit in bits_on:
        chars[width - bit] = "1"
    return "".join(chars)

# 该机型 TSL 里声明的"无位"属性（同样是实测）
D3072S_BITLESS_TSL = (
    "NightLightFunctionSwitch",
    "BodyInductionLightSwitch",
    "StokeUpSwitch",
    "BodyInductionSensitivitySwitch",
)


def make_device(properties, tsl_ids, product_key="a1abYBCSVlV"):
    return HotataDevice(
        iot_id="dev1",
        name="晾衣架",
        product_key=product_key,
        device_name="d1",
        online=True,
        raw={},
        properties=dict(properties),
        thing_model={"properties": [{"identifier": i} for i in tsl_ids]},
    )


def switch_keys(device):
    co = cover_harness.FakeCoordinator(device)
    return [
        getattr(getattr(e, "entity_description", None), "key", None)
        or getattr(e, "identifier", None)
        or getattr(e, "_attr_name", None)
        for e in switch_mod._switches_for_device(co, device)
    ]


def select_keys(device):
    co = cover_harness.FakeCoordinator(device)
    return [
        getattr(getattr(e, "entity_description", None), "key", None)
        or getattr(e, "identifier", None)
        or getattr(e, "_attr_name", None)
        for e in select_mod._entities(co, device)
    ]


def test_d3072s_gets_no_night_light_entities():
    """位表说没有夜灯 → 不该出现夜灯相关实体（尽管 TSL 声明了）。"""
    device = make_device(
        {"ModelFunctionList": D3072S_BITS, "PowerSwitch": 1},
        D3072S_BITLESS_TSL + ("PowerSwitch", "ModelFunctionList", "DisinfectionSwitch"),
    )
    got = switch_keys(device)
    R.check("no 夜灯功能", "NightLightFunctionSwitch" in got, False)
    R.check("no 人体感应夜灯", "BodyInductionLightSwitch" in got, False)
    R.check("no 轻抬上升（无功能位且未上报）", "StokeUpSwitch" in got, False)
    R.check("no 人体感应（位 18 = 0）", "BodyInductionSwitch" in got, False)
    got_sel = select_keys(device)
    R.check(
        "no 人体感应灵敏度 select",
        "BodyInductionSensitivitySwitch" in got_sel,
        False,
    )
    R.check("消毒开关仍在（位 6 = 1）", "DisinfectionSwitch" in got, True)


def test_reporting_alone_does_not_resurrect_a_capped_function():
    """设备上报了属性，但功能位说没有 → 以功能位为准（不建）。

    这正是 bug 的另一半：模板会同时提供属性上报和 TSL 声明，只有功能表能
    说明这台机器到底有没有夜灯。
    """
    device = make_device(
        {
            "ModelFunctionList": D3072S_BITS,
            "PowerSwitch": 1,
            "NightLightFunctionSwitch": 1,
        },
        D3072S_BITLESS_TSL + ("PowerSwitch", "ModelFunctionList"),
    )
    got = switch_keys(device)
    R.check("位 22 = 0 时，即使上报也不建夜灯",
            "NightLightFunctionSwitch" in got, False)


def test_best_position_switches_survive():
    """BestPickUp/BestSunCure 位表无位，但 D-3072S 确实上报，必须保留。"""
    device = make_device(
        {
            "ModelFunctionList": D3072S_BITS,
            "BestPickUpPositionSwitch": 1,
            "BestSunCurePositionSwitch": 1,
        },
        ("BestPickUpPositionSwitch", "BestSunCurePositionSwitch",
         "ModelFunctionList"),
    )
    got = switch_keys(device)
    R.check("最佳收衣点开关保留", "BestPickUpPositionSwitch" in got, True)
    R.check("最佳晒衣点开关保留", "BestSunCurePositionSwitch" in got, True)


def test_no_report_means_no_creation():
    """冷启动（还没上报）时不要凭空创建这类实体。"""
    device = make_device({}, D3072S_BITLESS_TSL + ("PowerSwitch",))
    got = switch_keys(device)
    R.check("未上报时不建夜灯", "NightLightFunctionSwitch" in got, False)


def test_night_light_appears_when_the_bit_says_so():
    """把位 22(夜灯)/17/18 置上 → 对应开关应当出现。"""
    bits22 = bit_string(5, 6, 10, 14, 15, 22)      # 含夜灯
    bits18 = bit_string(5, 6, 10, 14, 15, 17, 18)  # 含人体感应
    device = make_device(
        {"ModelFunctionList": bits22, "NightLightFunctionSwitch": 1},
        D3072S_BITLESS_TSL + ("PowerSwitch", "ModelFunctionList"),
    )
    got = switch_keys(device)
    R.check("位 22 = 1 → 夜灯功能出现", "NightLightFunctionSwitch" in got, True)

    device2 = make_device(
        {"ModelFunctionList": bits18, "BodyInductionSwitch": 1},
        D3072S_BITLESS_TSL + ("PowerSwitch", "ModelFunctionList"),
    )
    R.check("位 18 = 1 → 人体感应出现",
            "BodyInductionSwitch" in switch_keys(device2), True)


def test_declared_without_bit_helper():
    R.check("上报里没有 → False",
            capabilities.declared_without_bit(
                make_device({}, ()), "NightLightFunctionSwitch"), False)
    R.check("上报里有 → True",
            capabilities.declared_without_bit(
                make_device({"NightLightFunctionSwitch": 0}, ()),
                "NightLightFunctionSwitch"), True)


def main():
    test_d3072s_gets_no_night_light_entities()
    test_reporting_alone_does_not_resurrect_a_capped_function()
    test_best_position_switches_survive()
    test_no_report_means_no_creation()
    test_night_light_appears_when_the_bit_says_so()
    test_declared_without_bit_helper()
    return R.report("Bit-less airer functions")


if __name__ == "__main__":
    sys.exit(main())
