#!/usr/bin/env python3
"""ArgyllCMS 工具链封装 —— 设备枚举与命令构建。

## 职责

1. **枚举**: 从 ``工具 -?`` 的用法输出里解析出仪器与显示器列表
2. **构建**: 把界面上的选项翻译成 argv, 并在此处集中做参数校验

## 为什么校验放在这一层

``session.py`` 用 ``os.execve`` 直接执行, 参数不经过 shell, 因此没有元字符
注入的问题。真正的风险在别处:

- **路径穿越**: 前端传来的文件名如果是 ``../../.ssh/id_rsa``, colprof 就会
  往用户主目录里写东西。所有文件名必须经 :func:`safe_work_name` 收敛到 work/ 内。
- **参数注入**: 形如 ``-I/etc/passwd`` 的"文件名"会被 ArgyllCMS 当成选项解析。
  凡是以 ``-`` 开头的值一律拒绝。

命令一旦构建完成, 就是一个可信的 argv 列表。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import config

# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------

#: ArgyllCMS 的列表项统一长这样:  "    1 = 'Built-in Retina Display, at 0, 0, ...'"
RE_LIST_ITEM = re.compile(r"^\s*(\d+)\s*=\s*'(.+)'\s*$")

#: 显示器描述里的几何信息: "..., at 852, -1692, width 3008, height 1692"
RE_DISPLAY_GEOM = re.compile(
    r"^(?P<name>.*?),\s*at\s*(?P<x>-?\d+),\s*(?P<y>-?\d+),\s*"
    r"width\s*(?P<w>\d+),\s*height\s*(?P<h>\d+)"
)


@dataclass(frozen=True, slots=True)
class Instrument:
    """一台可用的测量仪器。"""

    index: int  # 传给 -c 的序号
    description: str
    model: str | None  # 从括号中提取, 如 "X-Rite i1 Pro 2"
    is_measuring_device: bool  # 串口/蓝牙端口也会混在列表里, 需区分

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "description": self.description,
            "model": self.model,
            "is_measuring_device": self.is_measuring_device,
        }


@dataclass(frozen=True, slots=True)
class Display:
    """一块可校准的显示器。"""

    index: int  # 传给 -d 的序号
    description: str
    name: str
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    is_primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "description": self.description,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "is_primary": self.is_primary,
        }


def _usage_text(tool: str) -> str:
    """取某个工具的用法输出。

    ArgyllCMS 没有 --help; 传 ``-?`` 会打印用法并以非零码退出, 这是预期行为。
    输出可能落在 stdout 也可能落在 stderr, 两个都收。
    """
    try:
        proc = subprocess.run(  # noqa: S603 - 路径来自 config 白名单
            [config.tool_path(tool), "-?"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def _parse_list_items(text: str, section_marker: str) -> list[tuple[int, str]]:
    """从用法文本中抽取某个选项下方的编号列表。

    列表紧跟在形如 "-c listno" / "-d n" 的说明行之后, 直到出现下一个不匹配
    列表格式的行为止。定位到 marker 再开始收集, 避免把别处的编号行也算进来。
    """
    items: list[tuple[int, str]] = []
    collecting = False

    for line in text.splitlines():
        if not collecting:
            if section_marker in line:
                collecting = True
            continue

        m = RE_LIST_ITEM.match(line)
        if m:
            items.append((int(m.group(1)), m.group(2)))
        elif items:
            break  # 列表结束
        elif line.strip().startswith("-"):
            break  # 该选项下没有列表, 已经到下一个选项了

    return items


def list_instruments() -> list[Instrument]:
    """枚举可用仪器 (spotread -? 的 -c 列表)。"""
    instruments: list[Instrument] = []

    for index, description in _parse_list_items(_usage_text("spotread"), "-c listno"):
        # 真正的仪器会带括号型号, 如 "usb33: (X-Rite i1 Pro 2)";
        # 串口/蓝牙端口只有路径, 如 "/dev/cu.Bluetooth-Incoming-Port"。
        model_match = re.search(r"\(([^)]+)\)", description)
        model = model_match.group(1) if model_match else None
        is_device = model is not None and not description.startswith("/dev/")

        instruments.append(
            Instrument(
                index=index,
                description=description,
                model=model,
                is_measuring_device=is_device,
            )
        )
    return instruments


def list_displays() -> list[Display]:
    """枚举显示器 (dispwin -? 的 -d 列表)。"""
    displays: list[Display] = []

    for index, description in _parse_list_items(_usage_text("dispwin"), "-d n"):
        geom = RE_DISPLAY_GEOM.match(description)
        if geom:
            displays.append(
                Display(
                    index=index,
                    description=description,
                    name=geom.group("name").strip(),
                    x=int(geom.group("x")),
                    y=int(geom.group("y")),
                    width=int(geom.group("w")),
                    height=int(geom.group("h")),
                    is_primary="Primary Display" in description,
                )
            )
        else:
            displays.append(
                Display(
                    index=index, description=description, name=description.split(",")[0].strip()
                )
            )
    return displays


# --------------------------------------------------------------------------
# 参数校验
# --------------------------------------------------------------------------


class ToolError(ValueError):
    """参数非法 —— 拒绝构建命令。"""


#: 工作文件名允许的字符。刻意保守: ArgyllCMS 会在同名基础上派生出
#: name.ti1 / name.ti3 / name.cal / name.icc 等一串文件, 名字里出现空格或
#: 特殊字符会让后续排查变得很痛苦。
RE_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


def safe_work_name(name: str) -> str:
    """校验工作文件的基名(不含目录)。

    Raises:
        ToolError: 名字为空、含非法字符、或试图路径穿越。
    """
    if not name or not name.strip():
        raise ToolError("文件名不能为空")
    name = name.strip()

    if "/" in name or "\\" in name or ".." in name:
        raise ToolError(f"文件名 {name!r} 含路径分隔符或上级引用")
    if name.startswith("-"):
        # 以 - 开头的"文件名"会被 ArgyllCMS 当成选项解析
        raise ToolError(f"文件名 {name!r} 不能以 - 开头")
    if not RE_SAFE_NAME.match(name):
        raise ToolError(f"文件名 {name!r} 含非法字符, 只允许字母数字与 . _ -")
    return name


def resolve_work_path(name: str, *, suffix: str = "") -> Path:
    """把基名解析为 work/ 目录内的绝对路径, 并确认没有逃逸。

    双保险: 即使 :func:`safe_work_name` 将来被绕过, 这里的 ``resolve()`` +
    前缀检查也能挡住指向工作目录之外的路径。
    """
    base = safe_work_name(name)
    path = (config.WORK_DIR / (base + suffix)).resolve()
    work_root = config.WORK_DIR.resolve()
    if not path.is_relative_to(work_root):
        raise ToolError(f"路径 {path} 逃逸出工作目录")
    return path


def _check_index(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError(f"{label} 必须是整数")
    if not (1 <= value <= 99):
        raise ToolError(f"{label} 超出范围: {value}")
    return value


def _check_range(value: float, low: float, high: float, label: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ToolError(f"{label} 必须是数值") from exc
    if not (low <= v <= high):
        raise ToolError(f"{label} 应在 {low}–{high} 之间, 收到 {v}")
    return v


def _check_choice(value: str, allowed: set[str], label: str) -> str:
    if value not in allowed:
        raise ToolError(f"{label} 必须是 {sorted(allowed)} 之一, 收到 {value!r}")
    return value


# --------------------------------------------------------------------------
# 选项字典
# --------------------------------------------------------------------------

#: spotread 的测量模式 -> 对应选项。反射模式是默认值, 不需要额外开关。
MEASURE_MODES: dict[str, list[str]] = {
    "reflective": [],  # 默认: 反射稿
    "emissive": ["-e"],  # 显示器等自发光体
    "ambient": ["-a"],  # 环境光照度
    "transmissive": ["-t"],  # 透射稿
}

#: ISO 13655 测量条件 -> spotread 的 -F 参数。
#:
#: 这是印刷行业最容易出错的一处: 测含荧光增白剂(OBA)的纸张时,
#: M0/M1/M2 三种模式的 b* 值会有可观差异。M1 是现行印刷标准。
FILTER_MODES: dict[str, str] = {
    "M0": "n",  # 无滤镜, 仪器原生钨丝灯
    "M1": "5",  # D50 含 UV —— ISO 13655 推荐
    "M2": "u",  # UV Cut, 排除荧光增白剂影响
    "M3": "p",  # 偏振滤镜
    "D65": "6",
}

DISPLAY_TYPES: dict[str, str] = {"lcd": "l", "crt": "c"}

QUALITY_LEVELS: dict[str, str] = {"low": "l", "medium": "m", "high": "h", "ultra": "u"}

#: colprof 的 profile 算法。
#:
#: 显示器用 matrix+shaper(-aS/-aG)即可, 体积小、外推行为稳定;
#: 打印机等非线性设备必须用 LUT(-aX), 否则色域边界会严重失真。
PROFILE_ALGORITHMS: dict[str, str] = {
    "shaper_matrix": "S",  # 曲线 + 矩阵, 显示器首选
    "gamma_matrix": "G",  # 单 gamma + 矩阵, 最省
    "matrix_only": "m",
    "lut": "X",  # 完整 LUT, 打印机必需
    "lut_xyz": "x",
}

OBSERVERS: dict[str, str] = {
    "1931_2": "1931_2",
    "1964_10": "1964_10",
    "2015_2": "2015_2",
    "2015_10": "2015_10",
}

ILLUMINANTS = {"A", "C", "D50", "D50M2", "D65", "F5", "F8", "F10"}


# --------------------------------------------------------------------------
# 命令构建
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Command:
    """一条待执行的命令。"""

    tool: str
    argv: list[str]
    label: str
    description: str = ""
    produces: list[str] = field(default_factory=list)  # 预期产出的文件名

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "argv": self.argv,
            "label": self.label,
            "description": self.description,
            "produces": self.produces,
            # 展示用: 去掉绝对路径前缀, 界面上看着清爽
            "display": " ".join([self.tool, *self.argv[1:]]),
        }


def _base(tool: str) -> list[str]:
    return [config.tool_path(tool)]


def build_spotread(
    *,
    instrument: int = 1,
    mode: Literal["reflective", "emissive", "ambient", "transmissive"] = "reflective",
    filter_mode: str | None = None,
    display_type: str | None = None,
    illuminant: str | None = None,
    observer: str | None = None,
    spectrum: bool = True,
    high_res: bool = False,
    extra_verbose: bool = True,
) -> Command:
    """构建 spotread 点测量命令。

    Args:
        instrument: 仪器序号 (-c)。
        mode: 测量模式。反射是默认值。
        filter_mode: M0/M1/M2/M3/D65, 仅反射与透射模式有意义。
        display_type: lcd/crt, 仅发光模式需要。
        illuminant: 计算用光源, 反射测量的行业默认是 D50。
        observer: CIE 观察者, 默认 1931 2°。
        spectrum: 是否输出光谱数据 (-s), 界面画曲线需要。
        high_res: i1Pro 系列的高分辨率光谱模式 (-H)。
    """
    argv = _base("spotread")
    if extra_verbose:
        argv.append("-v")

    argv += ["-c", str(_check_index(instrument, "仪器序号"))]
    argv += MEASURE_MODES[_check_choice(mode, set(MEASURE_MODES), "测量模式")]

    if spectrum:
        argv.append("-s")
    if high_res:
        argv.append("-H")

    if display_type is not None:
        argv += [
            "-y",
            DISPLAY_TYPES[_check_choice(display_type.lower(), set(DISPLAY_TYPES), "显示类型")],
        ]
    elif mode == "emissive":
        argv += ["-y", "l"]  # 发光模式必须给显示类型, 默认按 LCD

    if filter_mode is not None:
        if mode in ("emissive", "ambient"):
            raise ToolError(f"{mode} 模式不适用滤镜设置 (M0/M1/M2 只对反射与透射有意义)")
        argv += ["-F", FILTER_MODES[_check_choice(filter_mode.upper(), set(FILTER_MODES), "滤镜")]]

    if illuminant is not None:
        argv += ["-i", _check_choice(illuminant.upper(), ILLUMINANTS, "光源")]
    if observer is not None:
        argv += ["-Q", _check_choice(observer, set(OBSERVERS), "观察者")]

    return Command(
        tool="spotread",
        argv=argv,
        label=f"点测量 ({mode})",
        description="逐点测量, 空格触发读数, q 退出",
    )


def build_dispcal(
    *,
    name: str,
    display: int = 1,
    instrument: int = 1,
    quality: str = "medium",
    white_point: float | None = 6500.0,
    brightness: float | None = None,
    gamma: float = 2.2,
    display_type: str = "lcd",
    interactive_adjust: bool = False,
) -> Command:
    """构建 dispcal 显示器校准命令, 产出 ``name.cal``。

    Args:
        white_point: 目标色温(K)。None 表示沿用显示器原生白点。
        brightness: 目标白场亮度 cd/m²。None 表示不强制。
        gamma: 目标响应曲线 gamma。
        interactive_adjust: 是否进入交互式调节环节 (-m 关闭)。
            关掉它可以让流程全自动, 但就无法借助软件提示调整显示器的
            硬件旋钮/OSD 设置。
    """
    base = safe_work_name(name)
    argv = _base("dispcal")
    argv.append("-v")

    argv += ["-d", str(_check_index(display, "显示器序号"))]
    argv += ["-c", str(_check_index(instrument, "仪器序号"))]
    argv += ["-q", QUALITY_LEVELS[_check_choice(quality, set(QUALITY_LEVELS), "质量")]]
    argv += [
        "-y",
        DISPLAY_TYPES[_check_choice(display_type.lower(), set(DISPLAY_TYPES), "显示类型")],
    ]

    if not interactive_adjust:
        argv.append("-m")  # 跳过手动调节环节, 直接校准

    if white_point is not None:
        argv.append(f"-t{_check_range(white_point, 2000, 15000, '目标色温'):.0f}")
    if brightness is not None:
        argv += ["-b", f"{_check_range(brightness, 1, 1000, '目标亮度'):.1f}"]

    argv += ["-g", f"{_check_range(gamma, 0.5, 4.0, 'gamma'):.3f}"]
    argv.append(base)

    return Command(
        tool="dispcal",
        argv=argv,
        label="显示器校准",
        description="测量并生成显示器校准曲线 (.cal)",
        produces=[f"{base}.cal"],
    )


def build_targen(
    *,
    name: str,
    patches: int = 500,
    device: Literal["rgb", "cmyk", "gray"] = "rgb",
    optimize: bool = True,
) -> Command:
    """构建 targen 测试色块生成命令, 产出 ``name.ti1``。

    Args:
        patches: 色块数量。显示器 profile 通常 400–1000;
            少于 ~200 会让 LUT 拟合明显变差, 多于 ~3000 则收益递减而耗时线性增长。
    """
    base = safe_work_name(name)
    device_flag = {"rgb": "-d3", "cmyk": "-d4", "gray": "-d1"}[
        _check_choice(device, {"rgb", "cmyk", "gray"}, "设备类型")
    ]

    argv = _base("targen")
    argv += ["-v", device_flag]
    if optimize:
        argv.append("-G")  # 良好的空间分布优化
    argv += ["-f", str(int(_check_range(patches, 20, 5000, "色块数")))]
    argv.append(base)

    return Command(
        tool="targen",
        argv=argv,
        label="生成测试色块",
        description=f"生成 {patches} 个测试色块 (.ti1)",
        produces=[f"{base}.ti1"],
    )


def build_dispread(
    *,
    name: str,
    display: int = 1,
    instrument: int = 1,
    display_type: str = "lcd",
    use_calibration: bool = True,
) -> Command:
    """构建 dispread 色块读取命令, 读 ``name.ti1`` 产出 ``name.ti3``。

    Args:
        use_calibration: 是否加载 dispcal 产出的 .cal (-k)。
            标准流程应当加载 —— 否则测到的是未校准状态, 生成的 profile
            与实际显示效果对不上。
    """
    base = safe_work_name(name)
    argv = _base("dispread")
    argv.append("-v")
    argv += ["-d", str(_check_index(display, "显示器序号"))]
    argv += ["-c", str(_check_index(instrument, "仪器序号"))]
    argv += [
        "-y",
        DISPLAY_TYPES[_check_choice(display_type.lower(), set(DISPLAY_TYPES), "显示类型")],
    ]

    if use_calibration:
        cal_path = resolve_work_path(base, suffix=".cal")
        if not cal_path.is_file():
            raise ToolError(f"找不到校准文件 {base}.cal —— 请先运行 dispcal")
        argv += ["-k", f"{base}.cal"]

    argv.append(base)

    return Command(
        tool="dispread",
        argv=argv,
        label="读取色块",
        description="逐个显示并测量色块 (.ti1 -> .ti3)",
        produces=[f"{base}.ti3"],
    )


def build_colprof(
    *,
    name: str,
    quality: str = "medium",
    algorithm: str = "shaper_matrix",
    description: str | None = None,
    copyright_text: str | None = None,
) -> Command:
    """构建 colprof 命令, 由 ``name.ti3`` 生成 ``name.icc``。"""
    base = safe_work_name(name)
    argv = _base("colprof")
    argv.append("-v")
    argv += ["-q", QUALITY_LEVELS[_check_choice(quality, set(QUALITY_LEVELS), "质量")]]
    argv.append(
        "-a" + PROFILE_ALGORITHMS[_check_choice(algorithm, set(PROFILE_ALGORITHMS), "算法")]
    )

    if description:
        # profile 内嵌描述可以带空格, 但仍不允许以 - 开头, 否则会被当成选项
        if description.startswith("-"):
            raise ToolError("profile 描述不能以 - 开头")
        argv += ["-D", description[:200]]
    if copyright_text:
        if copyright_text.startswith("-"):
            raise ToolError("版权信息不能以 - 开头")
        argv += ["-C", copyright_text[:200]]

    argv.append(base)

    return Command(
        tool="colprof",
        argv=argv,
        label="生成 ICC Profile",
        description="由测量数据拟合 ICC profile (.ti3 -> .icc)",
        produces=[f"{base}.icc"],
    )


def build_chartread(
    *,
    name: str,
    instrument: int = 1,
    filter_mode: str | None = "M1",
    strip_mode: bool = False,
) -> Command:
    """构建 chartread 实体色卡读取命令, 读 ``name.ti2`` 产出 ``name.ti3``。

    Args:
        strip_mode: True 用条带扫描(滑过一整行), False 逐块点测。
            i1Pro 支持条带扫描, 速度快得多, 但对滑动速度有要求。
    """
    base = safe_work_name(name)
    ti2 = resolve_work_path(base, suffix=".ti2")
    if not ti2.is_file():
        raise ToolError(f"找不到色卡定义文件 {base}.ti2")

    argv = _base("chartread")
    argv.append("-v")
    argv += ["-c", str(_check_index(instrument, "仪器序号"))]
    if not strip_mode:
        argv.append("-p")  # 逐块点测模式
    if filter_mode:
        argv += ["-F", FILTER_MODES[_check_choice(filter_mode.upper(), set(FILTER_MODES), "滤镜")]]
    argv.append(base)

    return Command(
        tool="chartread",
        argv=argv,
        label="读取色卡",
        description="扫描实体色卡 (.ti2 -> .ti3)",
        produces=[f"{base}.ti3"],
    )


def build_dispwin_install(*, name: str, display: int = 1, system_wide: bool = False) -> Command:
    """构建 ICC profile 安装命令。

    Args:
        system_wide: True 装到系统级(需要管理员权限, 本服务不以 root 运行,
            因此通常会失败); False 装到当前用户级。
    """
    base = safe_work_name(name)
    icc = resolve_work_path(base, suffix=".icc")
    if not icc.is_file():
        raise ToolError(f"找不到 profile 文件 {base}.icc")

    argv = _base("dispwin")
    argv.append("-v")
    argv += ["-d", str(_check_index(display, "显示器序号"))]
    argv.append("-S" + ("s" if system_wide else "u"))
    argv += ["-I", f"{base}.icc"]

    return Command(
        tool="dispwin",
        argv=argv,
        label="安装 Profile",
        description=f"把 {base}.icc 安装为显示器 {display} 的色彩配置",
        produces=[],
    )


def build_dispwin_uninstall(*, name: str, display: int = 1, system_wide: bool = False) -> Command:
    """构建 ICC profile 卸载命令。"""
    base = safe_work_name(name)
    argv = _base("dispwin")
    argv.append("-v")
    argv += ["-d", str(_check_index(display, "显示器序号"))]
    argv.append("-S" + ("s" if system_wide else "u"))
    argv += ["-U", f"{base}.icc"]

    return Command(
        tool="dispwin",
        argv=argv,
        label="卸载 Profile",
        description=f"移除 {base}.icc",
    )


def build_profcheck(*, name: str, verbose_level: int = 2) -> Command:
    """构建 profcheck 命令, 用 ``name.ti3`` 校验 ``name.icc`` 的精度。"""
    base = safe_work_name(name)
    for suffix in (".ti3", ".icc"):
        if not resolve_work_path(base, suffix=suffix).is_file():
            raise ToolError(f"找不到 {base}{suffix}")

    argv = _base("profcheck")
    argv.append("-v" + str(int(_check_range(verbose_level, 1, 3, "详细级别"))))
    argv.append("-k")  # 输出每个色块的误差, 便于界面画分布
    argv += [f"{base}.ti3", f"{base}.icc"]

    return Command(
        tool="profcheck",
        argv=argv,
        label="校验 Profile",
        description="计算 profile 的平均/最大色差",
    )


# --------------------------------------------------------------------------
# 工作流
# --------------------------------------------------------------------------

#: 显示器 profile 的标准流程。界面按此顺序引导用户, 每步产出下一步的输入。
DISPLAY_PROFILE_WORKFLOW: tuple[dict[str, str], ...] = (
    {
        "step": "dispcal",
        "title": "校准显示器",
        "detail": "测量并调整白点、亮度与灰阶响应, 产出 .cal 曲线",
    },
    {
        "step": "targen",
        "title": "生成测试色块",
        "detail": "按设定数量生成均匀分布的 RGB 测试色块, 产出 .ti1",
    },
    {
        "step": "dispread",
        "title": "读取色块",
        "detail": "在校准状态下逐个显示并测量色块, 产出 .ti3",
    },
    {
        "step": "colprof",
        "title": "生成 Profile",
        "detail": "由测量数据拟合 ICC profile, 产出 .icc",
    },
    {
        "step": "dispwin",
        "title": "安装 Profile",
        "detail": "把 profile 设为该显示器的系统色彩配置",
    },
)

#: 命令构建器注册表。server.py 按名字分发, 避免写一长串 if/elif。
BUILDERS = {
    "spotread": build_spotread,
    "dispcal": build_dispcal,
    "targen": build_targen,
    "dispread": build_dispread,
    "colprof": build_colprof,
    "chartread": build_chartread,
    "install_profile": build_dispwin_install,
    "uninstall_profile": build_dispwin_uninstall,
    "profcheck": build_profcheck,
}


def build(action: str, params: dict[str, Any]) -> Command:
    """按动作名分发到对应的构建器。

    Raises:
        ToolError: 动作未注册, 或参数非法。
    """
    builder = BUILDERS.get(action)
    if builder is None:
        raise ToolError(f"未知动作: {action!r}")
    try:
        return builder(**params)  # type: ignore[operator]
    except TypeError as exc:
        # 参数名拼错或缺失必填项 —— 转成用户可读的错误而不是 500
        raise ToolError(f"动作 {action} 的参数不正确: {exc}") from exc
