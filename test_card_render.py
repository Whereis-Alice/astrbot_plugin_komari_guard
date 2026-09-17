"""Regression tests for transparent card-image cropping."""

from __future__ import annotations

import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from card_render import EmptyAlphaBoundsError, crop_to_alpha_bounds


def _encode(image: Image.Image, image_format: str = "PNG") -> bytes:
    with BytesIO() as output:
        image.save(output, format=image_format)
        return output.getvalue()


class CropToAlphaBoundsTests(unittest.TestCase):
    def test_crops_bytes_to_nonzero_alpha_bounds(self) -> None:
        image = Image.new("RGBA", (12, 10), (0, 0, 0, 0))
        image.paste((12, 34, 56, 255), (3, 2, 9, 8))
        image.putpixel((8, 7), (90, 80, 70, 1))

        result = crop_to_alpha_bounds(_encode(image))

        with Image.open(BytesIO(result)) as cropped:
            self.assertEqual(cropped.format, "PNG")
            self.assertEqual(cropped.mode, "RGBA")
            self.assertEqual(cropped.size, (6, 6))
            self.assertEqual(cropped.getpixel((0, 0)), (12, 34, 56, 255))
            self.assertEqual(cropped.getpixel((5, 5)), (90, 80, 70, 1))

    def test_decodes_file_by_content_when_suffix_is_jpeg(self) -> None:
        image = Image.new("RGBA", (9, 7), (0, 0, 0, 0))
        image.paste((20, 40, 60, 255), (1, 3, 8, 6))

        with tempfile.TemporaryDirectory() as temp_dir:
            rendered_path = Path(temp_dir) / "astrbot-render.jpg"
            image.save(rendered_path, format="PNG")

            result = crop_to_alpha_bounds(rendered_path)

        with Image.open(BytesIO(result)) as cropped:
            self.assertEqual(cropped.format, "PNG")
            self.assertEqual(cropped.size, (7, 3))

    def test_preserves_fully_opaque_images(self) -> None:
        image = Image.new("RGB", (4, 3), (5, 10, 15))

        result = crop_to_alpha_bounds(_encode(image))

        with Image.open(BytesIO(result)) as cropped:
            self.assertEqual(cropped.mode, "RGBA")
            self.assertEqual(cropped.size, image.size)
            self.assertEqual(cropped.getpixel((3, 2)), (5, 10, 15, 255))

    def test_rejects_fully_transparent_image_even_with_hidden_rgb(self) -> None:
        image = Image.new("RGBA", (5, 4), (255, 0, 255, 0))

        with self.assertRaisesRegex(EmptyAlphaBoundsError, "fully transparent"):
            crop_to_alpha_bounds(_encode(image))

    def test_invalid_image_bytes_remain_a_decode_error(self) -> None:
        with self.assertRaises(UnidentifiedImageError):
            crop_to_alpha_bounds(b"not an image")


if __name__ == "__main__":
    unittest.main()
