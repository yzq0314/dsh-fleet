"""Produce an inert SVG image from a bounded subset of static chart markup."""

import re
import xml.etree.ElementTree as ET

from fastapi import HTTPException

SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG)
SHAPES = {"path", "circle", "ellipse", "rect", "polygon", "polyline", "line"}
TAGS = SHAPES | {
    "svg",
    "g",
    "defs",
    "title",
    "desc",
    "text",
    "tspan",
    "clipPath",
    "use",
    "linearGradient",
    "radialGradient",
    "stop",
}
NUMBERS = set(
    "x y x1 y1 x2 y2 cx cy r rx ry width height dx dy opacity stroke-width fill-opacity stroke-opacity font-size offset stroke-miterlimit stroke-dasharray stroke-dashoffset".split()
)
COLORS = {"fill", "stroke", "color", "stop-color"}
WORDS = {
    "text-anchor",
    "font-weight",
    "font-style",
    "stroke-linecap",
    "stroke-linejoin",
    "fill-rule",
    "dominant-baseline",
    "gradientUnits",
    "clipPathUnits",
}
ID = re.compile(r"[a-zA-Z_][\w.-]{0,100}")
NUM = re.compile(r"[-+\d.eE,%\s]+(?:pt|px|em)?")
PAINT = re.compile(
    r"(?:#[\da-fA-F]{3,8}|[a-zA-Z]{1,24}|rgb\([\d.,%\s]+\)|url\(#[\w.-]+\))"
)


def allowed(key, value):
    if key == "id":
        return ID.fullmatch(value)
    if key in NUMBERS or key in {"viewBox", "points"}:
        return NUM.fullmatch(value)
    if key in COLORS:
        return PAINT.fullmatch(value)
    if key == "clip-path":
        return re.fullmatch(r"url\(#[\w.-]+\)", value)
    if key == "d":
        return re.fullmatch(r"[aAcChHlLmMqQsStTvVzZ\deE+.,\s-]+", value)
    if key in {"transform", "gradientTransform"}:
        return re.fullmatch(
            r"(?:(?:matrix|translate|scale|rotate|skewX|skewY)\([-+.\deE,\s]+\)\s*)+",
            value,
        )
    if key in WORDS:
        return re.fullmatch(r"[a-zA-Z0-9-]{1,30}", value)
    return False


def sanitize_svg(content):
    def bad():
        return HTTPException(415, "该 SVG 超出静态图表预览范围，可下载原文件")

    if len(content) > 512 * 1024:
        raise bad()
    try:
        xml = content.decode("utf-8-sig")
        # Some plotters include this fixed declaration. External DTDs are never parsed.
        xml = re.sub(
            r'<!DOCTYPE\s+svg\s+PUBLIC\s+"-//W3C//DTD SVG 1\.1//EN"\s+"http://www\.w3\.org/Graphics/SVG/1\.1/DTD/svg11\.dtd"\s*>',
            "",
            xml,
            count=1,
        )
        if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise bad()
        root = ET.fromstring(xml)
    except (ValueError, ET.ParseError, UnicodeError) as exc:
        raise bad() from exc
    if root.tag not in {"svg", "{" + SVG + "}svg"}:
        raise bad()
    elements = list(root.iter())
    if len(elements) > 8000:
        raise bad()
    shape_ids = {
        e.get("id") for e in elements if e.tag.removeprefix("{" + SVG + "}") in SHAPES
    }

    def copy(old, depth=0):
        tag = old.tag.removeprefix("{" + SVG + "}")
        if tag not in TAGS or depth > 48:
            return None
        out = ET.Element("{" + SVG + "}" + tag)
        attrs = dict(old.attrib)
        for part in attrs.pop("style", "").split(";"):
            k, _, v = part.partition(":")
            attrs.setdefault(k.strip(), v.strip())
        for key, value in attrs.items():
            if allowed(key, value):
                out.set(key, value)
            elif (
                tag == "use"
                and key in {"href", "{" + XLINK + "}href"}
                and value.startswith("#")
                and value[1:] in shape_ids
            ):
                out.set("href", value)
        if tag in {"title", "desc", "text", "tspan"}:
            out.text = old.text
        for child in old:
            if (new := copy(child, depth + 1)) is not None:
                out.append(new)
                if tag in {"text", "tspan"}:
                    new.tail = child.tail
        return out

    return ET.tostring(copy(root), encoding="utf-8")
