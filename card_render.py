"""Post-process rendered card images into tightly cropped PNG bytes."""

from __future__ import annotations

from io import BytesIO
from os import PathLike

from PIL import Image

ImageSource = str | PathLike[str] | bytes | bytearray | memoryview


class EmptyAlphaBoundsError(ValueError):
    """Raised when a rendered image contains no visible pixels."""


def crop_to_alpha_bounds(source: ImageSource) -> bytes:
    """Crop transparent padding from an image and return RGBA PNG bytes.

    File inputs are decoded from their content rather than their extension. This
    matters for AstrBot 4.16, whose T2I downloader stores PNG responses in a
    temporary file with a ``.jpg`` suffix.
    """

    byte_stream: BytesIO | None = None
    image_source: str | PathLike[str] | BytesIO
    if isinstance(source, (bytes, bytearray, memoryview)):
        byte_stream = BytesIO(bytes(source))
        image_source = byte_stream
    else:
        image_source = source

    try:
        with Image.open(image_source) as opened:
            opened.load()
            rgba = opened.convert("RGBA")
    finally:
        if byte_stream is not None:
            byte_stream.close()

    bounds = rgba.getchannel("A").getbbox()
    if bounds is None:
        raise EmptyAlphaBoundsError("rendered card is fully transparent")

    cropped = rgba.crop(bounds)
    with BytesIO() as output:
        cropped.save(output, format="PNG", optimize=True)
        return output.getvalue()


__all__ = ["EmptyAlphaBoundsError", "ImageSource", "crop_to_alpha_bounds"]
