#!/usr/bin/env python3
"""工具链封装测试。

分两类:
    - 纯单元测试: 用真实抓取的 usage 文本做 fixture, 不依赖硬件
    - 集成测试: 实际调用 ArgyllCMS 枚举设备, 标记为 integration
"""

from __future__ import annotations

import pytest

import config
from argyll import tools
from argyll.tools import (
    Command,
    ToolError,
    build,
    build_colprof,
    build_dispcal,
    build_dispread,
    build_spotread,
    build_targen,
    resolve_work_path,
    safe_work_name,
)

# --------------------------------------------------------------------------
# 枚举解析 (fixture 取自真实的 -? 输出)
# --------------------------------------------------------------------------

SPOTREAD_USAGE = """\
Measure spot values, Version 3.5.0
usage: spotread [-options] [logfile]
 -v                   Verbose mode
 -c listno            Set instrument port from the following list (default 1)
    1 = 'usb33: (X-Rite i1 Pro 2)'
    2 = '/dev/cu.Bluetooth-Incoming-Port'
 -t                   Use transmission measurement mode
 -e                   Use emissive measurement mode
"""

DISPWIN_USAGE = """\
Test display patch window, Set Video LUTs, Install profiles, Version 3.5.0
usage: dispwin [options] [calfile]
 -v                   Verbose mode
 -d n                 Choose the display from the following list (default 1)
    1 = 'Built-in Retina Display, at 0, 0, width 1728, height 1117 (Primary Display)'
    2 = 'U32V11N, at 852, -1692, width 3008, height 1692'
    3 = 'Mi Monitor, at -2588, -1440, width 3440, height 1440'
 -dweb[:port]         Display via web server at port (default 8080)
 -P ho,vo,ss[,vs]     Position test window and scale it
"""


def test_parse_instruments(monkeypatch):
    monkeypatch.setattr(tools, "_usage_text", lambda tool: SPOTREAD_USAGE)
    instruments = tools.list_instruments()

    assert len(instruments) == 2
    assert instruments[0].index == 1
    assert instruments[0].model == "X-Rite i1 Pro 2"
    assert instruments[0].is_measuring_device is True


def test_serial_ports_are_not_measuring_devices(monkeypatch):
    """蓝牙/串口也会混在 -c 列表里, 界面不该把它们当成可选仪器。"""
    monkeypatch.setattr(tools, "_usage_text", lambda tool: SPOTREAD_USAGE)
    bluetooth = tools.list_instruments()[1]

    assert bluetooth.index == 2
    assert bluetooth.is_measuring_device is False
    assert bluetooth.model is None


def test_parse_displays_with_geometry(monkeypatch):
    monkeypatch.setattr(tools, "_usage_text", lambda tool: DISPWIN_USAGE)
    displays = tools.list_displays()

    assert len(displays) == 3

    builtin = displays[0]
    assert builtin.name == "Built-in Retina Display"
    assert (builtin.x, builtin.y) == (0, 0)
    assert (builtin.width, builtin.height) == (1728, 1117)
    assert builtin.is_primary is True


def test_parse_displays_negative_coordinates(monkeypatch):
    """副屏常有负坐标(排在主屏左侧/上方), 不能解析成正数或失败。"""
    monkeypatch.setattr(tools, "_usage_text", lambda tool: DISPWIN_USAGE)
    mi = tools.list_displays()[2]

    assert mi.name == "Mi Monitor"
    assert mi.x == -2588
    assert mi.y == -1440
    assert mi.is_primary is False


def test_list_parsing_stops_at_next_option(monkeypatch):
    """列表后面紧跟的其他选项行不能被吞进列表。"""
    monkeypatch.setattr(tools, "_usage_text", lambda tool: DISPWIN_USAGE)
    assert len(tools.list_displays()) == 3  # 不含 -dweb / -P 行


def test_empty_usage_yields_empty_list(monkeypatch):
    """ArgyllCMS 缺失时不能抛异常, 返回空列表让界面显示"未检测到设备"。"""
    monkeypatch.setattr(tools, "_usage_text", lambda tool: "")
    assert tools.list_instruments() == []
    assert tools.list_displays() == []


# --------------------------------------------------------------------------
# 安全: 文件名校验
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../../../etc/passwd",
        "../escape",
        "sub/dir",
        "back\\slash",
        "..",
        "a/../../b",
    ],
)
def test_path_traversal_is_rejected(name):
    """路径穿越必须被挡下 —— 否则 colprof 会往工作目录外写文件。"""
    with pytest.raises(ToolError):
        safe_work_name(name)


