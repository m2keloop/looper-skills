#!/usr/bin/env python3
"""为一轮照片风格转换创建不可覆盖、带编号的输出目录。"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tif", ".tiff", ".avif"}
PRIVATE_COLLECTION_TYPE = "photo-style-batch-private"
PRIVATE_RUN_LABEL = "私密转换"


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def safe_label(value: str) -> str:
    label = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", value, flags=re.UNICODE)
    label = re.sub(r"-{2,}", "-", label).strip("-._")
    return (label or "风格转换")[:60]


def next_index(output_parent: Path) -> int:
    indices = []
    for child in output_parent.iterdir() if output_parent.exists() else []:
        match = re.match(r"^(\d+)-", child.name)
        if child.is_dir() and match:
            indices.append(int(match.group(1)))
    return max(indices, default=0) + 1


def suggested_output_filenames(images: list[Path]) -> dict[str, str]:
    stem_counts: dict[str, int] = {}
    for image in images:
        stem_counts[image.stem.casefold()] = stem_counts.get(image.stem.casefold(), 0) + 1
    return {
        image.name: (
            f"{image.stem}.png"
            if stem_counts[image.stem.casefold()] == 1
            else f"{image.stem}--{image.suffix[1:].casefold()}.png"
        )
        for image in images
    }


def private_suggested_output_filenames(images: list[Path]) -> list[str]:
    return [f"image-{index:03d}.png" for index, _ in enumerate(images, start=1)]


def write_restricted_text(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(text)
    path.chmod(0o600)


def write_restricted_json(path: Path, value: object) -> None:
    write_restricted_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--style-name", default="风格转换")
    parser.add_argument("--output-parent", type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--privacy-mode", action="store_true")
    args = parser.parse_args()

    source_argument = args.source_dir.expanduser()
    if source_argument.is_symlink():
        parser.error("原图文件夹不能是符号链接")
    source_dir = source_argument.resolve()
    if not source_dir.is_dir():
        parser.error(f"原图文件夹不存在：{source_dir}")

    output_parent_argument = (
        args.output_parent or source_dir.parent / f"{source_dir.name}-风格转换"
    ).expanduser()
    if output_parent_argument.is_symlink():
        parser.error("风格结果父文件夹不能是符号链接")
    output_parent = output_parent_argument.resolve()
    if output_parent == source_dir or source_dir in output_parent.parents:
        parser.error("风格结果父文件夹不能是原图文件夹本身，也不能位于原图文件夹内")

    prompt_file = args.prompt_file.expanduser()
    if prompt_file.is_symlink():
        parser.error("提示词文件不能是符号链接")
    try:
        prompt_text = prompt_file.read_text(encoding="utf-8")
    except OSError as error:
        parser.error(f"无法读取提示词文件：{error}")
    if not prompt_text or not prompt_text.strip():
        parser.error("提示词不能为空")

    images = sorted(
        (
            item
            for item in source_dir.iterdir()
            if not item.is_symlink() and item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES
        ),
        key=natural_key,
    )
    if not images:
        parser.error(f"原图文件夹第一层没有找到受支持的图片：{source_dir}")

    output_parent.mkdir(parents=True, exist_ok=True)
    if args.privacy_mode:
        output_parent.chmod(0o700)
    collection_path = output_parent / "collection.json"
    if collection_path.is_symlink():
        parser.error("集合配置文件不能是符号链接")
    if collection_path.exists():
        try:
            collection = json.loads(collection_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            parser.error(f"集合配置文件无效 {collection_path}：{error}")
        existing_private_mode = collection.get("privacy_mode") is True
        if existing_private_mode != args.privacy_mode:
            parser.error("隐私归档与普通归档不能共用同一个风格结果父文件夹")
        if args.privacy_mode:
            if collection.get("collection_type") != PRIVATE_COLLECTION_TYPE:
                parser.error("隐私归档集合配置无效，请选择新的结果父文件夹")
        else:
            recorded_source = collection.get("source_folder")
            if recorded_source != str(source_dir):
                parser.error(
                    "这个风格结果父文件夹已经属于另一套原图，请选择单独的结果父文件夹"
                )
        collection_path.chmod(0o600)
    else:
        if args.privacy_mode and any(output_parent.iterdir()):
            parser.error("隐私归档必须使用空的、新建的风格结果父文件夹")
        if args.privacy_mode:
            collection = {
                "collection_type": PRIVATE_COLLECTION_TYPE,
                "privacy_mode": True,
                "source_image_count": len(images),
            }
        else:
            collection = {
                "collection_type": "photo-style-batch",
                "source_folder": str(source_dir),
                "output_parent": str(output_parent),
                "established_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "baseline_source_images": [item.name for item in images],
            }
        write_restricted_json(collection_path, collection)

    label = PRIVATE_RUN_LABEL if args.privacy_mode else safe_label(args.style_name)
    index = next_index(output_parent)
    run_dir = output_parent / f"{index:02d}-{label}"
    while run_dir.exists():
        index += 1
        run_dir = output_parent / f"{index:02d}-{label}"
    run_dir.mkdir(mode=0o700)
    run_dir.chmod(0o700)

    if args.privacy_mode:
        write_restricted_text(run_dir / "Prompt.txt", "已启用隐私归档模式；完整提示词未保存。\n")
        manifest = {
            "manifest_type": "private-style-conversion",
            "privacy_mode": True,
            "source_image_count": len(images),
            "suggested_output_filenames": private_suggested_output_filenames(images),
        }
    else:
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        write_restricted_text(
            run_dir / "Prompt.txt",
            f"[原图文件夹]\n{source_dir}\n\n[风格名称]\n{args.style_name}\n\n[创建时间]\n{created_at}\n\n[提示词]\n{prompt_text.rstrip()}\n",
        )
        manifest = {
            "source_folder": str(source_dir),
            "output_parent": str(output_parent),
            "run_folder": str(run_dir),
            "style_label": args.style_name,
            "created_at": created_at,
            "source_images": [item.name for item in images],
            "suggested_output_filenames": suggested_output_filenames(images),
        }
    write_restricted_json(run_dir / "run-manifest.json", manifest)
    if args.privacy_mode:
        print("已创建私密归档目录")
    else:
        print(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
