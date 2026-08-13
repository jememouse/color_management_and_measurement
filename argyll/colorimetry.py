#!/usr/bin/env python3
"""色度学计算 —— 纯函数, 无副作用, 不依赖 ArgyllCMS。

职责边界: ArgyllCMS 已经给出 XYZ 与 Lab, 本模块补的是它不直接输出、
而界面又需要的派生量:

    - 屏幕预览色 (XYZ → sRGB), 用于在网页上画出"这一测点是什么颜色"
    - 相关色温 CCT, 用于纸白/光源判读
    - 色差 ΔE76 / ΔE94 / ΔE2000, 用于与参考色比对

**关于白点**: ArgyllCMS 反射测量默认以 D50 为计算光源(印刷业标准),
而 sRGB 的白点是 D65。两者之间必须做 Bradford 色适应, 否则纸白会
偏黄一大截。这是最容易被忽略、后果又最直观的一步。
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------
# 标准光源白点 (CIE 1931 2° 观察者), 归一化到 Y=100
# --------------------------------------------------------------------------

WHITE_POINTS: dict[str, tuple[float, float, float]] = {
    "A": (109.850, 100.000, 35.585),
    "C": (98.074, 100.000, 118.232),
    "D50": (96.422, 100.000, 82.521),
    "D55": (95.682, 100.000, 92.149),
    "D65": (95.047, 100.000, 108.883),
    "D75": (94.972, 100.000, 122.638),
    "F2": (99.187, 100.000, 67.395),
    "F7": (95.044, 100.000, 108.755),
    "F11": (100.966, 100.000, 64.370),
}

# CIE Lab 的两个魔数: ε = 216/24389, κ = 24389/27。
# 用精确分数而非 0.008856 / 903.3 —— 后者是旧标准的舍入值,
# 在近黑区会带来可观的偏差。
_EPSILON = 216.0 / 24389.0
_KAPPA = 24389.0 / 27.0


# --------------------------------------------------------------------------
# 色适应 (Bradford)
# --------------------------------------------------------------------------

# Bradford 锥体响应矩阵。相比 von Kries 与 XYZ Scaling,
# Bradford 在跨光源转换上的表现最好, 是 ICC 规范采用的方法。
_BRADFORD = (
    (0.8951, 0.2664, -0.1614),
    (-0.7502, 1.7135, 0.0367),
    (0.0389, -0.0685, 1.0296),
)


def _invert3(m: tuple[tuple[float, ...], ...]) -> tuple[tuple[float, ...], ...]:
    """3x3 矩阵求逆。

    这里现算而不抄文献里的舍入值: 常见的 Bradford 逆矩阵印刷版本只有 7 位有效
    数字, 用它做"同白点适应"时结果无法精确回到原值, 往返转换会持续累积漂移。
    由原矩阵直接求逆能保证 M @ M⁻¹ 在浮点精度内严格为单位阵。
    """
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-12:
        raise ValueError("矩阵不可逆")
    return (
        ((e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det),
        ((f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det),
        ((d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det),
    )


_BRADFORD_INV = _invert3(_BRADFORD)


def _mat_vec(m: tuple[tuple[float, ...], ...], v: tuple[float, float, float]) -> list[float]:
    return [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]


def adapt_white_point(
    xyz: tuple[float, float, float],
    src_white: tuple[float, float, float],
    dst_white: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Bradford 色适应: 把在 src_white 下测得的 XYZ 转换到 dst_white 下。

    例: 反射测量得到的 D50 XYZ, 要在 sRGB(D65) 屏幕上正确显示, 必须先经此转换。
    """
    src_lms = _mat_vec(_BRADFORD, src_white)
    dst_lms = _mat_vec(_BRADFORD, dst_white)
    lms = _mat_vec(_BRADFORD, xyz)

    # 逐通道缩放锥体响应
    adapted = [lms[i] * (dst_lms[i] / src_lms[i]) if src_lms[i] else 0.0 for i in range(3)]
    result = _mat_vec(_BRADFORD_INV, (adapted[0], adapted[1], adapted[2]))
    return (result[0], result[1], result[2])


