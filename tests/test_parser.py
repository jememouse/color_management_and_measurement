#!/usr/bin/env python3
"""ArgyllCMS 输出解析测试。

测试样例严格按照从二进制中提取的 printf 格式串构造::

     Result is XYZ: %f %f %f, %s Lab: %f %f %f
     Delta E to reference is %f %f %f (DE76 %f, CIE94 %f, DE2K %f)
    Spectrum from %.3f to %.3f nm in %d steps
    %cpatch %d of %d

这样测的是真实格式, 而不是我们想象中的格式。
"""

from __future__ import annotations

import pytest

from argyll.parser import ArgyllParser, parse_text, strip_ansi


def first_of(events: list[dict], kind: str) -> dict:
    for event in events:
        if event["type"] == kind:
            return event
    raise AssertionError(f"未解析出 {kind} 事件, 实得: {[e['type'] for e in events]}")


def types_of(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# --------------------------------------------------------------------------
# ANSI 清理
# --------------------------------------------------------------------------


def test_strip_ansi_removes_color_codes():
    assert strip_ansi("\x1b[1;31mError\x1b[0m") == "Error"


def test_strip_ansi_removes_osc():
    assert strip_ansi("\x1b]0;title\x07text") == "text"


def test_strip_ansi_keeps_plain_text():
    assert strip_ansi("Result is XYZ: 1 2 3") == "Result is XYZ: 1 2 3"


def test_ansi_does_not_break_number_parsing():
    """带颜色码的结果行仍须能解析出数值。"""
    line = "\x1b[32m Result is XYZ: 95.047 100.000 108.883, D50 Lab: 100.0 0.0 0.0\x1b[0m"
    event = first_of(ArgyllParser().feed(line), "reading")
    assert event["xyz"] == pytest.approx([95.047, 100.0, 108.883])


# --------------------------------------------------------------------------
# 五种 Result 变体
# --------------------------------------------------------------------------


def test_result_xyz_lab():
    line = " Result is XYZ: 95.047000 100.000000 108.883000, D50 Lab: 100.000000 0.000000 -0.000000"
    event = first_of(ArgyllParser().feed(line), "reading")

    assert event["xyz"] == pytest.approx([95.047, 100.0, 108.883])
    assert event["lab"] == pytest.approx([100.0, 0.0, -0.0])
    assert event["illuminant"] == "D50"
    assert event["partial"] is False
    assert event["hex"].startswith("#")


def test_result_uses_argyll_lab_over_local():
    """ArgyllCMS 的 Lab 是权威值, 必须覆盖本地计算结果。"""
    # 故意给一组与 XYZ 不自洽的 Lab, 看是否被原样保留
    line = " Result is XYZ: 20.0 30.0 15.0, D50 Lab: 11.111 22.222 33.333"
    event = first_of(ArgyllParser().feed(line), "reading")
    assert event["lab"] == pytest.approx([11.111, 22.222, 33.333])


def test_result_xyz_yxy():
    line = " Result is XYZ: 95.047 100.000 108.883, Yxy: 100.000 0.3127 0.3290"
    event = first_of(ArgyllParser().feed(line), "reading")
    assert event["yxy"] == pytest.approx([100.0, 0.3127, 0.3290])


def test_result_xyz_lch():
    line = " Result is XYZ: 20.0 30.0 15.0, LCh: 61.654 25.000 120.000"
    event = first_of(ArgyllParser().feed(line), "reading")
    assert event["lch"] == pytest.approx([61.654, 25.0, 120.0])


def test_result_xyz_yuv():
    line = " Result is XYZ: 95.047 100.000 108.883, Yuv: 100.000 0.1978 0.4683"
    event = first_of(ArgyllParser().feed(line), "reading")
    assert event["yuv"] == pytest.approx([100.0, 0.1978, 0.4683])


def test_result_y_only_is_marked_partial():
    """只有 Y 与 L* 时无法算派生色度量, 必须标记为 partial。"""
    event = first_of(ArgyllParser().feed(" Result is Y: 123.456, L*: 88.123"), "reading")
    assert event["partial"] is True
    assert event["y"] == pytest.approx(123.456)
    assert event["lstar"] == pytest.approx(88.123)
    assert "hex" not in event


def test_reading_index_increments():
    parser = ArgyllParser()
    line = " Result is XYZ: 20 30 15, D50 Lab: 61.6 -20 10"
    indices = [first_of(parser.feed(line), "reading")["index"] for _ in range(3)]
    assert indices == [1, 2, 3]


def test_reading_carries_preview_color():
    """界面要用 hex 画色块, 且需知道是否超出 sRGB 色域。"""
    line = " Result is XYZ: 96.422 100.0 82.521, D50 Lab: 100.0 0.0 0.0"
    event = first_of(ArgyllParser().feed(line), "reading")
    assert event["in_gamut"] is True
    # D50 纸白经色适应后应是中性白
    assert event["hex"] in ("#ffffff", "#fefefe", "#fffffe", "#feffff")


# --------------------------------------------------------------------------
# 色差
# --------------------------------------------------------------------------


def test_delta_e_line():
    line = (
        " Delta E to reference is 1.234000 -0.567000 0.890000 (DE76 1.599, CIE94 1.234, DE2K 1.100)"
    )
    event = first_of(ArgyllParser().feed(line), "delta_e")

    assert event["delta_lab"] == pytest.approx([1.234, -0.567, 0.890])
    assert event["de76"] == pytest.approx(1.599)
    assert event["de94"] == pytest.approx(1.234)
    assert event["de2000"] == pytest.approx(1.100)


# --------------------------------------------------------------------------
# 环境光
# --------------------------------------------------------------------------


def test_ambient_lux_only():
    event = first_of(ArgyllParser().feed(" Ambient = 350.5 Lux"), "ambient")
    assert event["lux"] == pytest.approx(350.5)
    assert "cct" not in event


def test_ambient_with_cct_and_duv():
    line = " Ambient = 350.5 Lux, CCT = 6503K (Duv 0.0032)"
    event = first_of(ArgyllParser().feed(line), "ambient")
    assert event["lux"] == pytest.approx(350.5)
    assert event["cct"] == pytest.approx(6503.0)
    assert event["duv"] == pytest.approx(0.0032)


def test_ambient_bad_cct_is_nulled():
    """(Bad CCT) 时不能把上一次的色温留在界面上。"""
    event = first_of(ArgyllParser().feed(" Ambient = 350.5 Lux, (Bad CCT)"), "ambient")
    assert event["cct"] is None
    assert "note" in event


# --------------------------------------------------------------------------
# 光谱 (跨行收集)
# --------------------------------------------------------------------------


def test_spectrum_collected_across_lines():
    parser = ArgyllParser()
    assert parser.feed("Spectrum from 380.000 to 730.000 nm in 8 steps") == []

    # 分三行给数据, 模拟真实的换行输出
    assert parser.feed(" 1.0 2.0 3.0") == []
    assert parser.feed(" 4.0 5.0") == []
    events = parser.feed(" 6.0 7.0 8.0")

    spectrum = first_of(events, "spectrum")
    assert spectrum["values"] == pytest.approx([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
    assert spectrum["steps"] == 8
    assert spectrum["start_nm"] == pytest.approx(380.0)
    assert spectrum["end_nm"] == pytest.approx(730.0)


def test_spectrum_wavelength_axis():
    """波长轴要能直接拿去画图, 首尾必须精确对齐。"""
    parser = ArgyllParser()
    parser.feed("Spectrum from 380.000 to 730.000 nm in 8 steps")
    events = parser.feed(" 1 2 3 4 5 6 7 8")

    spectrum = first_of(events, "spectrum")
    assert len(spectrum["wavelengths"]) == 8
    assert spectrum["wavelengths"][0] == pytest.approx(380.0)
    assert spectrum["wavelengths"][-1] == pytest.approx(730.0)
    assert spectrum["step_nm"] == pytest.approx(50.0)


def test_spectrum_single_step_does_not_divide_by_zero():
    parser = ArgyllParser()
    parser.feed("Spectrum from 550.000 to 550.000 nm in 1 steps")
    spectrum = first_of(parser.feed(" 42.0"), "spectrum")
    assert spectrum["step_nm"] == 0.0
    assert spectrum["values"] == [42.0]


def test_spectrum_interrupted_by_text_is_abandoned():
    """若中途插入非数字行, 应放弃收集而不是把后续数字胡乱拼进来。"""
    parser = ArgyllParser()
    parser.feed("Spectrum from 380.000 to 730.000 nm in 8 steps")
    parser.feed(" 1.0 2.0")
    parser.feed("Place instrument on spot to be measured,")

    # 新的数字行不应被当作上一段光谱的续集
    events = parser.feed(" 99.0 98.0")
    assert not any(e["type"] == "spectrum" for e in events)


def test_spectrum_links_to_reading_index():
    """光谱要能和它所属的那次读数对应起来。"""
    parser = ArgyllParser()
    parser.feed(" Result is XYZ: 20 30 15, D50 Lab: 61 -20 10")
    parser.feed("Spectrum from 380.000 to 730.000 nm in 2 steps")
    spectrum = first_of(parser.feed(" 1.0 2.0"), "spectrum")
    assert spectrum["reading_index"] == 1


# --------------------------------------------------------------------------
# 密度与 TM-30
# --------------------------------------------------------------------------


@pytest.mark.parametrize("standard", ["A", "M", "T", "E"])
def test_density_standards(standard):
    line = f"Status {standard} CMYV Density: 0.100000 0.200000 0.300000 0.400000"
    event = first_of(ArgyllParser().feed(line), "density")
    assert event["standard"] == standard
    assert event["cmyv"] == pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_tm30_line():
    line = " IES TM-30-15 Rf = 85.20 Rg = 98.70 CCT = 4000 Duv = 0.001500"
    event = first_of(ArgyllParser().feed(line), "tm30")
    assert event["rf"] == pytest.approx(85.2)
    assert event["rg"] == pytest.approx(98.7)
    assert event["cct"] == pytest.approx(4000.0)


# --------------------------------------------------------------------------
# 进度
# --------------------------------------------------------------------------


def test_patch_progress():
    """dispread 的格式是 "%cpatch %d of %d", %c 是 \\r, 会话层已拆成独立行。"""
    event = first_of(ArgyllParser().feed("patch 12 of 100"), "progress")
    assert event["current"] == 12
    assert event["total"] == 100
    assert event["fraction"] == pytest.approx(0.12)
    assert event["phase"] == "patch"


def test_patch_total_declaration():
    event = first_of(ArgyllParser().feed("Number of patches = 250"), "progress")
    assert event["total"] == 250
    assert event["current"] == 0


def test_dispcal_iteration_progress():
    line = "Doing iteration 3/8 with 40 sample points and repeat threshold of 0.5 DE"
    event = first_of(ArgyllParser().feed(line), "progress")
    assert event["phase"] == "iteration"
    assert (event["current"], event["total"]) == (3, 8)


def test_dispcal_verify_pass():
    event = first_of(ArgyllParser().feed("Doing verify pass 2/3 with 20 sample points"), "progress")
    assert event["phase"] == "verify"
    assert (event["current"], event["total"]) == (2, 3)


def test_calibration_target_metrics():
    parser = ArgyllParser()

    brightness = first_of(
        parser.feed("  Target Brightness = 120.000, Current = 118.500, error =  1.2%"),
        "calibration_target",
    )
    assert brightness["metric"] == "brightness"
    assert brightness["target"] == pytest.approx(120.0)

    white = first_of(parser.feed("White point error = 0.85 deltaE"), "calibration_target")
    assert white["metric"] == "white_point"
    assert white["delta_e"] == pytest.approx(0.85)

    black = first_of(parser.feed("Black level = 0.2500 cd/m^2"), "calibration_target")
    assert black["metric"] == "black_level"
    assert black["cd_m2"] == pytest.approx(0.25)


def test_profile_check_with_and_without_rms():
    parser = ArgyllParser()

    without = first_of(
        parser.feed("Profile check complete, peak err = 3.210000, avg err = 0.870000"),
        "profile_check",
    )
    assert without["peak_de"] == pytest.approx(3.21)
    assert "rms_de" not in without

    with_rms = first_of(
        parser.feed("Profile check complete, peak err = 3.21, avg err = 0.87, RMS = 1.05"),
        "profile_check",
    )
    assert with_rms["rms_de"] == pytest.approx(1.05)


# --------------------------------------------------------------------------
# 错误与提示
# --------------------------------------------------------------------------


def test_device_busy_error_gets_actionable_hint():
    """这是最常见的故障 —— 提示必须告诉用户该关掉什么, 而不是回显英文原文。"""
    line = "usb_open_port: open 'usb33: (X-Rite i1 Pro 2)' config 1 failed (0xe00002c5) (Device being used ?)"
    event = first_of(ArgyllParser().feed(line), "error")
    assert event["severity"] == "error"
    assert "占用" in event["message"]
    assert "i1Profiler" in event["message"]


def test_communication_failure():
    event = first_of(
        ArgyllParser().feed("Failed to initialise communications with instrument"), "error"
    )
    assert event["severity"] == "error"
    assert "通信" in event["message"]


def test_wrong_position_is_warning_not_error():
    """位置不对是可恢复的操作失误, 不该报成红色致命错误。"""
    line = "Spot read failed due to the sensor being in the wrong position"
    event = first_of(ArgyllParser().feed(line), "error")
    assert event["severity"] == "warning"


def test_needs_calibration_warning():
    event = first_of(
        ArgyllParser().feed("Spot read needs a calibration before continuing"), "error"
    )
    assert event["severity"] == "warning"
    assert "校准" in event["message"]


def test_error_keeps_raw_line():
    """翻译后的提示便于阅读, 但原文要保留以便排查。"""
    line = "Failed to initialise communications with instrument"
    event = first_of(ArgyllParser().feed(line), "error")
    assert event["raw"] == line


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("Place instrument on spot to be measured,", "measure"),
        ("Place instrument on its white calibration reference", "calibrate_white"),
        ("Hit ESC or Q to exit, instrument switch or any other key to take a reading:", "ready"),
    ],
)
def test_prompt_classification(line, kind):
    event = first_of(ArgyllParser().feed(line), "prompt")
    assert event["kind"] == kind


def test_calibration_complete():
    event = first_of(ArgyllParser().feed("Calibration complete"), "calibration")
    assert event["status"] == "complete"


# --------------------------------------------------------------------------
# 整段解析与状态管理
# --------------------------------------------------------------------------


SAMPLE_SESSION = """\
Place instrument on spot to be measured,
Hit ESC or Q to exit, instrument switch or any other key to take a reading:
 Result is XYZ: 88.123400 90.567800 75.432100, D50 Lab: 96.123400 -0.523400 2.345600
Spectrum from 380.000 to 730.000 nm in 8 steps
 0.812 0.834 0.851 0.867
 0.874 0.881 0.885 0.889
 Delta E to reference is 0.120000 -0.340000 0.560000 (DE76 0.670, CIE94 0.450, DE2K 0.410)
"""


def test_full_session_parse():
    events = parse_text(SAMPLE_SESSION)
    kinds = types_of(events)

    assert "prompt" in kinds
    assert "reading" in kinds
    assert "spectrum" in kinds
    assert "delta_e" in kinds

    reading = first_of(events, "reading")
    assert reading["xyz"] == pytest.approx([88.1234, 90.5678, 75.4321])

    spectrum = first_of(events, "spectrum")
    assert len(spectrum["values"]) == 8
    assert spectrum["reading_index"] == reading["index"]


def test_blank_lines_produce_nothing():
    parser = ArgyllParser()
    assert parser.feed("") == []
    assert parser.feed("   ") == []
    assert parser.feed("\t") == []


def test_reset_clears_state():
    parser = ArgyllParser()
    parser.feed(" Result is XYZ: 20 30 15, D50 Lab: 61 -20 10")
    parser.feed("Spectrum from 380.000 to 730.000 nm in 8 steps")
    parser.feed(" 1.0 2.0")

    parser.reset()

    # 重置后残留的半段光谱不应污染新会话
    assert parser.feed(" 3.0 4.0") == []
    event = first_of(parser.feed(" Result is XYZ: 20 30 15, D50 Lab: 61 -20 10"), "reading")
    assert event["index"] == 1


def test_unrecognized_lines_are_silent():
    """无法识别的行不该产出噪声事件 —— 原始输出已经通过 output 通道送达。"""
    parser = ArgyllParser()
    assert parser.feed("i1pro_getmisc: returning 634, 0x07d0") == []
    assert parser.feed("Some unknown diagnostic chatter") == []


# --------------------------------------------------------------------------
# 仪器档案与能力探测 (真机测试中发现的场景)
# --------------------------------------------------------------------------

INSTRUMENT_REPORT = """\
Connecting to the instrument ..
Instrument Type:   X-Rite i1 Pro 2
Serial Number:     1070504
Firmware version:  634
CPLD version:      999
Chip ID:           01-b476af1800004b
Date manufactured: 20-16-720
U.V. filter ?:     No
Measure Ambient ?: Yes
Tot. Measurement Count:           8599
Remission Spot Count:             1790
Remission Scan Count:             1891
Total lamp usage:                 7243.096680
"""


def test_instrument_report_is_parsed():
    events = [e for e in parse_text(INSTRUMENT_REPORT) if e["type"] == "instrument_info"]
    fields = {e["field"]: e["value"] for e in events}

    assert fields["model"] == "X-Rite i1 Pro 2"
    assert fields["serial"] == "1070504"
    assert fields["firmware"] == "634"
    assert fields["lamp_usage"] == "7243.096680"
    assert fields["total_measurements"] == "8599"


def test_instrument_fields_carry_chinese_labels():
    events = [e for e in parse_text(INSTRUMENT_REPORT) if e["type"] == "instrument_info"]
    labels = {e["field"]: e["label"] for e in events}
    assert labels["lamp_usage"] == "灯管累计点亮"
    assert labels["serial"] == "序列号"


def test_uv_filter_capability_is_flagged():
    """M1/M2 需要硬件 UV 滤镜 —— 界面据此禁用不可用的选项。"""
    events = [e for e in parse_text(INSTRUMENT_REPORT) if e["type"] == "instrument_info"]
    uv = next(e for e in events if e["field"] == "uv_filter")
    assert uv["supports_uv_filter"] is False

    supported = next(
        e for e in parse_text("U.V. filter ?:     Yes\n") if e["type"] == "instrument_info"
    )
    assert supported["supports_uv_filter"] is True


def test_unsupported_filter_error_is_actionable():
    """真机实测: 无 UV 滤镜的 i1Pro2 选 M1 会直接退出, 提示必须说清怎么办。"""
    event = first_of(
        ArgyllParser().feed("Setting requested filter not supported by instrument"), "error"
    )
    assert event["severity"] == "warning"
    assert "UV 滤镜" in event["message"]
    assert "不指定" in event["message"]


def test_instrument_lines_do_not_shadow_errors():
    """错误匹配必须优先于宽松的"字段: 值"匹配。"""
    event = first_of(
        ArgyllParser().feed("Failed to initialise communications with instrument"), "error"
    )
    assert event["type"] == "error"


def test_unknown_colon_lines_are_ignored():
    """只有白名单内的字段才产出事件, 避免把调试输出也当成仪器档案。"""
    parser = ArgyllParser()
    assert parser.feed("i1pro_getmisc:   returning 634, 0x07d0") == []
    assert parser.feed("Random Thing:    whatever") == []


# --------------------------------------------------------------------------
# 真实提示语 (原文取自二进制, 部分经真机确认)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        # 真机实测的那一条 —— 早期正则按 "white calibration reference" 写, 匹配不到
        ("Place the instrument on its reflective white reference S/N 1070504,", "calibrate_white"),
        ("Place instrument on white reference spot,", "calibrate_white"),
        ("Place the instrument on its transmissive white source,", "calibrate_transmissive"),
        ("Place the instrument on light trap, or in the dark,", "calibrate_black"),
        ("Place the instrument on black gloss reference", "calibrate_black"),
        (
            "Standard adapter should be fitted and instrument placed on calibration tile",
            "calibrate_tile",
        ),
        ("Doing Lamp Drift check - place instrument on calibration tile", "calibrate_lamp"),
        ("Place instrument on spot to be measured,", "measure"),
        ("Place the instrument on a 100% white test patch,", "measure_patch"),
        ("Place the instrument on a 80% white test patch,", "measure_patch"),
        ("Place the instrument back on the test window", "return_to_display"),
        (
            "Place the instrument so as to measure ambient upwards, beside the display,",
            "measure_ambient",
        ),
        ("Hit ESC or Q to exit, instrument switch or any other key to take a reading:", "ready"),
        (" and then hit any key to continue,", "confirm"),
        ("Hit Esc or Q to give up, any other key to retry:", "retry"),
    ],
)
def test_real_prompt_strings(line, kind):
    event = first_of(ArgyllParser().feed(line), "prompt")
    assert event["kind"] == kind, f"{line!r} 应识别为 {kind}, 实得 {event['kind']}"


