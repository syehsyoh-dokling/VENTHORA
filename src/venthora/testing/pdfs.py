# ruff: noqa: E501
"""A minimal text-based PDF (one page, Helvetica), like an invoice exported from an accounting system.
Used by the tests and the demo cases; no PDF library is needed to write one.

``images`` places raster images on the page, the way a scanned stamp or signature is pasted into an invoice:
each {"x", "y", "w_mm", "h_mm", "shape": "stamp" | "signature" | "logo", "ink": (r, g, b)}. ``info`` sets the
document information dictionary (CreationDate, ModDate, Producer ...), as an exporting or editing tool would."""
from __future__ import annotations

import math
import zlib

PT_PER_MM = 72 / 25.4


def _raster(shape: str, ink: tuple[int, int, int], px: int = 96) -> tuple[int, int, bytes]:
    """RGB pixels: a stamp is an ink ring with a dot centre, a signature a wavy stroke, a logo a filled block."""
    w, h = (px, px) if shape == "stamp" else (px * 3, px) if shape == "signature" else (px, px // 2)
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            if shape == "stamp":
                r = math.hypot(x - w / 2, y - h / 2)
                on = w * 0.38 <= r <= w * 0.46 or r <= w * 0.08
            elif shape == "signature":
                on = abs(y - (h / 2 + h * 0.3 * math.sin(x / 9))) <= 2 and w * 0.05 < x < w * 0.95
            else:
                on = True
            row += bytes(ink) if on else b"\xff\xff\xff"
        rows.append(bytes(row))
    return w, h, b"".join(rows)


def make_pdf(lines: list[str], images: list[dict] | None = None, info: dict[str, str] | None = None) -> bytes:
    esc = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines]
    stream = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({t}) Tj T*" for t in esc) + " ET"
    images = images or []
    for i, im in enumerate(images):
        w, h = im["w_mm"] * PT_PER_MM, im["h_mm"] * PT_PER_MM
        stream += f" q {w:.2f} 0 0 {h:.2f} {im.get('x', 380)} {im.get('y', 120)} cm /Im{i} Do Q"
    xobjs = " ".join(f"/Im{i} {6 + i} 0 R" for i in range(len(images)))
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >>"
            + (f" /XObject << {xobjs} >>" if images else "") + " >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    binary: dict[int, bytes] = {}
    for im in images:
        pw, ph, pix = _raster(im.get("shape", "stamp"), tuple(im.get("ink", (30, 60, 170))))
        data = zlib.compress(pix)
        binary[len(objs)] = data
        objs.append(f"<< /Type /XObject /Subtype /Image /Width {pw} /Height {ph} /ColorSpace /DeviceRGB /BitsPerComponent 8"
                    f" /Filter /FlateDecode /Length {len(data)} >>")
    if info:
        esc_info = " ".join(f"/{k} ({v})" for k, v in info.items())
        objs.append(f"<< {esc_info} >>")
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        if i - 1 in binary:
            out += f"{i} 0 obj\n{body}\nstream\n".encode("latin-1") + binary[i - 1] + b"\nendstream\nendobj\n"
        else:
            out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    info_ref = f" /Info {len(objs)} 0 R" if info else ""
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R{info_ref} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out
