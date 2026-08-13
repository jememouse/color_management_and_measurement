#!/usr/bin/env python3
"""ArgyllCMS 输出解析 —— 把文本行转成结构化事件。

## 正则从哪来

不是靠猜, 而是直接从可执行文件里提取的 printf 格式串::

    strings $(which spotread) | grep "Result is"
     Result is XYZ: %f %f %f, %s Lab: %f %f %f

这样得到的是权威格式, 覆盖了全部五种 ``Result is`` 变体、色差行、环境光行、
密度行。凭输出样例反推正则很容易漏掉少见分支(比如只在 -T 下出现的 TM-30 行)。

## 有状态的原因

光谱数据横跨多行: 先来一行 ``Spectrum from 380.000 to 730.000 nm in 36 steps``,
随后是若干行浮点数, 数量由头行的 steps 决定。因此解析器必须记住"正在收集光谱",
是个小状态机, 不能做成无状态的行函数。
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from argyll import colorimetry

# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------

#: 浮点数, 兼容科学计数法与 ArgyllCMS 偶尔输出的 nan/inf
_NUM = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?(?:nan|inf)"

#: CSI 与 OSC 两类 ANSI 转义序列。TERM 设成 xterm-256color 后,
#: ArgyllCMS 及其调用的库可能插入颜色码, 不清掉会污染数值解析。
_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def strip_ansi(text: str) -> str:
    """移除 ANSI 转义序列与退格。"""
    cleaned = _ANSI.sub("", text)
    # ArgyllCMS 有时用退格做原地覆盖
    return cleaned.replace("\b", "")


def _f(value: str) -> float:
    """把捕获到的字符串转成 float, 非法值返回 nan 而不是抛异常。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


# --------------------------------------------------------------------------
# 正则表 —— 每条都对应二进制里的一个 printf 格式串
# --------------------------------------------------------------------------

# " Result is XYZ: %f %f %f, %s Lab: %f %f %f"   (最常用)
RE_RESULT_LAB = re.compile(
    rf"Result is XYZ:\s*({_NUM})\s+({_NUM})\s+({_NUM}),\s*(\S+)\s+Lab:\s*({_NUM})\s+({_NUM})\s+({_NUM})"
)
# " Result is XYZ: %f %f %f, Yxy: %f %f %f"
RE_RESULT_YXY = re.compile(
    rf"Result is XYZ:\s*({_NUM})\s+({_NUM})\s+({_NUM}),\s*Yxy:\s*({_NUM})\s+({_NUM})\s+({_NUM})"
)
# " Result is XYZ: %f %f %f, LCh: %f %f %f"
RE_RESULT_LCH = re.compile(
    rf"Result is XYZ:\s*({_NUM})\s+({_NUM})\s+({_NUM}),\s*LCh:\s*({_NUM})\s+({_NUM})\s+({_NUM})"
)
# " Result is XYZ: %f %f %f, Yuv: %f %f %f"
RE_RESULT_YUV = re.compile(
    rf"Result is XYZ:\s*({_NUM})\s+({_NUM})\s+({_NUM}),\s*Yuv:\s*({_NUM})\s+({_NUM})\s+({_NUM})"
)
# " Result is Y: %f, L*: %f"   (只测亮度时)
RE_RESULT_Y = re.compile(rf"Result is Y:\s*({_NUM}),\s*L\*:\s*({_NUM})")

# " Delta E to reference is %f %f %f (DE76 %f, CIE94 %f, DE2K %f)"
RE_DELTA_E = re.compile(
    rf"Delta E to reference is\s*({_NUM})\s+({_NUM})\s+({_NUM})\s*"
    rf"\(DE76\s*({_NUM}),\s*CIE94\s*({_NUM}),\s*DE2K\s*({_NUM})\)"
)

# " Ambient = %.1f Lux%s"  /  ", CCT = %.0fK (Duv %.4f)"
RE_AMBIENT = re.compile(rf"Ambient\s*=\s*({_NUM})\s*Lux")
RE_CCT = re.compile(rf"CCT\s*=\s*({_NUM})\s*K?\s*\(Duv\s*({_NUM})\)")

