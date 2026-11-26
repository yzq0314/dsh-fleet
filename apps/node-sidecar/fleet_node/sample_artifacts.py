"""Small deterministic report fixtures for an explicitly requested Mock artifact."""

import struct
import zlib


TITLES = {
    "txt": "任务摘要.txt",
    "md": "节点检查报告.md",
    "csv": "设备检查明细.csv",
    "png": "检查项统计.png",
    "svg": "检查结果趋势.svg",
}


def chart_png():
    width, height = 160, 64

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    rows = bytearray()
    levels = (19, 42, 31, 48, 39)
    for y in range(height):
        rows.append(0)
        for x in range(width):
            bar = (
                14 <= x < 146
                and (x - 14) % 27 < 17
                and y >= 55 - levels[min((x - 14) // 27, 4)]
                and y < 55
            )
            rows.extend((107, 218, 174) if bar else (21, 36, 34))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def samples(prompt):
    return {
        "txt": ("Mock result\n" + prompt).encode(),
        "md": "# 节点检查报告\n\n本次演示完成以下检查。\n\n- 读取工作区\n- 生成统计表\n\n**结论**：三个检查项均通过。\n".encode(),
        "csv": '节点,检查项,状态\nAtlas,"读写,预览",通过\nBirch,NAS,通过\nCedar,任务日志,通过\n'.encode(),
        "png": chart_png(),
        "svg": b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 64"><rect width="160" height="64" fill="#152422"/><polyline points="5,46 40,31 75,37 110,15 155,20" fill="none" stroke="#6bdaae" stroke-width="4"/></svg>',
    }
