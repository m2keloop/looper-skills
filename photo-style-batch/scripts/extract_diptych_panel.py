#!/usr/bin/env python3
"""从布局一致的双画面图片中确定性裁切上半部或下半部。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff", ".avif"}


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def lexical_absolute_path(path: Path) -> Path:
    """返回不解析符号链接的绝对路径。"""
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def first_symbolic_link(path: Path) -> Path | None:
    """查找路径中第一个已存在的符号链接组件。"""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            break
        if stat.S_ISLNK(mode):
            return current
    return None


def image_size(path: Path, magick_path: str) -> tuple[int, int]:
    """读取自动校正方向后的尺寸，使其与实际裁切坐标保持一致。"""
    result = subprocess.run(
        [magick_path, str(path), "-auto-orient", "-format", "%w %h", "info:"],
        text=True,
        capture_output=True,
        check=True,
    )
    dimensions = result.stdout.strip().split()
    if len(dimensions) != 2:
        raise RuntimeError(f"无法读取图片尺寸：{path}")
    width, height = dimensions
    return int(width), int(height)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--panel", choices=("upper", "lower"), required=True)
    split = parser.add_mutually_exclusive_group()
    split.add_argument("--split-ratio", type=float, default=0.5)
    split.add_argument("--split-y", type=int)
    parser.add_argument(
        "--preserve-metadata",
        action="store_true",
        help="保留图片元数据；默认会在裁切前自动校正方向并移除 EXIF、XMP、IPTC 与缩略图",
    )
    parser.add_argument(
        "--privacy-mode",
        action="store_true",
        help="使用匿名输出文件名，并在裁切清单中省略原始文件名和绝对路径",
    )
    parser.add_argument("--overwrite", action="store_true", help="清理本脚本已有的裁切图片后重建输出目录")
    args = parser.parse_args()

    if args.privacy_mode and args.preserve_metadata:
        parser.error("--privacy-mode 不能与 --preserve-metadata 同时使用")

    magick_binary = shutil.which("magick")
    if magick_binary is None:
        parser.error("确定性裁切需要 ImageMagick 的“magick”命令")
    magick_path = str(Path(magick_binary).resolve())
    if not 0 < args.split_ratio < 1:
        parser.error("--split-ratio 必须大于 0 且小于 1")

    input_dir = lexical_absolute_path(args.input_dir)
    output_dir = lexical_absolute_path(args.output_dir)
    input_link = first_symbolic_link(input_dir)
    if input_link is not None:
        parser.error(f"输入文件夹路径不能包含符号链接：{input_link}")
    output_link = first_symbolic_link(output_dir)
    if output_link is not None:
        parser.error(f"输出文件夹路径不能包含符号链接：{output_link}")
    if not input_dir.is_dir():
        parser.error(f"输入文件夹不存在：{input_dir}")
    if output_dir == input_dir or output_dir in input_dir.parents:
        parser.error("输出文件夹不能是输入文件夹本身，也不能是它的上级文件夹")

    images = []
    for item in input_dir.iterdir():
        if item.suffix.casefold() not in IMAGE_SUFFIXES:
            continue
        if item.is_symlink():
            parser.error(f"拒绝符号链接图片：{item}")
        if item.is_file():
            images.append(item)
    images.sort(key=natural_key)
    if not images:
        parser.error(f"输入文件夹第一层没有找到受支持的图片：{input_dir}")

    destination_names = [
        f"image-{index:03d}{source.suffix.casefold()}" if args.privacy_mode else source.name
        for index, source in enumerate(images, start=1)
    ]
    destinations = [output_dir / name for name in destination_names]
    manifest_path = output_dir / "crop-manifest.json"
    for target in [*destinations, manifest_path]:
        if target.is_symlink():
            parser.error(f"拒绝已有目标符号链接：{target}")
        if target.exists() and not target.is_file():
            parser.error(f"输出目标不是普通文件：{target}")

    output_exists = output_dir.exists()
    if output_exists and not output_dir.is_dir():
        parser.error(f"输出路径不是文件夹：{output_dir}")
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        parser.error(f"输出文件夹非空：{output_dir}；仅在确实要覆盖时使用 --overwrite")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.privacy_mode or not output_exists:
        output_dir.chmod(0o700)
    if args.overwrite:
        for existing in output_dir.iterdir():
            if existing.is_file() and (
                existing.suffix.casefold() in IMAGE_SUFFIXES
                or existing.name == "crop-manifest.json"
            ):
                if existing.is_symlink():
                    parser.error(f"拒绝删除符号链接目标：{existing}")
                existing.unlink()

    crops = []
    for source, destination in zip(images, destinations, strict=True):
        width, height = image_size(source, magick_path)
        split_y = args.split_y if args.split_y is not None else round(height * args.split_ratio)
        if not 1 <= split_y < height:
            parser.error(f"{source.name} 的裁切分隔行无效：{split_y}（图片尺寸为 {width}x{height}）")

        if args.panel == "upper":
            crop_height, offset_y = split_y, 0
        else:
            crop_height, offset_y = height - split_y, split_y
        command = [
            magick_path,
            str(source),
            "-auto-orient",
        ]
        if not args.preserve_metadata:
            command.append("-strip")
        command.extend(
            [
                "-crop",
                f"{width}x{crop_height}+0+{offset_y}",
                "+repage",
            ]
        )
        if source.suffix.casefold() in {".jpg", ".jpeg"}:
            command.extend(["-quality", "100"])
        command.append(str(destination))
        subprocess.run(command, check=True)
        destination.chmod(0o600)

        cropped_width, cropped_height = image_size(destination, magick_path)
        if (cropped_width, cropped_height) != (width, crop_height):
            raise RuntimeError(
                f"{source.name} 裁切校验失败：得到 {cropped_width}x{cropped_height}，"
                f"预期为 {width}x{crop_height}"
            )
        crop = {
            "output": destination.name,
            "source_size": [width, height],
            "split_y": split_y,
            "panel": args.panel,
            "crop_size": [cropped_width, cropped_height],
        }
        if not args.privacy_mode:
            crop["source"] = source.name
        crops.append(crop)

    manifest = {
        "privacy_mode": args.privacy_mode,
        "panel": args.panel,
        "split_ratio": None if args.split_y is not None else args.split_ratio,
        "split_y": args.split_y,
        "crops": crops,
    }
    if not args.privacy_mode:
        manifest["created_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        manifest["input_directory"] = str(input_dir)
        manifest["output_directory"] = str(output_dir)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o600)
    if args.privacy_mode:
        print("已完成私密裁切")
    else:
        print(output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
