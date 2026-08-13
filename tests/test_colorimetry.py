#!/usr/bin/env python3
"""色度学计算测试。

核心是 CIEDE2000 的 34 组标准测试向量 —— 来自 Sharma, Wu & Dalal (2005),
"The CIEDE2000 Color-Difference Formula: Implementation Notes, Supplementary
Test Data, and Mathematical Observations", Color Research & Application 30(1)。

这组数据专门挑选了公式的三个陷阱区: 色相角跨 0°/360° 环绕、C'=0 的中性色、
以及蓝色区的 RT 旋转项。绝大多数错误实现能通过前 20 组而在这些边界上翻车,
所以必须全测。
"""

from __future__ import annotations

import math

import pytest

from argyll.colorimetry import (
    DUV_LIMIT,
    WHITE_POINTS,
    adapt_white_point,
    delta_e_76,
    delta_e_94,
    delta_e_2000,
    describe,
    lab_to_xyz,
    srgb_to_hex,
    xy_to_cct_mccamy,
    xy_to_duv,
    xyz_to_duv,
    xyz_to_lab,
    xyz_to_lch,
    xyz_to_srgb,
    xyz_to_xy,
)

# (L1, a1, b1), (L2, a2, b2), 期望 ΔE00
CIEDE2000_TEST_DATA: list[tuple[tuple[float, float, float], tuple[float, float, float], float]] = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize(("lab1", "lab2", "expected"), CIEDE2000_TEST_DATA)
def test_ciede2000_sharma_vectors(lab1, lab2, expected):
    """34 组标准向量, 容差 1e-4 —— 论文给出的就是四位小数。"""
    assert delta_e_2000(lab1, lab2) == pytest.approx(expected, abs=1e-4)


def test_ciede2000_is_symmetric():
    """色差必须对称: ΔE(a,b) == ΔE(b,a)。"""
    for lab1, lab2, _ in CIEDE2000_TEST_DATA:
        assert delta_e_2000(lab1, lab2) == pytest.approx(delta_e_2000(lab2, lab1), abs=1e-10)


def test_ciede2000_identity_is_zero():
    for lab1, _, _ in CIEDE2000_TEST_DATA:
        assert delta_e_2000(lab1, lab1) == pytest.approx(0.0, abs=1e-12)


# --------------------------------------------------------------------------
# 其他色差公式
# --------------------------------------------------------------------------


def test_delta_e_76_is_euclidean():
    assert delta_e_76((50.0, 0.0, 0.0), (53.0, 4.0, 0.0)) == pytest.approx(5.0)


def test_delta_e_94_reduces_lightness_only_case():
    """纯明度差时 CIE94 退化为明度差本身(kL=1, SL=1)。"""
    assert delta_e_94((50.0, 0.0, 0.0), (55.0, 0.0, 0.0)) == pytest.approx(5.0)


def test_delta_e_94_is_asymmetric_by_design():
    """CIE94 以参考色计算 SC/SH, 因此交换两色结果会变 —— 这是公式特性而非缺陷。"""
    a = (60.0, 40.0, 20.0)
    b = (62.0, 45.0, 25.0)
    assert delta_e_94(a, b) != pytest.approx(delta_e_94(b, a), abs=1e-6)


def test_delta_e_ordering_on_saturated_colors():
    """在高饱和区, CIE76 会明显高估色差 —— 这正是 CIEDE2000 要修正的。"""
    ref = (50.0, 60.0, 40.0)
    sample = (50.0, 66.0, 44.0)
    assert delta_e_76(ref, sample) > delta_e_2000(ref, sample)


# --------------------------------------------------------------------------
# XYZ <-> Lab
# --------------------------------------------------------------------------


def test_white_point_maps_to_l100():
    """白点自身的 Lab 必须是 (100, 0, 0)。"""
    lightness, a_star, b_star = xyz_to_lab(WHITE_POINTS["D50"], WHITE_POINTS["D50"])
    assert lightness == pytest.approx(100.0, abs=1e-9)
    assert a_star == pytest.approx(0.0, abs=1e-9)
    assert b_star == pytest.approx(0.0, abs=1e-9)


def test_black_maps_to_l0():
    assert xyz_to_lab((0.0, 0.0, 0.0))[0] == pytest.approx(0.0, abs=1e-12)


