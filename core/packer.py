"""统一打包与加密层。

这是本插件的「上层统一流程」里最关键的一段：**任何渠道下载完图片后，都走这里**，
渠道不关心产物是 zip 还是 pdf、要不要密码。加密策略全局一致：

- 设置了密码但缺少加密所需的库时**失败关闭**，绝不静默产出未加密的包（用户以为加密了才危险）
- 未设置密码时不加任何加密，产物照常可用
"""

from __future__ import annotations

import io
import os
import re
import shutil
import zipfile
from pathlib import Path

from astrbot.api import logger

from ..domain import PackResult
from ..domain.constants import (
    IMAGE_SUFFIXES,
    LONG_IMG_MAX_PER_STRIP,
    LONG_IMG_MAX_STRIP_HEIGHT,
    LONG_IMG_WIDTH,
    PLUGIN_NAME,
)

try:  # 加密 ZIP（AES）依赖
    import pyzipper

    PYZIPPER_AVAILABLE = True
except ImportError:  # pragma: no cover - 运行环境决定
    PYZIPPER_AVAILABLE = False

try:  # 长图拼接依赖
    from PIL import Image
    from PIL import Image as PILImage

    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover - 运行环境决定
    PIL_AVAILABLE = False

try:  # PDF 生成与加密依赖（pymupdf）
    import fitz

    FITZ_AVAILABLE = True
except ImportError:  # pragma: no cover - 运行环境决定
    FITZ_AVAILABLE = False


def collect_images(source_dir: Path) -> list[Path]:
    """递归收集图片并按阅读顺序排序。

    多章节本子按「章节目录/页码」落盘，目录名是 1、2、…、10，普通字符串排序会得到
    1,10,11,2,20 的错误顺序，因此数字段按整数比较。
    """

    def natural_key(path: Path) -> list[tuple[int, object]]:
        tokens = re.split(r"(\d+)", str(path.relative_to(source_dir)))
        return [
            (0, int(token)) if token.isdigit() else (1, token.lower())
            for token in tokens
        ]

    files = [
        Path(root) / name
        for root, _dirs, names in os.walk(source_dir)
        for name in names
        if Path(name).suffix.lower() in IMAGE_SUFFIXES
    ]
    files.sort(key=natural_key)
    return files


