# -*- coding: utf-8 -*-
"""重新生成品牌图片资产：大熊AI测试平台（替换旧 FullScopeTest 位图）"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter

PUB = r"E:\FullScopeTest\web\public"
PRIMARY = (12, 86, 77)        # #0C564D 深青绿（旧 logo 主色）
PRIMARY_LIGHT = (95, 165, 155)  # #5FA59B
WHITE = (255, 255, 255)
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
FONT = r"C:\Windows\Fonts\msyh.ttc"


def font(path, size):
    return ImageFont.truetype(path, size)


def lerp(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def gradient(size, top, bottom):
    w, h = size
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(top, bottom, y / max(1, h - 1)))
    return im


def draw_bear(draw, cx, cy, r, body=PRIMARY, inner=(232, 244, 241)):
    """扁平熊头：双耳 + 脸 + 内耳 + 眼睛 + 鼻口"""
    # 双耳
    ear = r * 0.52
    for sx in (-1, 1):
        ex, ey = cx + sx * r * 0.78, cy - r * 0.72
        draw.ellipse([ex - ear, ey - ear, ex + ear, ey + ear], fill=body)
        ir = ear * 0.55
        draw.ellipse([ex - ir, ey - ir, ex + ir, ey + ir], fill=inner)
    # 脸
    draw.ellipse([cx - r, cy - r * 0.88, cx + r, cy + r * 0.88], fill=body)
    # 眼睛
    er = r * 0.10
    for sx in (-1, 1):
        ex, ey = cx + sx * r * 0.36, cy - r * 0.18
        draw.ellipse([ex - er, ey - er * 1.2, ex + er, ey + er * 1.2], fill=inner)
    # 鼻口
    mw, mh = r * 0.42, r * 0.30
    draw.ellipse([cx - mw, cy + r * 0.08, cx + mw, cy + r * 0.08 + mh * 2], fill=inner)
    nr = r * 0.11
    ny = cy + r * 0.22
    draw.ellipse([cx - nr, ny - nr, cx + nr, ny + nr], fill=body)


def make_logo_full():
    W, H = 1007, 248
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    draw_bear(d, 130, 124, 92)
    d.text((258, 52), "大熊AI测试平台", font=font(FONT_BOLD, 96), fill=PRIMARY)
    d.text((262, 178), "AI-DRIVEN  FULL-STACK  TESTING  PLATFORM",
           font=font(FONT, 26), fill=(120, 144, 140))
    im.save(f"{PUB}/logo-full.webp", "WEBP", quality=95)
    im.save(f"{PUB}/logo-full.png")


def make_logo_icon():
    W, H = 707, 353
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    draw_bear(d, W // 2, H // 2, 138)
    im.save(f"{PUB}/logo-icon.webp", "WEBP", quality=95)
    im.save(f"{PUB}/logo-icon.png")


def make_favicon():
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    draw_bear(d, S // 2, S // 2 + 8, 96)
    # 圆角底
    mask = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([4, 4, S - 4, S - 4], radius=48, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    out.save(f"{PUB}/favicon.png")


def make_login_left():
    W, H = 945, 928
    im = gradient((W, H), (18, 104, 93), (7, 58, 52)).convert("RGBA")
    d = ImageDraw.Draw(im, "RGBA")
    # 装饰圆环
    for r, alpha in ((430, 22), (330, 30), (230, 40)):
        d.ellipse([W // 2 - r, H // 2 - r - 60, W // 2 + r, H // 2 + r - 60],
                  outline=(255, 255, 255, alpha), width=3)
    # 中央大白熊
    draw_bear(d, W // 2, H // 2 - 90, 190, body=WHITE, inner=(24, 110, 99))
    # 平台名 + slogan
    d.text((W // 2, H - 190), "大熊AI测试平台", font=font(FONT_BOLD, 84),
           fill=WHITE, anchor="mm")
    d.text((W // 2, H - 96), "让 AI 覆盖测试的每一个角落", font=font(FONT, 34),
           fill=(214, 236, 231), anchor="mm")
    im.convert("RGB").save(f"{PUB}/login-left.webp", "WEBP", quality=88)
    im.convert("RGB").save(f"{PUB}/login-left.png")


make_logo_full()
make_logo_icon()
make_favicon()
make_login_left()
print("brand assets regenerated")