def test_white_reference_out_of_tolerance():
    """白板超差是实际使用中很常见的一条 —— 通常是白板脏了。"""
    event = first_of(ArgyllParser().feed("White reference reading is out of tollerance"), "error")
    assert event["severity"] == "warning"
    assert "污渍" in event["message"]


def test_calibration_prompt_beats_generic_confirm():
    """具体的校准提示必须优先于通用的"按任意键"匹配。"""
    line = "Place the instrument on its reflective white reference S/N 1070504, and then hit any key to continue,"
    event = first_of(ArgyllParser().feed(line), "prompt")
    assert event["kind"] == "calibrate_white"


def test_ready_to_read_patch_yields_progress_not_prompt():
    """ "Ready to read patch 12 of 500" 同时含提示与进度语义。

    进度更有用 —— 它能直接驱动进度条, 而这一步 dispread 是自动读取的,
    用户无需动手。因此让它落到 progress 而非 prompt。
    """
    events = ArgyllParser().feed("Ready to read patch 12 of 500 RGB 100.0 50.0 25.0")
    progress = first_of(events, "progress")
    assert (progress["current"], progress["total"]) == (12, 500)
    assert not any(e["type"] == "prompt" for e in events)


# --------------------------------------------------------------------------
# 光强与灯管故障 (实际使用中遇到)
# --------------------------------------------------------------------------