class Packer:
    """把下载目录打包成用户可收的单文件产物。"""

    def __init__(self, fmt: str, password: str = "") -> None:
        self.fmt = (fmt or "zip").lower()
        self.password = password or ""

    def pack(self, source_dir: Path, base_name: str, output_dir: Path) -> PackResult:
        if not source_dir.exists():
            return self._fail(f"下载目录不存在：{source_dir}")
        if not collect_images(source_dir):
            return self._fail("没有可打包的图片，可能全部下载失败")

        if self.fmt == "none":
            # 仅保留本地目录，调用方不发送
            return PackResult(
                success=True, output_path=source_dir, fmt="none", encrypted=False
            )
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            if self.fmt == "zip":
                return self._pack_zip(source_dir, base_name, output_dir)
            if self.fmt == "pdf":
                return self._pack_pdf(source_dir, base_name, output_dir)
            if self.fmt == "long_img":
                return self._pack_long_img(source_dir, base_name, output_dir)
        except Exception as exc:
            # 没有 traceback 就分不清是坏图、依赖缺失还是磁盘问题，排障只能靠猜
            logger.error(
                f"[{PLUGIN_NAME}] ❌ 打包异常（{self.fmt}）："
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            raise
        return self._fail(f"不支持的打包格式：{self.fmt}")

    # ---- 各格式实现 ----
    def _pack_zip(self, source_dir: Path, base_name: str, output_dir: Path) -> PackResult:
        images = collect_images(source_dir)
        out_path = output_dir / f"{base_name}.zip"

        if self.password:
            if not PYZIPPER_AVAILABLE:
                return self._fail(
                    "已设置打包密码，但未安装 pyzipper，无法生成加密压缩包；"
                    "请安装 pyzipper 后重试，或清空密码"
                )
            with pyzipper.AESZipFile(
                out_path,
                "w",
                compression=pyzipper.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as zf:
                zf.setpassword(self.password.encode("utf-8"))
                for image in images:
                    zf.write(image, image.relative_to(source_dir))
            return PackResult(True, out_path, "zip", True)

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for image in images:
                zf.write(image, image.relative_to(source_dir))
        return PackResult(True, out_path, "zip", False)

    def _pack_pdf(self, source_dir: Path, base_name: str, output_dir: Path) -> PackResult:
        if not FITZ_AVAILABLE:
            return self._fail("PDF 打包需要 pymupdf，请安装后重试，或改用 zip 格式")

        images = collect_images(source_dir)
        out_path = output_dir / f"{base_name}.pdf"
        # 先写 .part 再原子改名：中途失败也不会在目标位置留下半截 PDF 被当成成品发出去
        part_path = output_dir / f"{base_name}.pdf.part"
        doc = fitz.open()
        skipped: list[str] = []
        try:
            # 让 pymupdf 自己把图片转成单页 PDF 再合并，省掉 PIL 依赖与手工尺寸换算
            for image in images:
                try:
                    page_pdf = self._image_to_pdf_bytes(image)
                    with fitz.open("pdf", page_pdf) as page_doc:
                        doc.insert_pdf(page_doc)
                except Exception as exc:
                    # 单张坏图不该让整本报废：跳过这一页并记账，结果里如实告诉用户缺了哪几页
                    skipped.append(image.name)
                    logger.warning(
                        f"[{PLUGIN_NAME}] ⚠️ PDF 跳过无法解码的图片 {image.name}："
                        f"{type(exc).__name__}: {exc}",
                        exc_info=True,
                    )

            if skipped and len(skipped) == len(images):
                return self._fail("PDF 打包失败：所有图片都无法解码")

            self._save_pdf(doc, part_path)
            part_path.replace(out_path)
        except Exception as exc:
            # 合并/落盘阶段的异常跟具体哪张图无关，别再把它说成「图片 X 无法解码」
            logger.error(
                f"[{PLUGIN_NAME}] ❌ PDF 打包失败（合并或落盘阶段）："
                f"{type(exc).__name__}: {exc}",
                exc_info=True,
            )
            return self._fail(f"PDF 打包失败：{type(exc).__name__}: {exc}")
        finally:
            doc.close()
            self._drop_partial(part_path)

        note = ""
        if skipped:
            shown = "、".join(skipped[:3])
            tail = f" 等 {len(skipped)} 页" if len(skipped) > 3 else ""
            note = f"{len(skipped)} 页图片无法解码已跳过（{shown}{tail}），PDF 缺这几页"
        return PackResult(True, out_path, "pdf", bool(self.password), note=note)

    def _save_pdf(self, doc: fitz.Document, path: Path) -> None:
        """保存 PDF，系统级失败重试一次。

        pymupdf 在 I/O 或内存吃紧时会抛 `FzErrorSystem`，而 2.x 构造这个异常自身有 bug，
        用户侧只剩一句 `missing 1 required positional argument`；重试一次能把瞬时故障吃掉，
        仍然失败就把异常类型一并交给上层记进日志。
        """
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if self.password:
                    doc.save(
                        str(path),
                        encryption=fitz.PDF_ENCRYPT_AES_256,
                        owner_pw=self.password,
                        user_pw=self.password,
                    )
                else:
                    doc.save(str(path))
                return
            except Exception as exc:
                last_error = exc
                self._drop_partial(path)
                logger.warning(
                    f"[{PLUGIN_NAME}] ⚠️ 写入 PDF 失败（第 {attempt + 1}/2 次）："
                    f"{type(exc).__name__}: {exc}"
                )
        assert last_error is not None
        raise last_error

    @staticmethod
    def _drop_partial(path: Path) -> None:
        """删掉写了一半的产物；路径本身异常（比如被目录占着）不能盖住真正的失败原因。"""
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] ⚠️ 清理半成品失败：{path}：{exc}")

    def _image_to_pdf_bytes(self, image: Path) -> bytes:
        """把一张图片转成单页 PDF 的字节流。

        pymupdf 对 webp/avif 这类格式的支持取决于编译进版本里的解码器，缺解码器时
        `fitz.open` 会直接抛 `FileDataError`（整本打包失败）。这里退回 Pillow：先转成
        JPEG 再交给 pymupdf，代价是多一次内存里的转码。
        """
        try:
            with fitz.open(str(image)) as image_doc:
                return image_doc.convert_to_pdf()
        except Exception:
            # 这里必须宽捕获：pymupdf 2.x 的解码失败抛 `FzErrorFormat`（既不继承 RuntimeError
            # 也不继承 OSError），漏掉就会让整本打包直接崩掉；抓宽了才轮得到 Pillow 兜底
            if not PIL_AVAILABLE:
                raise
            buffer = io.BytesIO()
            with PILImage.open(image) as raw:
                raw.convert("RGB").save(buffer, format="JPEG", quality=92)
            with fitz.open(stream=buffer.getvalue(), filetype="jpg") as image_doc:
                return image_doc.convert_to_pdf()

    def _pack_long_img(
        self, source_dir: Path, base_name: str, output_dir: Path
    ) -> PackResult:
        if not PIL_AVAILABLE:
            return self._fail("长图打包需要 Pillow，请安装后重试，或改用 zip 格式")

        strips = self._render_long_strips(collect_images(source_dir), output_dir, base_name)
        if not strips:
            return self._fail("长图渲染失败：没有生成任何图片")

        # 单张长图且不加密码：直接发这张图，不必再套一层压缩包
        if len(strips) == 1 and not self.password:
            return PackResult(True, strips[0], "long_img", False)

        if self.password and not PYZIPPER_AVAILABLE:
            return self._fail(
                "已设置打包密码，但未安装 pyzipper，无法生成加密压缩包；"
                "请安装 pyzipper 后重试，或清空密码"
            )

        out_path = output_dir / f"{base_name}_long.zip"
        if self.password:
            with pyzipper.AESZipFile(
                out_path,
                "w",
                compression=pyzipper.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as zf:
                zf.setpassword(self.password.encode("utf-8"))
                for strip in strips:
                    zf.write(strip, strip.name)
        else:
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for strip in strips:
                    zf.write(strip, strip.name)

        self._drop_strips(strips)
        return PackResult(True, out_path, "long_img", bool(self.password))

    @staticmethod
    def _drop_strips(strips: list[Path]) -> None:
        """分段长图已经打进入口压缩包，中间产物及时清掉，避免占满磁盘。"""
        for strip in strips:
            try:
                strip.unlink(missing_ok=True)
            except OSError:
                pass

    def _render_long_strips(
        self, images: list[Path], output_dir: Path, base_name: str
    ) -> list[Path]:
        """按宽度统一缩放后纵向拼接，超长时分段输出，避免单图超出图片解码上限。"""
        strips: list[Path] = []
        batch: list[Image.Image] = []
        batch_height = 0
        index = 1

        def flush() -> None:
            nonlocal batch, batch_height, index
            if not batch:
                return
            strip_path = output_dir / f"{base_name}_part{index:02d}.jpg"
            canvas = Image.new("RGB", (LONG_IMG_WIDTH, batch_height), "white")
            offset = 0
            for frame in batch:
                canvas.paste(frame, (0, offset))
                offset += frame.height
            canvas.save(strip_path, "JPEG", quality=90)
            canvas.close()
            for frame in batch:
                frame.close()
            strips.append(strip_path)
            batch = []
            batch_height = 0
            index += 1

        for image_path in images:
            with Image.open(image_path) as probe:
                source = probe.convert("RGB")
            scale = LONG_IMG_WIDTH / source.width
            resized = source.resize(
                (LONG_IMG_WIDTH, max(1, int(source.height * scale)))
            )
            source.close()
            if batch and (
                batch_height + resized.height > LONG_IMG_MAX_STRIP_HEIGHT
                or len(batch) >= LONG_IMG_MAX_PER_STRIP
            ):
                flush()
            batch.append(resized)
            batch_height += resized.height

        flush()
        return strips

    # ---- 清理与错误 ----
    @staticmethod
    def cleanup(path: Path | None) -> None:
        """尽力删除文件或目录；失败只当缓存残留，不影响主流程。"""
        if path is None:
            return
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _fail(message: str) -> PackResult:
        logger.error(f"[{PLUGIN_NAME}] ❌ 打包失败：{message}")
        return PackResult(
            success=False, output_path=None, fmt="", encrypted=False, error=message
        )