# --------------------------------------------------------------------------
# XYZ <-> Lab
# --------------------------------------------------------------------------


def xyz_to_lab(
    xyz: tuple[float, float, float],
    white: tuple[float, float, float] = WHITE_POINTS["D50"],
) -> tuple[float, float, float]:
    """XYZ → CIE L*a*b*。输入 XYZ 与白点同尺度(通常 Y=100)。"""

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > _EPSILON else (_KAPPA * t + 16.0) / 116.0

    fx, fy, fz = (f(xyz[i] / white[i]) if white[i] else 0.0 for i in range(3))
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def lab_to_xyz(
    lab: tuple[float, float, float],
    white: tuple[float, float, float] = WHITE_POINTS["D50"],
) -> tuple[float, float, float]:
    """CIE L*a*b* → XYZ。"""
    lightness, a_star, b_star = lab
    fy = (lightness + 16.0) / 116.0
    fx = fy + a_star / 500.0
    fz = fy - b_star / 200.0

    def finv(t: float) -> float:
        return t**3 if t**3 > _EPSILON else (116.0 * t - 16.0) / _KAPPA

    yr = fy**3 if lightness > _KAPPA * _EPSILON else lightness / _KAPPA
    return (finv(fx) * white[0], yr * white[1], finv(fz) * white[2])


def xyz_to_xy(xyz: tuple[float, float, float]) -> tuple[float, float]:
    """XYZ → CIE 1931 xy 色度坐标。"""
    total = xyz[0] + xyz[1] + xyz[2]
    if total <= 0:
        return (0.0, 0.0)
    return (xyz[0] / total, xyz[1] / total)


def xyz_to_lch(
    xyz: tuple[float, float, float],
    white: tuple[float, float, float] = WHITE_POINTS["D50"],
) -> tuple[float, float, float]:
    """XYZ → CIE LCh(ab)。h 以度为单位, 落在 [0, 360)。"""
    lightness, a_star, b_star = xyz_to_lab(xyz, white)
    chroma = math.hypot(a_star, b_star)
    hue = math.degrees(math.atan2(b_star, a_star)) % 360.0
    return (lightness, chroma, hue)


# --------------------------------------------------------------------------
# XYZ -> sRGB (屏幕预览)
# --------------------------------------------------------------------------

# sRGB 原色矩阵 (D65)
_XYZ_TO_LINEAR_SRGB = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)


def _srgb_gamma(c: float) -> float:
    """线性光 → sRGB 电信号 (IEC 61966-2-1 传输函数)。"""
    if c <= 0.0031308:
        return 12.92 * c
    return 1.055 * (c ** (1.0 / 2.4)) - 0.055


def xyz_to_srgb(
    xyz: tuple[float, float, float],
    src_white: str | tuple[float, float, float] = "D50",
    *,
    normalize: bool = True,
) -> tuple[int, int, int, bool]:
    """XYZ → sRGB 8bit, 用于在网页上画出该测点的近似颜色。

    Args:
        xyz: 输入 XYZ, Y 以 100 为满刻度。
        src_white: 测量所用的计算光源。反射测量通常是 D50。
        normalize: 若 Y > 100(发光测量常见, 亮度可达数百 cd/m²),
            是否按亮度 Y 归一化。关闭则直接裁剪。

    Returns:
        (r, g, b, in_gamut) —— in_gamut 为 False 表示该颜色超出 sRGB 色域,
        显示值是裁剪后的近似, 界面应给出提示而不是假装准确。
    """
    white = WHITE_POINTS[src_white] if isinstance(src_white, str) else src_white

    # D50 → D65: 不做这一步, 纸白会明显偏黄
    adapted = adapt_white_point(xyz, white, WHITE_POINTS["D65"])

    # 归一化必须以亮度 Y 为准, 不能用 max(XYZ)。
    # D65 白点的 Z 是 108.883 —— 按最大分量缩放会把纯白压成 246 级灰,
    # 而 Y 才是"多亮"的物理量, 也正是发光测量里可能超过 100 的那一个。
    scale = 100.0
    if normalize and adapted[1] > 100.0:
        scale = adapted[1]

    linear = _mat_vec(_XYZ_TO_LINEAR_SRGB, tuple(v / scale for v in adapted))  # type: ignore[arg-type]

    # 任一通道为负 => 该颜色在 sRGB 三角形之外, 无法准确再现
    in_gamut = all(-1e-6 <= c <= 1.0 + 1e-6 for c in linear)

    channels = []
    for c in linear:
        clamped = min(1.0, max(0.0, c))
        channels.append(round(_srgb_gamma(clamped) * 255))
    return (channels[0], channels[1], channels[2], in_gamut)


def srgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


# --------------------------------------------------------------------------
# 相关色温
# --------------------------------------------------------------------------


# Ohno (2013) 用于计算 Duv 的多项式系数, 按 a 的升幂排列。
_DUV_COEFFS = (
    -0.471106,
    1.925865,
    -2.4243787,
    1.5317403,
    -0.5179722,
    0.0893944,
    -0.00616793,
)

#: CCT 的适用边界。色度点离黑体轨迹越远, "色温"越没有物理意义 ——
#: 对一块饱和绿标注 8791K 是纯粹的误导。CIE 15 建议仅在 |Duv| < 0.05
#: 范围内使用相关色温, 这里采用该上限。
DUV_LIMIT = 0.05


def xy_to_uv60(x: float, y: float) -> tuple[float, float]:
    """CIE 1931 xy → CIE 1960 UCS uv。Duv 定义在这个均匀色度空间里。"""
    denom = -2.0 * x + 12.0 * y + 3.0
    if abs(denom) < 1e-12:
        return (0.0, 0.0)
    return (4.0 * x / denom, 6.0 * y / denom)


def xy_to_duv(x: float, y: float) -> float | None:
    """色度点到普朗克黑体轨迹的有符号距离 (Ohno 2013 近似)。

    正值表示偏绿(轨迹上方), 负值表示偏品红(轨迹下方)。
    照明工程中 |Duv| > 0.006 即认为光源明显偏色。

    Returns:
        Duv; 输入退化时返回 None。
    """
    u, v = xy_to_uv60(x, y)
    du = u - 0.292
    dv = v - 0.24
    dist = math.hypot(du, dv)
    if dist < 1e-12:
        return 0.0

    # acos 的定义域保护: 浮点误差可能让比值略微越过 ±1
    cos_a = max(-1.0, min(1.0, du / dist))
    a = math.acos(cos_a)
    l_bb = sum(coeff * a**i for i, coeff in enumerate(_DUV_COEFFS))
    return dist - l_bb


def xy_to_cct_mccamy(x: float, y: float) -> float | None:
    """McCamy 三次多项式近似求相关色温 (CCT)。

    适用范围约 2856K–6500K, 误差 < 2K; 超出后误差增大, 但对判读纸白、
    校准目标已经足够。真正严格的做法是 Robertson 等温线插值 ——
    若将来需要更宽范围再替换。

    Returns:
        CCT(K); 当色度点非法或落在极端位置导致公式发散时返回 None。
    """
    # 物理有效性: 色度坐标必须落在 x>0, y>0, x+y<=1 的三角形内。
    # 少了这道检查, 像 (0,0) 这种退化输入也会算出一个看似合理的数值,
    # 界面上就成了凭空捏造的色温。
    if x <= 0.0 or y <= 0.0 or (x + y) > 1.0:
        return None

    denom = 0.1858 - y
    if abs(denom) < 1e-9:
        return None
    n = (x - 0.3320) / denom
    cct = 449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33
    if not (1000.0 <= cct <= 25000.0):
        return None

    # 离黑体轨迹太远时, 相关色温不再有物理意义
    duv = xy_to_duv(x, y)
    if duv is None or abs(duv) > DUV_LIMIT:
        return None
    return cct