# "Spectrum from %.3f to %.3f nm in %d steps"
RE_SPECTRUM_HEAD = re.compile(rf"Spectrum from\s*({_NUM})\s*to\s*({_NUM})\s*nm in\s*(\d+)\s*steps")

# "Status A CMYV Density: %f %f %f %f"
RE_DENSITY = re.compile(
    rf"Status\s+([AMTE])\s+CMYV Density:\s*({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})"
)

# " IES TM-30-15 Rf = %.2f Rg = %.2f CCT = %.0f Duv = %f"
RE_TM30 = re.compile(
    rf"IES TM-30-15\s+Rf\s*=\s*({_NUM})\s+Rg\s*=\s*({_NUM})\s+CCT\s*=\s*({_NUM})\s+Duv\s*=\s*({_NUM})"
)

# "%cpatch %d of %d"  —— %c 是 \r, 已被会话层拆成独立行
RE_PATCH = re.compile(r"\bpatch\s+(\d+)\s+of\s+(\d+)", re.IGNORECASE)
RE_PATCH_TOTAL = re.compile(r"Number of patches\s*=\s*(\d+)")

# "Doing iteration %d/%d ..."  /  "Doing verify pass %d/%d ..."
RE_ITERATION = re.compile(r"Doing\s+(iteration|verify pass)\s+(\d+)\s*/\s*(\d+)")

# "Profile check complete, peak err = %f, avg err = %f[, RMS = %f]"
RE_PROFILE_CHECK = re.compile(
    rf"Profile check complete,\s*peak err\s*=\s*({_NUM}),\s*avg err\s*=\s*({_NUM})"
    rf"(?:,\s*RMS\s*=\s*({_NUM}))?"
)

# dispcal 的目标/当前值对比行
RE_TARGET_BRIGHTNESS = re.compile(
    rf"Target Brightness\s*=\s*({_NUM}),\s*Current\s*=\s*({_NUM}),\s*error\s*=\s*({_NUM})%"
)
RE_WHITE_POINT_ERR = re.compile(rf"White point error\s*=\s*({_NUM})\s*deltaE")
RE_BLACK_LEVEL = re.compile(rf"Black level\s*=\s*({_NUM})\s*cd/m\^2")

#: 提示语 -> 语义类型。界面据此显示对应的中文操作指引。
#:
#: 这份表同样来自二进制里的原文, 而非凭印象拼写。真机测试时踩过一次:
#: 按"white calibration reference"写的正则匹配不到实际的
#: "Place the instrument on its **reflective** white reference S/N 1070504",
#: 结果用户在校准环节看不到任何中文指引。
#:
#: 顺序敏感 —— 具体模式必须排在通用模式前面。
PROMPT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ---- 校准 ----
    (re.compile(r"reflective white reference", re.I), "calibrate_white"),
    (re.compile(r"white reference spot", re.I), "calibrate_white"),
    (re.compile(r"white calibration (?:reference|tile)", re.I), "calibrate_white"),
    (re.compile(r"transmissive white (?:source|reference)", re.I), "calibrate_transmissive"),
    (re.compile(r"light trap|,\s*or in the dark", re.I), "calibrate_black"),
    (re.compile(r"black gloss reference", re.I), "calibrate_black"),
    (re.compile(r"Lamp Drift check", re.I), "calibrate_lamp"),
    (re.compile(r"(?:place instrument on|placed on) calibration tile", re.I), "calibrate_tile"),
    # ---- 测量 ----
    (re.compile(r"Place instrument on spot to be measured", re.I), "measure"),
    (re.compile(r"Place the instrument on a \d+%* white test patch", re.I), "measure_patch"),
    (re.compile(r"Place the instrument back on the test window", re.I), "return_to_display"),
    (re.compile(r"measure ambient upwards", re.I), "measure_ambient"),
    (re.compile(r"Hit ESC or Q to exit.*take a reading", re.I), "ready"),
    # ---- 通用确认 ----
    (re.compile(r"and then hit any key to continue", re.I), "confirm"),
    (re.compile(r"key to retry|to give up, any other key", re.I), "retry"),
    (re.compile(r"\(spacebar to continue\)", re.I), "confirm"),
)