def test_light_level_too_low_gives_ordered_checklist():
    """实际报错原文: Calibration failed with 'Measurement misread' (Light level is too low)

    这一行同时命中"光强不足"和"读数无效"两条规则。光强那条具体得多,
    必须优先 —— 否则用户只看到一句"读数无效", 完全不知道该动哪里。
    """
    line = "Calibration failed with 'Measurement misread' (Light level is too low)"
    event = first_of(ArgyllParser().feed(line), "error")

    assert event["severity"] == "warning"
    assert "光强不足" in event["message"]
    # 扩散帽排在首位 —— 它比"没扣紧"更隐蔽: 帽子装着时仪器看起来仍能正常放进底座
    assert "扩散帽" in event["message"]
    assert event["message"].index("扩散帽") < event["message"].index("校准底座")
    assert "污渍" in event["message"]


def test_light_level_too_high():
    event = first_of(ArgyllParser().feed("Light level is too high"), "error")
    assert "漏入" in event["message"]


def test_bare_misread_still_handled():
    """不带光强说明的 misread 也要有可操作提示。"""
    event = first_of(ArgyllParser().feed("Calibration failed with 'Measurement misread'"), "error")
    assert event["severity"] == "warning"
    assert "重试" in event["message"]


@pytest.mark.parametrize(
    ("line", "severity", "keyword"),
    [
        ("Lamp has failed", "error", "失效"),
        ("Lamp failure", "error", "失效"),
        ("Lamp is weak", "warning", "衰弱"),
        ("Lamp marginal", "warning", "衰弱"),
        ("Reflectance lamp error", "error", "异常"),
        ("Battery level too low to measure, Charge battery", "warning", "电量"),
        ("Black calibration values are too high", "warning", "杂散光"),
    ],
)
def test_hardware_fault_messages(line, severity, keyword):
    event = first_of(ArgyllParser().feed(line), "error")
    assert event["severity"] == severity
    assert keyword in event["message"]