def xyz_to_cct(xyz: tuple[float, float, float]) -> float | None:
    x, y = xyz_to_xy(xyz)
    return xy_to_cct_mccamy(x, y)


def xyz_to_duv(xyz: tuple[float, float, float]) -> float | None:
    x, y = xyz_to_xy(xyz)
    if x <= 0.0 or y <= 0.0 or (x + y) > 1.0:
        return None
    return xy_to_duv(x, y)


# --------------------------------------------------------------------------
# 色差
# --------------------------------------------------------------------------


def delta_e_76(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIE76 色差 —— 就是 Lab 空间里的欧氏距离。

    最简单, 但在饱和色区与人眼感知偏离较大。保留它主要是为了与老数据对齐。
    """
    return math.sqrt(sum((lab1[i] - lab2[i]) ** 2 for i in range(3)))


def delta_e_94(
    lab_ref: tuple[float, float, float],
    lab_sample: tuple[float, float, float],
    *,
    graphics: bool = True,
) -> float:
    """CIE94 色差。

    Args:
        graphics: True 用图文场景参数(kL=1, K1=0.045, K2=0.015),
            False 用纺织场景参数(kL=2, K1=0.048, K2=0.014)。
    """
    l1, a1, b1 = lab_ref
    l2, a2, b2 = lab_sample
    kl, k1, k2 = (1.0, 0.045, 0.015) if graphics else (2.0, 0.048, 0.014)

    dl = l1 - l2
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    dc = c1 - c2
    da = a1 - a2
    db = b1 - b2
    dh_sq = da**2 + db**2 - dc**2
    dh = math.sqrt(dh_sq) if dh_sq > 0 else 0.0

    sl = 1.0
    sc = 1.0 + k1 * c1
    sh = 1.0 + k2 * c1

    return math.sqrt((dl / (kl * sl)) ** 2 + (dc / sc) ** 2 + (dh / sh) ** 2)


def delta_e_2000(
    lab_ref: tuple[float, float, float],
    lab_sample: tuple[float, float, float],
    *,
    kl: float = 1.0,
    kc: float = 1.0,
    kh: float = 1.0,
) -> float:
    """CIEDE2000 色差 —— 印刷与包装行业的现行标准 (ISO 13655 / G7)。

    公式本身有若干易错点, 已逐一标注。实现以 Sharma, Wu & Dalal (2005)
    的论文为准, 并用其配套的 34 组测试向量验证(见 tests/test_colorimetry.py) ——
    这类公式"看起来对"和"真的对"之间差得很远, 必须用标准向量卡死。
    """
    l1, a1, b1 = lab_ref
    l2, a2, b2 = lab_sample

    # --- 1. C' 与 h' ---
    c1 = math.hypot(a1, b1)
    c2 = math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2.0

    # G 因子: 对低彩度区的 a* 做补偿, 修正 CIE94 在近中性色的失真
    c_bar7 = c_bar**7
    g = 0.5 * (1.0 - math.sqrt(c_bar7 / (c_bar7 + 25.0**7)))

    a1p = (1.0 + g) * a1
    a2p = (1.0 + g) * a2
    c1p = math.hypot(a1p, b1)
    c2p = math.hypot(a2p, b2)

    # 易错点一: 当 a'=b'=0 时 h' 定义为 0, 而非 atan2 的返回值
    h1p = 0.0 if (a1p == 0.0 and b1 == 0.0) else math.degrees(math.atan2(b1, a1p)) % 360.0
    h2p = 0.0 if (a2p == 0.0 and b2 == 0.0) else math.degrees(math.atan2(b2, a2p)) % 360.0

    # --- 2. 差值 ---
    dlp = l2 - l1
    dcp = c2p - c1p

    # 易错点二: 任一 C' 为 0 时色相差为 0; 否则要把差值折回 [-180, 180]
    if c1p * c2p == 0.0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180.0:
            dhp -= 360.0
        elif dhp < -180.0:
            dhp += 360.0
    dhp_big = 2.0 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2.0))

    # --- 3. 均值 ---
    lp_bar = (l1 + l2) / 2.0
    cp_bar = (c1p + c2p) / 2.0

    # 易错点三: 平均色相要处理跨 0°/360° 的环绕, 且 C'=0 时直接取和
    if c1p * c2p == 0.0:
        hp_bar = h1p + h2p
    else:
        h_diff = abs(h1p - h2p)
        h_sum = h1p + h2p
        if h_diff <= 180.0:
            hp_bar = h_sum / 2.0
        elif h_sum < 360.0:
            hp_bar = (h_sum + 360.0) / 2.0
        else:
            hp_bar = (h_sum - 360.0) / 2.0

    # --- 4. 加权函数 ---
    t = (
        1.0
        - 0.17 * math.cos(math.radians(hp_bar - 30.0))
        + 0.24 * math.cos(math.radians(2.0 * hp_bar))
        + 0.32 * math.cos(math.radians(3.0 * hp_bar + 6.0))
        - 0.20 * math.cos(math.radians(4.0 * hp_bar - 63.0))
    )

    d_theta = 30.0 * math.exp(-(((hp_bar - 275.0) / 25.0) ** 2))
    cp_bar7 = cp_bar**7
    rc = 2.0 * math.sqrt(cp_bar7 / (cp_bar7 + 25.0**7))
    rt = -rc * math.sin(math.radians(2.0 * d_theta))

    lp_bar_m50_sq = (lp_bar - 50.0) ** 2
    sl = 1.0 + (0.015 * lp_bar_m50_sq) / math.sqrt(20.0 + lp_bar_m50_sq)
    sc = 1.0 + 0.045 * cp_bar
    sh = 1.0 + 0.015 * cp_bar * t

    # --- 5. 合成 ---
    term_l = dlp / (kl * sl)
    term_c = dcp / (kc * sc)
    term_h = dhp_big / (kh * sh)
    return math.sqrt(term_l**2 + term_c**2 + term_h**2 + rt * term_c * term_h)


def delta_e_all(
    lab_ref: tuple[float, float, float], lab_sample: tuple[float, float, float]
) -> dict[str, float]:
    """一次算齐三种色差, 供界面并列展示。"""
    return {
        "de76": delta_e_76(lab_ref, lab_sample),
        "de94": delta_e_94(lab_ref, lab_sample),
        "de2000": delta_e_2000(lab_ref, lab_sample),
    }


# --------------------------------------------------------------------------
# 面向界面的汇总
# --------------------------------------------------------------------------


def describe(
    xyz: tuple[float, float, float],
    illuminant: str = "D50",
) -> dict[str, object]:
    """把一组 XYZ 展开成界面需要的全部派生量。

    这样前端拿到的就是可以直接渲染的数据, 不必在 JS 里重写一遍色度学 ——
    浮点细节在两处实现同步演化, 迟早会对不上。
    """
    white = WHITE_POINTS.get(illuminant, WHITE_POINTS["D50"])
    lab = xyz_to_lab(xyz, white)
    lch = xyz_to_lch(xyz, white)
    x, y = xyz_to_xy(xyz)
    r, g, b, in_gamut = xyz_to_srgb(xyz, white)

    return {
        "xyz": list(xyz),
        "lab": list(lab),
        "lch": list(lch),
        "xy": [x, y],
        "srgb": [r, g, b],
        "hex": srgb_to_hex((r, g, b)),
        "in_gamut": in_gamut,
        "cct": xyz_to_cct(xyz),
        "duv": xyz_to_duv(xyz),
        "illuminant": illuminant,
    }
