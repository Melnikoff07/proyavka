"""Профиль Display P3 (матрица + гамма 2.2) собранный вручную, чтобы проверять перевод в sRGB без внешних файлов."""
import struct


def p3_profile():
    s15 = lambda x: struct.pack(">i", int(round(x * 65536)))
    xyz = lambda x, y, z: b"XYZ \0\0\0\0" + s15(x) + s15(y) + s15(z)
    name = b"Display P3 test\0"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(name)) + name + b"\0" * 8 + b"\0\0" + b"\0" + b"\0" * 67
    curv = b"curv\0\0\0\0" + struct.pack(">I", 1) + struct.pack(">H", 0x0233) + b"\0\0"
    tags = [(b"desc", desc), (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
            (b"rXYZ", xyz(0.5151, 0.2412, -0.0011)), (b"gXYZ", xyz(0.2920, 0.6922, 0.0419)), (b"bXYZ", xyz(0.1571, 0.0666, 0.7841)),
            (b"rTRC", curv), (b"gTRC", curv), (b"bTRC", curv), (b"cprt", b"text\0\0\0\0test\0")]
    head_len = 128 + 4 + 12 * len(tags)
    body, table, off = b"", b"", head_len
    for sig, data in tags:
        data += b"\0" * (-len(data) % 4)
        table += sig + struct.pack(">II", off, len(data))
        body += data
        off += len(data)
    size = head_len + len(body)
    head = (struct.pack(">I", size) + b"lcms" + struct.pack(">I", 0x02400000) + b"mntrRGB XYZ " + b"\0" * 12 + b"acsp"
            + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 8 + struct.pack(">I", 0)
            + s15(0.9642) + s15(1.0) + s15(0.8249) + b"lcms" + b"\0" * 44)
    return head + struct.pack(">I", len(tags)) + table + body