def test_lamp_failure_is_error_not_warning():
    """灯管失效意味着数据不可用, 不能只给个黄色提示放过去。"""
    failed = first_of(ArgyllParser().feed("Lamp has failed"), "error")
    weak = first_of(ArgyllParser().feed("Lamp is weak"), "error")
    assert failed["severity"] == "error"
    assert weak["severity"] == "warning"


def test_retry_prompt_real_wording():
    """真实原文是 "Hit any key to retry", 早期模式按 "any other key to retry" 写, 匹配不到。"""
    event = first_of(ArgyllParser().feed("Hit any key to retry, or Esc or Q to abort:"), "prompt")
    assert event["kind"] == "retry"


# --------------------------------------------------------------------------
# 灯管时长: 单位与健康评估
# --------------------------------------------------------------------------


def test_lamp_usage_unit_is_seconds_not_hours():
    """**单位是秒**。

    7243.09 这个值乍看像"小时", 但结合累计测量次数一除就露馅:
    8599 次测量 / 7243 秒 = 每次点亮 0.84 秒, 正是 i1Pro 一次反射测量的
    积分时间量级; 若当成小时, 就成了每次测量点灯 50 分钟, 显然不成立。

    这条测试把这个换算钉死 —— 搞错单位会让用户误以为灯管接近报废。
    """
    from argyll.parser import assess_lamp

    result = assess_lamp(7243.096680)
    assert result["seconds"] == pytest.approx(7243.09668)
    assert result["hours"] == pytest.approx(2.012, abs=0.01)
    assert "小时" in result["display"]
    assert result["level"] == "good"