@pytest.mark.parametrize("name", ["-I", "-rf", "--help", "-o"])
def test_option_like_names_are_rejected(name):
    """以 - 开头的"文件名"会被 ArgyllCMS 当成选项解析。"""
    with pytest.raises(ToolError, match="不能以 - 开头"):
        safe_work_name(name)


@pytest.mark.parametrize("name", ["", "   ", "with space", "semi;colon", "pipe|x", "dollar$"])
def test_invalid_characters_are_rejected(name):
    with pytest.raises(ToolError):
        safe_work_name(name)


@pytest.mark.parametrize("name", ["display1", "my-profile", "test_2026.01", "A", "x" * 64])
def test_valid_names_are_accepted(name):
    assert safe_work_name(name) == name


def test_name_length_limit():
    with pytest.raises(ToolError):
        safe_work_name("x" * 65)


def test_resolve_work_path_stays_inside_work_dir():
    path = resolve_work_path("myprofile", suffix=".icc")
    assert path.is_relative_to(config.WORK_DIR.resolve())
    assert path.name == "myprofile.icc"


def test_resolve_work_path_rejects_traversal():
    with pytest.raises(ToolError):
        resolve_work_path("../outside", suffix=".icc")


# --------------------------------------------------------------------------
# spotread 命令构建
# --------------------------------------------------------------------------


def test_spotread_reflective_defaults():
    cmd = build_spotread()
    assert cmd.tool == "spotread"
    assert cmd.argv[0].endswith("spotread")
    assert "-c" in cmd.argv and "1" in cmd.argv
    assert "-s" in cmd.argv  # 默认输出光谱, 界面要画曲线
    # 反射是默认模式, 不该出现 -e / -a / -t
    assert not {"-e", "-a", "-t"} & set(cmd.argv)


def test_spotread_emissive_gets_display_type():
    """发光模式必须带 -y, 否则 ArgyllCMS 不知道按什么显示技术积分。"""
    cmd = build_spotread(mode="emissive")
    assert "-e" in cmd.argv
    assert "-y" in cmd.argv
    assert cmd.argv[cmd.argv.index("-y") + 1] == "l"


def test_spotread_filter_modes():
    assert "-F" in build_spotread(filter_mode="M1").argv
    # M1 -> 5 (D50 含 UV), ISO 13655 推荐
    cmd = build_spotread(filter_mode="M1")
    assert cmd.argv[cmd.argv.index("-F") + 1] == "5"
    # M2 -> u (UV Cut)
    cmd2 = build_spotread(filter_mode="M2")
    assert cmd2.argv[cmd2.argv.index("-F") + 1] == "u"


def test_spotread_filter_rejected_in_emissive_mode():
    """滤镜对自发光体没有物理意义, 应给出明确错误而不是静默忽略。"""
    with pytest.raises(ToolError, match="滤镜"):
        build_spotread(mode="emissive", filter_mode="M1")


def test_spotread_high_res():
    assert "-H" in build_spotread(high_res=True).argv


def test_spotread_illuminant_and_observer():
    cmd = build_spotread(illuminant="D65", observer="1964_10")
    assert cmd.argv[cmd.argv.index("-i") + 1] == "D65"
    assert cmd.argv[cmd.argv.index("-Q") + 1] == "1964_10"


def test_spotread_rejects_unknown_illuminant():
    with pytest.raises(ToolError, match="光源"):
        build_spotread(illuminant="D99")


def test_spotread_rejects_bad_instrument_index():
    with pytest.raises(ToolError, match="仪器序号"):
        build_spotread(instrument=0)
    with pytest.raises(ToolError, match="仪器序号"):
        build_spotread(instrument=999)