@pytest.mark.parametrize(
    "xyz",
    [
        (96.422, 100.0, 82.521),
        (20.0, 30.0, 15.0),
        (5.0, 4.0, 3.0),
        (0.5, 0.4, 0.3),  # 近黑区, 走线性分支
        (0.05, 0.04, 0.03),
    ],
)
def test_lab_roundtrip(xyz):
    """XYZ → Lab → XYZ 必须回到原点, 尤其是近黑的线性分支。"""
    lab = xyz_to_lab(xyz)
    back = lab_to_xyz(lab)
    for original, restored in zip(xyz, back, strict=True):
        assert restored == pytest.approx(original, rel=1e-9, abs=1e-9)


def test_lch_matches_lab_polar_form():
    xyz = (20.0, 30.0, 15.0)
    _, a_star, b_star = xyz_to_lab(xyz)
    _, chroma, hue = xyz_to_lch(xyz)
    assert chroma == pytest.approx(math.hypot(a_star, b_star))
    assert hue == pytest.approx(math.degrees(math.atan2(b_star, a_star)) % 360.0)


def test_xy_sums_within_unit_triangle():
    x, y = xyz_to_xy(WHITE_POINTS["D65"])
    assert x == pytest.approx(0.3127, abs=1e-3)
    assert y == pytest.approx(0.3290, abs=1e-3)


def test_xy_of_zero_is_safe():
    """全黑输入不能抛除零异常。"""
    assert xyz_to_xy((0.0, 0.0, 0.0)) == (0.0, 0.0)


# --------------------------------------------------------------------------
# 色适应
# --------------------------------------------------------------------------


def test_adaptation_maps_white_to_white():
    """D50 白点经色适应后应正好落在 D65 白点上。"""
    result = adapt_white_point(WHITE_POINTS["D50"], WHITE_POINTS["D50"], WHITE_POINTS["D65"])
    for got, want in zip(result, WHITE_POINTS["D65"], strict=True):
        assert got == pytest.approx(want, rel=2e-3)


def test_adaptation_is_identity_for_same_white():
    xyz = (30.0, 40.0, 20.0)
    result = adapt_white_point(xyz, WHITE_POINTS["D50"], WHITE_POINTS["D50"])
    for got, want in zip(result, xyz, strict=True):
        assert got == pytest.approx(want, rel=1e-9)


def test_adaptation_roundtrip():
    xyz = (30.0, 40.0, 20.0)
    there = adapt_white_point(xyz, WHITE_POINTS["D50"], WHITE_POINTS["D65"])
    back = adapt_white_point(there, WHITE_POINTS["D65"], WHITE_POINTS["D50"])
    for got, want in zip(back, xyz, strict=True):
        assert got == pytest.approx(want, rel=1e-6)


# --------------------------------------------------------------------------
# sRGB 预览
# --------------------------------------------------------------------------


def test_d65_white_renders_as_white():
    r, g, b, in_gamut = xyz_to_srgb(WHITE_POINTS["D65"], "D65")
    assert (r, g, b) == (255, 255, 255)
    assert in_gamut


def test_d50_white_also_renders_as_white_after_adaptation():
    """关键: D50 测得的纸白, 经 Bradford 适应后应显示为中性白而非偏黄。

    若漏掉色适应, 蓝通道会明显低于红通道(肉眼可见的黄)。
    """
    r, g, b, _ = xyz_to_srgb(WHITE_POINTS["D50"], "D50")
    assert abs(r - b) <= 2, f"色适应缺失或有误: RGB=({r},{g},{b}) 偏色"
    assert min(r, g, b) >= 250


def test_black_renders_as_black():
    r, g, b, _ = xyz_to_srgb((0.0, 0.0, 0.0), "D50")
    assert (r, g, b) == (0, 0, 0)


def test_out_of_gamut_is_flagged():
    """高饱和青色超出 sRGB 色域, 必须被标记而不是悄悄裁剪。"""
    # 接近光谱轨迹的青色
    _, _, _, in_gamut = xyz_to_srgb((15.0, 30.0, 35.0), "D65")
    assert in_gamut is False


def test_bright_emissive_is_normalized():
    """发光测量 Y 常远超 100(如 250 cd/m²), 归一化后仍应是白色而非全裁剪。"""
    bright = tuple(v * 2.5 for v in WHITE_POINTS["D65"])
    r, g, b, _ = xyz_to_srgb(bright, "D65", normalize=True)  # type: ignore[arg-type]
    assert (r, g, b) == (255, 255, 255)