#: 仪器自检信息 —— spotread -v 在连接成功后会打印一段设备档案。
#:
#: 分组用于界面上的分区展示: identity(身份) / capability(能力) /
#: usage(使用状况) / calibration(校准记录)。
INSTRUMENT_FIELDS: dict[str, tuple[str, str, str]] = {
    # 身份
    "Instrument Type": ("model", "型号", "identity"),
    "Serial Number": ("serial", "序列号", "identity"),
    "Firmware version": ("firmware", "固件版本", "identity"),
    "CPLD version": ("cpld", "CPLD 版本", "identity"),
    "Chip ID": ("chip_id", "芯片 ID", "identity"),
    "Date manufactured": ("manufactured", "生产日期", "identity"),
    # 能力
    "U.V. filter ?": ("uv_filter", "UV 滤镜", "capability"),
    "Measure Ambient ?": ("ambient_capable", "环境光测量", "capability"),
    # 使用状况
    "Tot. Measurement Count": ("total_measurements", "累计测量次数", "usage"),
    "Remission Spot Count": ("reflective_spots", "反射点测", "usage"),
    "Remission Scan Count": ("reflective_scans", "反射扫描", "usage"),
    "Emission Spot Count": ("emissive_spots", "发光点测", "usage"),
    "Total lamp usage": ("lamp_usage", "灯管累计点亮", "usage"),
    # 校准记录
    "Date of last Remission spot cal": ("last_reflective_cal", "上次反射校准", "calibration"),
    "Remission Spot Count at last cal": ("spots_at_last_cal", "上次校准时测量数", "calibration"),
    "Date of last Emission spot cal": ("last_emissive_cal", "上次发光校准", "calibration"),
    "Date of last Transmission spot cal": ("last_transmissive_cal", "上次透射校准", "calibration"),
}


def _format_duration(seconds: float) -> str:
    """把秒数格式化成人能读的时长。"""
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds / 3600:.2f} 小时"


def assess_lamp(seconds: float) -> dict[str, Any]:
    """按灯管累计点亮时长给出健康参考。

    **单位是秒, 不是小时**。这一点极易搞错: 7243 这个数字乍看像"小时",
    但结合累计测量次数一除就露馅了 —— 8599 次测量对应 7243 秒, 即每次点亮
    0.84 秒, 正是 i1Pro 一次反射测量的积分时间量级; 若当成小时, 就成了每次
    测量点灯 50 分钟, 显然荒谬。

    需要说明的是: **X-Rite 未公开 i1Pro 灯管的额定寿命**, 下面的分档只是
    按卤钨灯的一般经验给出的参考, 不是厂商规格。真正权威的判断来自仪器
    自己报告的 "Lamp is weak" / "Lamp has failed", 那两条会以告警形式
    单独出现。
    """
    hours = seconds / 3600.0

    if hours < 50:
        level, text = "good", "良好"
    elif hours < 200:
        level, text = "good", "正常"
    elif hours < 500:
        level, text = "warning", "使用较多, 建议关注短波长测量一致性"
    else:
        level, text = "serious", "使用量很大, 建议送检或考虑更换灯管"

    return {
        "seconds": seconds,
        "hours": hours,
        "display": _format_duration(seconds),
        "level": level,
        "text": text,
        "note": "X-Rite 未公开额定寿命, 此分档仅为卤钨灯一般经验参考",
    }


RE_INSTRUMENT_FIELD = re.compile(r"^\s*([A-Za-z][A-Za-z.\s]*[A-Za-z?])\s*:\s{2,}(.+?)\s*$")

