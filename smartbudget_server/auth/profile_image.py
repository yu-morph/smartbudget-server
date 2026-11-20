"""계정 프로필에 저장할 이미지를 검증하고 PNG로 정규화합니다."""

from base64 import b64decode
from binascii import Error as Base64Error
from io import BytesIO

from PIL import Image, ImageOps

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_BASE64_LENGTH = 4 * ((MAX_IMAGE_BYTES + 2) // 3)
MAX_IMAGE_PIXELS = 16_000_000
PROFILE_IMAGE_SIZE = 500


def prepare_profile_image(value: str) -> bytes:
    """Base64 단일 이미지를 디코딩하고 중앙을 최대 500×500으로 잘라 저장합니다."""
    try:
        raw = b64decode(value, validate=True)
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Invalid profile image size")
        formats = ("JPEG", "PNG", "WEBP", "HEIF")
        with Image.open(BytesIO(raw), formats=formats) as original:
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise ValueError("Too many image pixels")
            if getattr(original, "n_frames", 1) != 1:
                raise ValueError("Animated profile images are unsupported")
            original.verify()
        with Image.open(BytesIO(raw), formats=formats) as original:
            image = ImageOps.exif_transpose(original)
            width, height = image.size
            cropped_width = min(width, PROFILE_IMAGE_SIZE)
            cropped_height = min(height, PROFILE_IMAGE_SIZE)
            left = (width - cropped_width) // 2
            top = (height - cropped_height) // 2
            image = image.crop((left, top, left + cropped_width, top + cropped_height))
            pixels = image.convert("RGBA")
            pixels.info.clear()
            output = BytesIO()
            pixels.save(output, format="PNG")
            return output.getvalue()
    except (Base64Error, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("Invalid profile image") from error
