"""通用工具层：不感知业务语义，可被任何上层复用。"""

from .filename import build_package_name, resolve_template, sanitize_filename
from .link import host_matches, split_link

__all__ = [
    "build_package_name",
    "host_matches",
    "resolve_template",
    "sanitize_filename",
    "split_link",
]