#: 错误与告警。severity 决定界面是红色横幅还是黄色提示。
ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"Setting requested filter not supported|filter not supported by instrument", re.I
        ),
        "warning",
        "该仪器不支持所选测量条件 —— M1/M2 需要仪器配备 UV 滤镜硬件, "
        "请把测量条件改为「不指定」后重试",
    ),
    (
        re.compile(r"Setting calibration standard not supported", re.I),
        "warning",
        "该仪器不支持所选的校准标准, 请使用默认设置",
    ),
    (
        re.compile(r"doesn't support it|doesn't have .* capability", re.I),
        "warning",
        "仪器不具备该功能, 相关设置已被忽略",
    ),
    (
        re.compile(r"Device being used|kIOReturnExclusiveAccess|0xe00002c5", re.I),
        "error",
        "设备被其他程序占用 —— 请退出 i1Profiler 或其他正在运行的测量程序",
    ),
    (
        re.compile(r"Failed to initialise communications", re.I),
        "error",
        "无法与仪器建立通信 —— 检查 USB 连接, 或设备是否被占用",
    ),
    (
        re.compile(r"Spot read failed due to the sensor being in the wrong position", re.I),
        "warning",
        "测量失败: 仪器位置不对 —— 反射测量需贴紧样品, 校准需扣在底座上",
    ),
    (re.compile(r"Spot read failed due to misread", re.I), "warning", "测量失败: 读数无效, 请重测"),
    (
        re.compile(r"needs a calibration before continuing", re.I),
        "warning",
        "需要先校准 —— 把仪器扣在白色校准底座上",
    ),
    # ---- 光强类: 实际使用中最常撞到的一组 ----
    #
    # 完整错误形如:
    #   Calibration failed with 'Measurement misread' (Light level is too low)
    # "Light level" 比 "misread" 具体得多, 必须排在前面, 否则用户只会看到
    # 一句无从下手的"读数无效"。
    (
        re.compile(r"Light level is too low|peak magnitude too low", re.I),
        "warning",
        "校准光强不足 —— 按顺序检查: "
        "①测量口上是否套着环境光扩散帽/适配器(反射测量必须取下, 这是最易被忽略的一条) "
        "②仪器是否完全扣进校准底座并推到卡住 "
        "③白板有无污渍指纹(用吹气球或无绒布轻擦) "
        "④测量口是否正对白板; "
        "仍未解决可运行 diagnose_light.py 读取传感器原始光强",
    ),
    (
        re.compile(r"Light level is too high", re.I),
        "warning",
        "校准光强过高 —— 通常是有外部光线漏入, 请确认仪器已完全扣入底座并避开强光直射",
    ),
    (
        re.compile(r"Black calibration values are too high", re.I),
        "warning",
        "暗电流校准值偏高 —— 有杂散光漏入, 请确认仪器已扣紧且处于遮光状态",
    ),
    (
        re.compile(r"Wavelength calibration reading is too low", re.I),
        "warning",
        "波长校准读数偏低 —— 光路可能被遮挡, 或灯管输出不足",
    ),
    # ---- 灯管健康 ----
    #
    # 钨丝灯老化会让蓝端能量先衰减, 表现为短波长测量不准。
    # 这几条是仪器自己给出的判断, 比累计使用时长更有参考价值。
    (
        re.compile(r"Lamp has failed|Lamp failure|Lamp failed during reading", re.I),
        "error",
        "仪器灯管已失效 —— 需要送修更换, 此状态下的测量结果不可用",
    ),
    (
        re.compile(r"Lamp is weak|Lamp marginal", re.I),
        "warning",
        "仪器判定灯管已衰弱 —— 短波长(蓝端)测量精度会下降, 建议安排送检或更换",
    ),
    (
        re.compile(r"Reflectance lamp error|Transmission lamp error", re.I),
        "error",
        "灯管工作异常 —— 请重新插拔 USB 后重试, 若持续出现需送修",
    ),
    # ---- 电量 ----
    (
        re.compile(r"Battery (?:level )?too low", re.I),
        "warning",
        "仪器电量不足 —— 请连接 USB 充电后再测量",
    ),
    (
        re.compile(
            r"White reference (?:reading )?is out of toll?erance|Checking white reference failed",
            re.I,
        ),
        "warning",
        "白板校准读数超出容差 —— 校准白板可能有污渍或划痕, 也可能是仪器未完全扣紧底座",
    ),
    (
        re.compile(r"Calibration failed with|Measurement misread", re.I),
        "warning",
        "校准读数无效 —— 请确认仪器已扣紧校准底座且保持静止, 然后按空格重试",
    ),
    (
        re.compile(r"Transmission white reference is (?:out of range|too low)", re.I),
        "warning",
        "透射白参考超出范围 —— 检查透射光源亮度与仪器位置",
    ),
    (
        re.compile(r"scan white reference is not bright enough", re.I),
        "warning",
        "扫描白参考亮度不足 —— 检查校准底座是否洁净",
    ),
    (re.compile(r"No such file or directory|not found", re.I), "error", "文件或工具不存在"),
    (re.compile(r"Got abort or error from calibration", re.I), "error", "校准被中止或失败"),
    (re.compile(r"^\s*Error\s*[-:]", re.I), "error", ""),
    (re.compile(r"^\s*Warning\s*[-:]", re.I), "warning", ""),
)


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------