def test_hex_format():
    assert srgb_to_hex((255, 128, 0)) == "#ff8000"
    assert srgb_to_hex((0, 0, 0)) == "#000000"


# --------------------------------------------------------------------------
# 色温
# --------------------------------------------------------------------------


def test_cct_of_d65():
    x, y = xyz_to_xy(WHITE_POINTS["D65"])
    cct = xy_to_cct_mccamy(x, y)
    assert cct is not None
    assert cct == pytest.approx(6500.0, rel=0.02)


def test_cct_of_d50():
    x, y = xyz_to_xy(WHITE_POINTS["D50"])
    cct = xy_to_cct_mccamy(x, y)
    assert cct is not None
    assert cct == pytest.approx(5000.0, rel=0.02)


def test_cct_of_illuminant_a():
    """A 光源(钨丝灯)约 2856K —— 已接近 McCamy 近似的下沿。"""
    x, y = xyz_to_xy(WHITE_POINTS["A"])
    cct = xy_to_cct_mccamy(x, y)
    assert cct is not None
    assert cct == pytest.approx(2856.0, rel=0.05)


def test_cct_returns_none_when_undefined():
    """退化输入不能抛异常, 返回 None 让界面显示"—"。"""
    assert xy_to_cct_mccamy(0.3320, 0.1858) is None  # 分母为零
    assert xy_to_cct_mccamy(0.0, 0.0) is None  # 退化色度点, 物理上无意义
    assert xy_to_cct_mccamy(-0.1, 0.3) is None  # 负色度坐标
    assert xy_to_cct_mccamy(0.7, 0.6) is None  # x+y > 1, 落在色度三角形外
    assert xy_to_cct_mccamy(0.05, 0.9) is None  # 极端绿, CCT 超出合理区间


# --------------------------------------------------------------------------
# 汇总接口
# --------------------------------------------------------------------------


def test_describe_contains_all_fields():
    result = describe(WHITE_POINTS["D50"], "D50")
    for key in ("xyz", "lab", "lch", "xy", "srgb", "hex", "in_gamut", "cct", "illuminant"):
        assert key in result, f"describe() 缺少字段 {key}"
    assert result["hex"].startswith("#")
    assert result["lab"][0] == pytest.approx(100.0, abs=1e-9)


def test_describe_handles_unknown_illuminant():
    """未知光源名应回退到 D50 而不是 KeyError。"""
    result = describe((20.0, 20.0, 20.0), "NOT-A-REAL-ILLUMINANT")
    assert result["lab"][0] > 0


# --------------------------------------------------------------------------
# Duv (到黑体轨迹的距离)
# --------------------------------------------------------------------------


def test_duv_of_illuminant_a_is_near_zero():
    """A 光源就是 2856K 的黑体辐射, 按定义应正好落在轨迹上。

    这是验证 Ohno 多项式系数是否抄对的最硬指标 —— 系数错一位, 这里立刻偏掉。
    """
    duv = xyz_to_duv(WHITE_POINTS["A"])
    assert duv is not None
    assert abs(duv) < 0.001, f"A 光源 Duv={duv:.5f}, 应接近 0"


def test_duv_of_daylight_illuminants_is_small():
    """D 系列在日光轨迹上, 略微偏离黑体轨迹但幅度很小。"""
    for name in ("D50", "D55", "D65", "D75"):
        duv = xyz_to_duv(WHITE_POINTS[name])
        assert duv is not None
        assert abs(duv) < 0.01, f"{name} 的 Duv={duv:.5f} 偏离过大"


def test_duv_sign_convention():
    """正值偏绿, 负值偏品红。F2 冷白荧光灯明显偏绿。"""
    duv = xyz_to_duv(WHITE_POINTS["F2"])
    assert duv is not None
    assert duv > 0, "荧光灯 F2 应为正 Duv(偏绿)"


def test_saturated_green_exceeds_duv_limit():
    """饱和绿远离黑体轨迹, 必须超过阈值 —— 这正是 CCT 该被拒绝的场景。"""
    duv = xy_to_duv(0.05, 0.9)
    assert duv is not None
    assert abs(duv) > DUV_LIMIT


def test_describe_exposes_duv():
    result = describe(WHITE_POINTS["D50"], "D50")
    assert "duv" in result
    assert result["duv"] is not None
    assert abs(result["duv"]) < 0.01
