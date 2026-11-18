"""Validate and stage screenshot uploads for product teardown."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import stat
import zipfile
import zlib
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

MIB = 1024 * 1024
MAX_FILES = 50
MAX_FILE_BYTES = 10 * MIB
MAX_UPLOAD_BYTES = 100 * MIB
MAX_ZIP_BYTES = 100 * MIB
MAX_ZIP_EXPANDED_BYTES = 200 * MIB
MAX_ZIP_IMAGES = 100
MAX_REQUEST_BYTES = 101 * MIB
MAX_LEGACY_REQUEST_BYTES = 4 * ((MAX_UPLOAD_BYTES + 2) // 3) + MIB
MAX_PIXELS = 32_000_000
IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "BMP", "DIB", "TIFF", "ICO", "AVIF", "HEIF", "JPEG2000"}
IMAGE_EXTENSIONS = {
    ".png", ".apng", ".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".gif", ".bmp", ".dib",
    ".tif", ".tiff", ".ico", ".avif", ".heic", ".heif", ".jp2", ".j2k",
}
register_heif_opener(decode_threads=1)


class TeardownError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def parse_upload(stream, content_type: str, length: int, directory: Path) -> tuple[dict, list[tuple[str, str, Path]]]:
    """Stream the multipart body to temporary files, preserving file order."""
    if not 0 < length <= MAX_REQUEST_BYTES:
        raise TeardownError("上传请求超过大小限制或缺少文件。", 413 if length > MAX_REQUEST_BYTES else 400)
    _, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")
    if not boundary or len(boundary) > 70:
        raise TeardownError("上传请求格式无效。")
    files = []
    metadata = None
    current = None
    field_name = b""
    filename = ""
    total = 0
    finished = False

    with ExitStack() as resources:
        headers = {}
        header_name = bytearray()
        header_value = bytearray()
        header_size = 0

        def begin():
            nonlocal header_size
            headers.clear()
            header_name.clear()
            header_value.clear()
            header_size = 0

        def header_data(target, data, start, end):
            nonlocal header_size
            header_size += end - start
            if header_size > 8192:
                raise TeardownError("上传字段头过长。")
            target.extend(data[start:end])

        def header_end():
            headers[bytes(header_name).lower()] = bytes(header_value)
            header_name.clear()
            header_value.clear()

        def headers_finished():
            nonlocal current, field_name, filename, size, limit, current_path
            disposition, fields = parse_options_header(headers.get(b"content-disposition"))
            field_name = fields.get(b"name")
            if disposition != b"form-data":
                raise TeardownError("上传字段格式无效。")
            size = 0
            if field_name == b"metadata" and b"filename" not in fields and metadata is None:
                limit = 8192
                current = io.BytesIO()
                return
            if field_name not in (b"screenshots", b"archive") or b"filename" not in fields:
                raise TeardownError("上传字段格式无效。")
            filename = fields[b"filename"].decode("utf-8", errors="replace")
            if files and (field_name == b"archive" or files[0][0] == "archive"):
                raise TeardownError("请选择单独上传图片或上传一个 ZIP。")
            if len(files) >= MAX_FILES:
                raise TeardownError("单独上传最多支持 50 张截图。")
            limit = MAX_ZIP_BYTES if field_name == b"archive" else MAX_FILE_BYTES
            current_path = directory / f"upload-{len(files)}"
            current = resources.enter_context(current_path.open("wb"))

        size = 0
        limit = 0
        current_path = directory

        def part_data(data, start, end):
            nonlocal size, total
            count = end - start
            size += count
            if size > limit:
                if field_name == b"archive":
                    raise TeardownError("ZIP 压缩包不能超过 100 MiB。", 413)
                if field_name == b"screenshots":
                    raise TeardownError("单张截图不能超过 10 MiB。", 413)
                raise TeardownError("上传字段过长。")
            if field_name != b"metadata":
                total += count
                if total > MAX_UPLOAD_BYTES:
                    raise TeardownError("上传文件总大小不能超过 100 MiB。", 413)
            current.write(memoryview(data)[start:end])

        def part_end():
            nonlocal metadata
            if field_name == b"metadata":
                metadata = json.loads(current.getvalue().decode("utf-8"))
                if not isinstance(metadata, dict):
                    raise TeardownError("上传参数格式无效。")
            else:
                files.append((field_name.decode("ascii"), filename, current_path))
            current.close()

        def end():
            nonlocal finished
            finished = True

        parser = MultipartParser(boundary, {
            "on_part_begin": begin,
            "on_header_field": lambda *args: header_data(header_name, *args),
            "on_header_value": lambda *args: header_data(header_value, *args),
            "on_header_end": header_end,
            "on_headers_finished": headers_finished,
            "on_part_data": part_data,
            "on_part_end": part_end,
            "on_end": end,
        })
        try:
            remaining = length
            while remaining:
                chunk = stream.read(min(64 * 1024, remaining))
                if not chunk:
                    raise TeardownError("上传提前结束，请重新选择文件。")
                parser.write(chunk)
                remaining -= len(chunk)
            parser.finalize()
        except (MultipartParseError, UnicodeError, json.JSONDecodeError) as exc:
            raise TeardownError("无法读取上传的文件。") from exc
    if not finished or metadata is None or not files:
        raise TeardownError("请提交有效的上传信息和文件。")
    return metadata, files


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise TeardownError(f"{label}文件名无效，最多支持 200 字。")
    return value.strip()


def _legacy_images(files: object):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise TeardownError("请上传 1 至 50 张截图。")
    for file in files:
        if not isinstance(file, dict) or not isinstance(file.get("data"), str):
            raise TeardownError("截图数据格式无效。")
        name = _name(file.get("name"), "截图")
        if len(file["data"]) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise TeardownError(f"截图超过 10 MiB：{name}", 413)
        try:
            raw = base64.b64decode(file["data"], validate=True)
        except ValueError as exc:
            raise TeardownError(f"无法读取截图：{name}") from exc
        yield name, raw


def _natural_key(value: str):
    return [int(part) if index % 2 else part for index, part in enumerate(re.split(r"(\d+)", value.casefold(), flags=re.ASCII))]


def _zip_images(source: Path):
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(entry.file_size for entry in entries) > MAX_ZIP_EXPANDED_BYTES:
                raise TeardownError("ZIP 解压后不能超过 200 MiB 或 1000 个条目。", 413)
            images = []
            for entry in entries:
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", str(path)) or stat.S_ISLNK(entry.external_attr >> 16):
                    raise TeardownError("ZIP 中存在无效的文件路径。")
                if entry.is_dir() or "__MACOSX" in path.parts or any(part.startswith(".") and part != ".." for part in path.parts):
                    continue
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                name = _name(str(path), "截图")
                if entry.flag_bits & 1:
                    raise TeardownError("请上传未加密的 ZIP。")
                if entry.file_size > MAX_FILE_BYTES:
                    raise TeardownError(f"截图超过 10 MiB：{name}", 413)
                images.append((name, entry))
            if not 1 <= len(images) <= MAX_ZIP_IMAGES:
                raise TeardownError("ZIP 中应包含 1 至 100 张受支持的图片。")
            for name, entry in sorted(images, key=lambda item: _natural_key(item[0])):
                with archive.open(entry) as member:
                    yield name, member.read(MAX_FILE_BYTES + 1)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError, EOFError, zlib.error) as exc:
        raise TeardownError("无法解压 ZIP，请检查文件是否完整并重新上传。") from exc


def _screen_data(name: str, raw: bytes) -> tuple[str, str, bytes]:
    if len(raw) > MAX_FILE_BYTES:
        raise TeardownError(f"截图超过 10 MiB：{name}", 413)
    try:
        with Image.open(io.BytesIO(raw), formats=sorted(IMAGE_FORMATS)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise TeardownError(f"截图超过 3200 万像素：{name}")
            if image.format in ("PNG", "JPEG") and not getattr(image, "is_animated", False):
                image.verify()
                with Image.open(io.BytesIO(raw)) as verified:
                    verified.load()
                    return ("png", "image/png", raw) if verified.format == "PNG" else ("jpg", "image/jpeg", raw)
            preview = ImageOps.exif_transpose(image)
            preview.thumbnail((2048, 2048))
            data = io.BytesIO()
            preview.convert("RGBA").save(data, "PNG")
            return "png", "image/png", data.getvalue()
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise TeardownError(f"截图已损坏或格式不支持：{name}。") from exc


def stage_upload(body: dict, uploads: list[tuple[str, str, Path]] | None, staging: Path) -> tuple[list[dict], dict | None]:
    """Create browser-readable evidence and retain the original ZIP, if present."""
    upload_archive = None
    if uploads is None:
        source = _legacy_images(body.get("screenshots"))
    elif len(uploads) == 1 and uploads[0][0] == "archive":
        _, filename, path = uploads[0]
        name = _name(filename, "压缩包")
        size = path.stat().st_size
        if not name.lower().endswith(".zip"):
            raise TeardownError("请选择 ZIP 压缩包。")
        if size > MAX_ZIP_BYTES:
            raise TeardownError("ZIP 压缩包不能超过 100 MiB。", 413)
        source = _zip_images(path)
        upload_archive = {"name": name, "size": size}
    elif 1 <= len(uploads) <= MAX_FILES and all(kind == "screenshots" for kind, _, _ in uploads):
        source = ((_name(name, "截图"), path.read_bytes()) for _, name, path in uploads)
    else:
        raise TeardownError("请选择 1 至 50 张截图或一个 ZIP。")

    folder = staging / "screenshots"
    folder.mkdir()
    screenshots, hashes = [], set()
    total = 0
    for name, raw in source:
        total += len(raw)
        if total > (MAX_ZIP_EXPANDED_BYTES if upload_archive else MAX_UPLOAD_BYTES):
            raise TeardownError("截图总大小超出限制。", 413)
        digest = hashlib.sha256(raw).hexdigest()
        if digest in hashes:
            if upload_archive:
                continue
            raise TeardownError(f"发现重复截图：{name}。请移除后再提交。")
        hashes.add(digest)
        suffix, media_type, data = _screen_data(name, raw)
        key = f"S{len(screenshots) + 1:02d}"
        filename = f"{key}.{suffix}"
        (folder / filename).write_bytes(data)
        screenshots.append({
            "id": key, "name": name, "file": filename, "media_type": media_type,
            "sha256": hashlib.sha256(data).hexdigest() if data is not raw else digest,
        })
    if upload_archive:
        shutil.copyfile(path, staging / "source.zip")
    return screenshots, upload_archive
