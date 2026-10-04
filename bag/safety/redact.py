"""Redaction: keeping sensitive data out of logs, recordings, prompts and screenshots.

Three things are scrubbed:

    secrets          real passwords and user names are replaced by {{secret:NAME}}
    account numbers  long digit strings keep only their last 4 digits
    screenshots      given regions of the image are pixelated and blurred

This module must not import bag.surface at the top: the surface imports this module.
"""

import re
from io import BytesIO
from math import ceil, floor

from PIL import Image, ImageFilter

# A run of 8-19 digits, or groups of 3+ digits joined by spaces or dashes (9990-0000-1001,
# 1234 5678 9012 3456). Not matched: amounts (12345678.90), and digits stuck to letters or
# hyphenated words, such as the timestamp inside a file name.
_ACCOUNT = re.compile(r"(?<![\w.\-])(?:\d{8,19}|\d{3,}(?:[ \-]\d{3,})+)(?![\w\-]|\.\d)")


def _digit_count(text: str) -> int:
    return sum(char.isdigit() for char in text)


def _mask(match: re.Match) -> str:
    text = match.group(0)
    if not 8 <= _digit_count(text) <= 19:
        return text  # too short or too long to be an account number
    keep, masked = 4, []
    for char in reversed(text):  # walk from the right, keeping the last 4 digits
        if char.isdigit():
            masked.append(char if keep > 0 else "X")
            keep -= 1
        else:
            masked.append(char)  # keep spaces and dashes so the shape stays readable
    return "".join(reversed(masked))


def mask_account_numbers(text: str) -> str:
    """9990-0000-1001 becomes XXXX-XXXX-1001. Safe to run twice."""
    return _ACCOUNT.sub(_mask, text)


def has_account_number(text: str) -> bool:
    return any(8 <= _digit_count(m.group(0)) <= 19 for m in _ACCOUNT.finditer(text))


# ------------------------------------------------------------------ screenshots


def _as_box(box) -> tuple[float, float, float, float]:
    if isinstance(box, dict):
        return box["x"], box["y"], box["width"], box["height"]
    x, y, width, height = box
    return x, y, width, height


def blur_boxes(png: bytes, boxes, radius: float = 6, padding: int = 3) -> bytes:
    """Obscure rectangles of a PNG screenshot. Each box is (x, y, width, height) in pixels.

    The area is pixelated first and blurred after: a plain blur on small text can sometimes be
    reversed, a mosaic cannot. Boxes outside the image are clipped. With no boxes the original
    bytes come back untouched.
    """
    boxes = [_as_box(box) for box in boxes]
    if not boxes:
        return png
    image = Image.open(BytesIO(png)).convert("RGB")
    image_width, image_height = image.size
    for x, y, width, height in boxes:
        left, top = max(0, floor(x) - padding), max(0, floor(y) - padding)
        right, bottom = min(image_width, ceil(x + width) + padding), min(image_height, ceil(y + height) + padding)
        if right <= left or bottom <= top:
            continue  # completely outside the screenshot
        region = image.crop((left, top, right, bottom))
        small = region.resize((max(1, (right - left) // 8), max(1, (bottom - top) // 8)), Image.BILINEAR)
        mosaic = small.resize(region.size, Image.NEAREST).filter(ImageFilter.GaussianBlur(radius))
        image.paste(mosaic, (left, top))
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# --------------------------------------------------------------------- Redactor


class Redactor:
    """Everything that scrubs, in one place, so every log line goes through the same rules.

    `values` is the bag.surface Values holding the real secrets (anything with a
    .redact(text) method will do). Without it, secrets are read from the environment.
    """

    def __init__(self, values=None):
        self._values = values

    @property
    def values(self):
        if self._values is None:
            from bag.surface.placeholders import Values  # imported late: bag.surface imports this module

            self._values = Values()
        return self._values

    def text(self, text: str) -> str:
        """Secrets out, account numbers masked."""
        return mask_account_numbers(self.values.redact(str(text)))

    def data(self, obj):
        """The same, applied to every string inside nested dicts and lists."""
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {key: self.data(value) for key, value in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.data(item) for item in obj]
        return obj

    def png(self, png: bytes, boxes) -> bytes:
        return blur_boxes(png, boxes)