@pytest.mark.parametrize(
    ("seconds", "level"),
    [
        (60, "good"),  # 1 分钟
        (3600 * 10, "good"),  # 10 小时
        (3600 * 100, "good"),  # 100 小时
        (3600 * 300, "warning"),  # 300 小时
        (3600 * 800, "serious"),  # 800 小时
    ],
)
def test_lamp_health_thresholds(seconds, level):
    from argyll.parser import assess_lamp

    assert assess_lamp(seconds)["level"] == level


def test_lamp_assessment_admits_uncertainty():
    """X-Rite 未公开额定寿命 —— 界面上必须说明这只是经验参考, 不能冒充规格。"""
    from argyll.parser import assess_lamp

    assert "未公开" in assess_lamp(3600)["note"]


def test_lamp_duration_formatting():
    from argyll.parser import assess_lamp

    assert "秒" in assess_lamp(45)["display"]
    assert "分钟" in assess_lamp(600)["display"]
    assert "小时" in assess_lamp(7200)["display"]


def test_instrument_event_carries_lamp_assessment():
    event = first_of(
        ArgyllParser().feed("Total lamp usage:                 7243.096680"), "instrument_info"
    )
    assert "lamp" in event
    assert event["lamp"]["level"] == "good"


def test_instrument_fields_are_grouped():
    """界面按 group 分区展示, 每个字段都必须归好组。"""
    events = [e for e in parse_text(INSTRUMENT_REPORT) if e["type"] == "instrument_info"]
    groups = {e["group"] for e in events}
    assert groups <= {"identity", "capability", "usage", "calibration"}
    assert "identity" in groups
    assert "usage" in groups


def test_never_calibrated_is_flagged():
    """EEPROM 里的 1970 纪元起点表示从未校准过, 不能当成一个真实日期显示。"""
    line = "Date of last Remission spot cal:  Thu Jan  1 08:00:00 1970"
    event = first_of(ArgyllParser().feed(line), "instrument_info")
    assert event["never_calibrated"] is True
    assert event["group"] == "calibration"


def test_real_calibration_date_is_not_flagged():
    line = "Date of last Remission spot cal:  Mon Aug 12 14:30:00 2026"
    event = first_of(ArgyllParser().feed(line), "instrument_info")
    assert "never_calibrated" not in event
