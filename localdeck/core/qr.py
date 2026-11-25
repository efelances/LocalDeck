"""Inline SVG QR code generator for LocalDeck pairing."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import qrcode
import qrcode.image.svg


def generate_qr_svg(url: str) -> str:
    """Generate a crisp inline SVG QR code styled in LocalDeck ink (#1C1B19)."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    raw_svg = img.to_string(encoding="unicode")

    try:
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        root = ET.fromstring(raw_svg)
        width_attr = root.attrib.get("width", "240mm")
        height_attr = root.attrib.get("height", "240mm")
        if "viewBox" not in root.attrib:
            vb_w = "".join(ch for ch in width_attr if ch.isdigit() or ch == ".") or "240"
            vb_h = "".join(ch for ch in height_attr if ch.isdigit() or ch == ".") or "240"
            root.attrib["viewBox"] = f"0 0 {vb_w} {vb_h}"
        root.attrib["width"] = "100%"
        root.attrib["height"] = "100%"
        root.attrib["role"] = "img"
        root.attrib["aria-label"] = "Pairing QR Code"
        for elem in root.iter():
            tag = elem.tag.split("}")[-1]
            if tag == "path":
                elem.attrib["fill"] = "#1C1B19"
        return ET.tostring(root, encoding="unicode")
    except ET.ParseError:
        return raw_svg
