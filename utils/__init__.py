"""通用工具层：不感知业务语义，可被任何上层复用。"""

from .filename import build_package_name, resolve_template, sanitize_filename
from .files import find_existing_image, write_image_atomically
from .format import format_size
from .link import host_matches, split_link
from .pack_format import describe_pack_format, split_pack_format

__all__ = [
    "build_package_name",
    "describe_pack_format",
    "find_existing_image",
    "format_size",
    "host_matches",
    "resolve_template",
    "sanitize_filename",
    "split_link",
    "split_pack_format",
    "write_image_atomically",
]