def test_spotread_rejects_bool_as_index():
    """True 在 Python 里是 int 的子类 —— 必须显式挡掉, 否则会变成 -c 1。"""
    with pytest.raises(ToolError):
        build_spotread(instrument=True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# dispcal / targen / colprof
# --------------------------------------------------------------------------


def test_dispcal_basic():
    cmd = build_dispcal(name="mydisplay", display=2, white_point=6500, gamma=2.2)
    assert cmd.tool == "dispcal"
    assert cmd.argv[cmd.argv.index("-d") + 1] == "2"
    assert "-t6500" in cmd.argv
    assert cmd.argv[-1] == "mydisplay"
    assert cmd.produces == ["mydisplay.cal"]


def test_dispcal_native_white_point():
    """white_point=None 表示沿用显示器原生白点, 不应出现 -t。"""
    cmd = build_dispcal(name="native", white_point=None)
    assert not any(a.startswith("-t") for a in cmd.argv)


def test_dispcal_rejects_absurd_temperature():
    with pytest.raises(ToolError, match="目标色温"):
        build_dispcal(name="x", white_point=99999)


def test_dispcal_rejects_absurd_gamma():
    with pytest.raises(ToolError, match="gamma"):
        build_dispcal(name="x", gamma=99)


def test_dispcal_interactive_flag():
    assert "-m" in build_dispcal(name="x", interactive_adjust=False).argv
    assert "-m" not in build_dispcal(name="x", interactive_adjust=True).argv


def test_targen_patch_count():
    cmd = build_targen(name="chart", patches=800)
    assert cmd.argv[cmd.argv.index("-f") + 1] == "800"
    assert "-d3" in cmd.argv  # RGB 显示器
    assert cmd.produces == ["chart.ti1"]


def test_targen_rejects_absurd_patch_count():
    with pytest.raises(ToolError, match="色块数"):
        build_targen(name="x", patches=1)
    with pytest.raises(ToolError, match="色块数"):
        build_targen(name="x", patches=99999)


def test_colprof_algorithm_mapping():
    """显示器用 shaper+matrix, 打印机必须用 LUT —— 映射不能搞反。"""
    assert "-aS" in build_colprof(name="x", algorithm="shaper_matrix").argv
    assert "-aX" in build_colprof(name="x", algorithm="lut").argv


def test_colprof_description_is_quoted_safely():
    cmd = build_colprof(name="x", description="My Display 2026")
    assert cmd.argv[cmd.argv.index("-D") + 1] == "My Display 2026"


def test_colprof_rejects_option_like_description():
    with pytest.raises(ToolError, match="不能以 - 开头"):
        build_colprof(name="x", description="-D/etc/passwd")


def test_colprof_truncates_long_description():
    cmd = build_colprof(name="x", description="A" * 500)
    assert len(cmd.argv[cmd.argv.index("-D") + 1]) == 200


# --------------------------------------------------------------------------
# 依赖前序产物的步骤
# --------------------------------------------------------------------------


def test_dispread_requires_calibration_file():
    """.cal 不存在时应给出可操作的提示, 而不是等 ArgyllCMS 自己报错。"""
    with pytest.raises(ToolError, match="请先运行 dispcal"):
        build_dispread(name="nonexistent-profile-xyz", use_calibration=True)


def test_dispread_without_calibration_skips_check(tmp_path, monkeypatch):
    cmd = build_dispread(name="anything", use_calibration=False)
    assert "-k" not in cmd.argv


def test_dispread_with_existing_cal(monkeypatch):
    cal = config.WORK_DIR / "testcal.cal"
    cal.write_text("# dummy cal\n", encoding="utf-8")
    try:
        cmd = build_dispread(name="testcal", use_calibration=True)
        assert cmd.argv[cmd.argv.index("-k") + 1] == "testcal.cal"
    finally:
        cal.unlink(missing_ok=True)


def test_chartread_requires_ti2():
    with pytest.raises(ToolError, match="找不到色卡定义文件"):
        tools.build_chartread(name="no-such-chart-xyz")


def test_install_profile_requires_icc():
    with pytest.raises(ToolError, match="找不到 profile 文件"):
        tools.build_dispwin_install(name="no-such-profile-xyz")


# --------------------------------------------------------------------------
# 分发器
# --------------------------------------------------------------------------


def test_build_dispatch():
    cmd = build("spotread", {"mode": "emissive"})
    assert isinstance(cmd, Command)
    assert "-e" in cmd.argv


def test_build_unknown_action():
    with pytest.raises(ToolError, match="未知动作"):
        build("rm-rf-slash", {})


def test_build_bad_params_gives_readable_error():
    with pytest.raises(ToolError, match="参数不正确"):
        build("spotread", {"not_a_real_param": 1})


def test_command_to_dict_hides_absolute_path():
    """界面上显示命令时, 不必让用户看见一长串 Homebrew 路径。"""
    payload = build_spotread().to_dict()
    assert payload["display"].startswith("spotread ")
    assert "/opt/homebrew" not in payload["display"]


# --------------------------------------------------------------------------
# 集成: 真实调用 ArgyllCMS
# --------------------------------------------------------------------------


@pytest.mark.skipif(config.ARGYLL_BIN is None, reason="未安装 ArgyllCMS")
def test_real_display_enumeration():
    displays = tools.list_displays()
    assert len(displays) >= 1, "至少应能枚举到一块显示器"
    assert all(d.index >= 1 for d in displays)
    assert any(d.width > 0 for d in displays)


@pytest.mark.skipif(config.ARGYLL_BIN is None, reason="未安装 ArgyllCMS")
def test_real_instrument_enumeration_does_not_crash():
    """仪器可能没插, 但枚举本身不该抛异常。"""
    instruments = tools.list_instruments()
    assert isinstance(instruments, list)
