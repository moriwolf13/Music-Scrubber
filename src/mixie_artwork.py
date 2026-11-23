"""GUI-independent artwork validation, conversion, and immutable review snapshots."""
from io import BytesIO
from pathlib import Path
import hashlib
import os
import stat
import tempfile

from PIL import Image, ImageOps

MAX_BYTES = 20_000_000
MAX_PIXELS = 60_000_000


def normalized_image(data):
    """Return (JPEG/PNG bytes, MIME); convert WebP without touching the original."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ValueError("Artwork must be a nonempty image under 20 MB.")
    with Image.open(BytesIO(data)) as image:
        if image.width < 16 or image.height < 16 or image.width * image.height > MAX_PIXELS:
            raise ValueError("Artwork image dimensions are unsupported.")
        if getattr(image, "n_frames", 1) > 1:
            raise ValueError("Animated artwork is not accepted.")
        if image.format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("Only JPEG, PNG or WebP album artwork is accepted.")
        original_format = image.format
        image.load()
        if original_format != "WEBP":
            return data, "image/jpeg" if original_format == "JPEG" else "image/png"
        # Rockbox uses an ordinary JPEG/PNG; original user-supplied WebP remains intact.
        converted = ImageOps.exif_transpose(image).convert("RGB")
        output = BytesIO()
        converted.save(output, format="JPEG", quality=93, optimize=True)
        result = output.getvalue()
        if len(result) > MAX_BYTES:
            raise ValueError("Converted WebP artwork exceeds 20 MB.")
        return result, "image/jpeg"


def snapshot_artwork(data, *, root):
    """Save a content-addressed review copy, refusing symlinked/altered objects."""
    normalized, mime = normalized_image(data)
    root = Path(root).absolute()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("Unsafe managed artwork folder.")
    root.mkdir(mode=0o700, parents=False, exist_ok=True)
    digest = hashlib.sha256(normalized).hexdigest()
    target = root / (digest + (".png" if mime == "image/png" else ".jpg"))
    if target.is_symlink():
        raise ValueError("Unsafe artwork snapshot link.")
    if target.exists():
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size != len(normalized) or target.read_bytes() != normalized:
            raise ValueError("Managed artwork snapshot has changed.")
    else:
        fd, temporary = tempfile.mkstemp(prefix=".cover-", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(normalized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"kind": "review_artwork_file", "path": str(target),
            "sha256": digest, "mime": mime, "status": "pending_application"}