class ArgyllParser:
    """有状态的逐行解析器。

    用法::

        parser = ArgyllParser()
        for line in output_lines:
            for event in parser.feed(line):
                handle(event)

    每个 ``feed`` 返回该行产生的事件列表(通常 0 或 1 个, 光谱收满时可能带上
    一个 spectrum 事件)。事件是纯 dict, 可直接 json.dumps 送给前端。
    """

    def __init__(self, *, illuminant: str = "D50") -> None:
        self.illuminant = illuminant

        # 光谱收集状态
        self._spec_start: float = 0.0
        self._spec_end: float = 0.0
        self._spec_steps: int = 0
        self._spec_values: list[float] = []
        self._collecting_spectrum = False

        # 最近一次 patch 总数, 用于补全只报当前序号的进度行
        self._patch_total: int | None = None

        # 最近一次读数, 供 spectrum/密度事件关联
        self._reading_index = 0

    # ---------------- 主入口 ----------------

    def feed(self, raw_line: str) -> list[dict[str, Any]]:
        """喂入一行, 返回解析出的事件。"""
        line = strip_ansi(raw_line).rstrip()
        if not line.strip():
            return []

        events: list[dict[str, Any]] = []

        # 光谱数据行优先: 处于收集态时, 整行都应是数字
        if self._collecting_spectrum:
            spectrum_event = self._collect_spectrum(line)
            if spectrum_event is not None:
                events.append(spectrum_event)
            # 收集态下若该行确实是数字, 就不再做其他匹配
            if self._collecting_spectrum or spectrum_event is not None:
                return events

        for handler in (
            self._match_result,
            self._match_delta_e,
            self._match_ambient,
            self._match_spectrum_head,
            self._match_density,
            self._match_tm30,
            self._match_progress,
            self._match_dispcal,
            self._match_profile_check,
            self._match_error,
            self._match_instrument_info,
            self._match_prompt,
        ):
            event = handler(line)
            if event is not None:
                events.append(event)
                break

        return events

    def reset(self) -> None:
        """开始新会话时清空状态。"""
        self._collecting_spectrum = False
        self._spec_values = []
        self._patch_total = None
        self._reading_index = 0

    # ---------------- 测量结果 ----------------

    def _match_result(self, line: str) -> dict[str, Any] | None:
        if "Result is" not in line:
            return None

        if m := RE_RESULT_LAB.search(line):
            xyz = (_f(m.group(1)), _f(m.group(2)), _f(m.group(3)))
            illuminant = m.group(4).upper()
            lab = (_f(m.group(5)), _f(m.group(6)), _f(m.group(7)))
            return self._build_reading(xyz, lab=lab, illuminant=illuminant)

        if m := RE_RESULT_YXY.search(line):
            xyz = (_f(m.group(1)), _f(m.group(2)), _f(m.group(3)))
            return self._build_reading(xyz, extra={"yxy": [_f(m.group(i)) for i in (4, 5, 6)]})

        if m := RE_RESULT_LCH.search(line):
            xyz = (_f(m.group(1)), _f(m.group(2)), _f(m.group(3)))
            return self._build_reading(xyz, extra={"lch": [_f(m.group(i)) for i in (4, 5, 6)]})

        if m := RE_RESULT_YUV.search(line):
            xyz = (_f(m.group(1)), _f(m.group(2)), _f(m.group(3)))
            return self._build_reading(xyz, extra={"yuv": [_f(m.group(i)) for i in (4, 5, 6)]})

        if m := RE_RESULT_Y.search(line):
            # 只有亮度, 没有完整 XYZ —— 不能算派生色度量
            self._reading_index += 1
            return {
                "type": "reading",
                "index": self._reading_index,
                "partial": True,
                "y": _f(m.group(1)),
                "lstar": _f(m.group(2)),
                "raw": line.strip(),
            }

        return None

    def _build_reading(
        self,
        xyz: tuple[float, float, float],
        *,
        lab: tuple[float, float, float] | None = None,
        illuminant: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """组装 reading 事件, 并补上界面需要的派生量。

        ArgyllCMS 给的 Lab 是权威值(它知道自己用的观察者与光源), 优先采用;
        描述性派生量(sRGB 预览色、CCT、Duv、色域内外)由本地计算补齐。
        """
        self._reading_index += 1
        illum = (illuminant or self.illuminant).upper()
        derived = colorimetry.describe(xyz, illum)

        # 用 ArgyllCMS 自己算的 Lab 覆盖本地值 —— 两者理应一致,
        # 不一致时以仪器软件为准, 因为它掌握完整的光谱与观察者数据。
        if lab is not None:
            derived["lab"] = list(lab)

        event: dict[str, Any] = {
            "type": "reading",
            "index": self._reading_index,
            "partial": False,
            **derived,
        }
        if extra:
            event.update(extra)
        return event

    # ---------------- 色差 ----------------

    def _match_delta_e(self, line: str) -> dict[str, Any] | None:
        m = RE_DELTA_E.search(line)
        if not m:
            return None
        return {
            "type": "delta_e",
            "delta_lab": [_f(m.group(i)) for i in (1, 2, 3)],
            "de76": _f(m.group(4)),
            "de94": _f(m.group(5)),
            "de2000": _f(m.group(6)),
        }

    # ---------------- 环境光 ----------------

    def _match_ambient(self, line: str) -> dict[str, Any] | None:
        m_amb = RE_AMBIENT.search(line)
        m_cct = RE_CCT.search(line)
        if not m_amb and not m_cct:
            return None

        event: dict[str, Any] = {"type": "ambient"}
        if m_amb:
            event["lux"] = _f(m_amb.group(1))
        if m_cct:
            event["cct"] = _f(m_cct.group(1))
            event["duv"] = _f(m_cct.group(2))
        if "Bad CCT" in line:
            event["cct"] = None
            event["note"] = "色度点远离黑体轨迹, 色温不适用"
        return event

    # ---------------- 光谱 ----------------

    def _match_spectrum_head(self, line: str) -> dict[str, Any] | None:
        m = RE_SPECTRUM_HEAD.search(line)
        if not m:
            return None
        self._spec_start = _f(m.group(1))
        self._spec_end = _f(m.group(2))
        self._spec_steps = int(m.group(3))
        self._spec_values = []
        self._collecting_spectrum = self._spec_steps > 0
        return None  # 头行本身不产生事件, 等数据收齐

    def _collect_spectrum(self, line: str) -> dict[str, Any] | None:
        """收集光谱数值行。收满 steps 个后产出 spectrum 事件。"""
        tokens = re.findall(_NUM, line)
        if not tokens:
            # 不是数字行 —— 光谱被打断(可能被提示语插入), 放弃收集
            self._collecting_spectrum = False
            return None

        self._spec_values.extend(_f(t) for t in tokens)
        if len(self._spec_values) < self._spec_steps:
            return None

        values = self._spec_values[: self._spec_steps]
        self._collecting_spectrum = False
        self._spec_values = []

        step_nm = (
            (self._spec_end - self._spec_start) / (self._spec_steps - 1)
            if self._spec_steps > 1
            else 0.0
        )
        return {
            "type": "spectrum",
            "reading_index": self._reading_index,
            "start_nm": self._spec_start,
            "end_nm": self._spec_end,
            "steps": self._spec_steps,
            "step_nm": step_nm,
            "wavelengths": [self._spec_start + i * step_nm for i in range(self._spec_steps)],
            "values": values,
        }

    # ---------------- 密度与 TM-30 ----------------

    def _match_density(self, line: str) -> dict[str, Any] | None:
        m = RE_DENSITY.search(line)
        if not m:
            return None
        return {
            "type": "density",
            "standard": m.group(1),
            "cmyv": [_f(m.group(i)) for i in (2, 3, 4, 5)],
        }

    def _match_tm30(self, line: str) -> dict[str, Any] | None:
        m = RE_TM30.search(line)
        if not m:
            return None
        return {
            "type": "tm30",
            "rf": _f(m.group(1)),
            "rg": _f(m.group(2)),
            "cct": _f(m.group(3)),
            "duv": _f(m.group(4)),
        }

    # ---------------- 进度 ----------------

    def _match_progress(self, line: str) -> dict[str, Any] | None:
        if m := RE_PATCH_TOTAL.search(line):
            self._patch_total = int(m.group(1))
            return {"type": "progress", "phase": "patch", "current": 0, "total": self._patch_total}

        if m := RE_PATCH.search(line):
            current, total = int(m.group(1)), int(m.group(2))
            self._patch_total = total
            return {
                "type": "progress",
                "phase": "patch",
                "current": current,
                "total": total,
                "fraction": current / total if total else 0.0,
            }
        return None

    def _match_dispcal(self, line: str) -> dict[str, Any] | None:
        if m := RE_ITERATION.search(line):
            kind = "verify" if "verify" in m.group(1).lower() else "iteration"
            current, total = int(m.group(2)), int(m.group(3))
            return {
                "type": "progress",
                "phase": kind,
                "current": current,
                "total": total,
                "fraction": current / total if total else 0.0,
            }

        if m := RE_TARGET_BRIGHTNESS.search(line):
            return {
                "type": "calibration_target",
                "metric": "brightness",
                "target": _f(m.group(1)),
                "current": _f(m.group(2)),
                "error_pct": _f(m.group(3)),
            }

        if m := RE_WHITE_POINT_ERR.search(line):
            return {
                "type": "calibration_target",
                "metric": "white_point",
                "delta_e": _f(m.group(1)),
            }

        if m := RE_BLACK_LEVEL.search(line):
            return {"type": "calibration_target", "metric": "black_level", "cd_m2": _f(m.group(1))}

        return None

    def _match_profile_check(self, line: str) -> dict[str, Any] | None:
        m = RE_PROFILE_CHECK.search(line)
        if not m:
            return None
        event = {"type": "profile_check", "peak_de": _f(m.group(1)), "avg_de": _f(m.group(2))}
        if m.group(3):
            event["rms_de"] = _f(m.group(3))
        return event

    # ---------------- 错误与提示 ----------------

    def _match_error(self, line: str) -> dict[str, Any] | None:
        for pattern, severity, hint in ERROR_PATTERNS:
            if pattern.search(line):
                return {
                    "type": "error",
                    "severity": severity,
                    "message": hint or line.strip(),
                    "raw": line.strip(),
                }
        return None

    def _match_instrument_info(self, line: str) -> dict[str, Any] | None:
        """解析仪器自检档案(spotread -v 连接成功后打印)。

        必须放在错误匹配之后: 这里的正则相对宽松, 先让明确的错误模式挑走,
        剩下的才按"字段: 值"处理。
        """
        m = RE_INSTRUMENT_FIELD.match(line)
        if not m:
            return None

        key = m.group(1).strip()
        mapping = INSTRUMENT_FIELDS.get(key)
        if mapping is None:
            return None

        field, label, group = mapping
        value = m.group(2).strip()

        event: dict[str, Any] = {
            "type": "instrument_info",
            "field": field,
            "label": label,
            "group": group,
            "value": value,
        }

        # UV 滤镜能力直接决定 M1/M2 可用与否, 单独标出来给界面用
        if field == "uv_filter":
            event["supports_uv_filter"] = value.lower().startswith("y")

        # 灯管时长附上换算与健康评估, 免得前端再实现一遍单位判断
        if field == "lamp_usage":
            with contextlib.suppress(ValueError):
                event["lamp"] = assess_lamp(float(value))

        # 从未校准过的设备, EEPROM 里是 Unix 纪元起点
        if field.startswith("last_") and "1970" in value:
            event["never_calibrated"] = True

        return event

    def _match_prompt(self, line: str) -> dict[str, Any] | None:
        for pattern, kind in PROMPT_PATTERNS:  # type: ignore[misc]
            if pattern.search(line):
                return {"type": "prompt", "kind": kind, "text": line.strip()}

        if re.search(r"Calibration complete", line, re.I):
            return {"type": "calibration", "status": "complete", "text": line.strip()}
        return None


def parse_text(text: str, *, illuminant: str = "D50") -> list[dict[str, Any]]:
    """一次性解析整段输出, 便于测试与离线分析。"""
    parser = ArgyllParser(illuminant=illuminant)
    events: list[dict[str, Any]] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        events.extend(parser.feed(line))
    return events
