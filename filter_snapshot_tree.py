#!/usr/bin/env python3
"""从 dropbox_snapshot.py 生成的 JSON 中按路径前缀筛选条目，输出为易读的目录树文本。

基本用法：

1. 修改本文件开头“可配置参数”区域中的 PREFIXES（以及其他可选参数）。
2. 运行：
       python filter_snapshot_tree.py --in <快照 JSON> --out <输出 txt>

   --in   必填。dropbox_snapshot.py 生成的 JSON 文件。
   --out  必填。输出的 UTF-8 纯文本文件；已存在时会被覆盖，但不允许与 --in 是同一个文件。

版本号：

- 本脚本版本号 = 所支持的 dropbox_snapshot.py 版本号 + 一个字母修订号。
  例如 1.2.3a 表示：专门解析 dropbox_snapshot.py v1.2.3 生成的 JSON，
  是对应这一版的第一个筛选脚本；只修改本脚本时依次改为 1.2.3b、1.2.3c……
  dropbox_snapshot.py 升级后，确认 JSON 格式仍兼容，再把本脚本改为新版本号 + a。
- 运行时会核对 JSON 中 info.version 记录的生成脚本版本：一致时提示受支持；
  不一致时给出警告但仍继续处理，结果需要自行确认。
- 查看版本：python filter_snapshot_tree.py --version

本脚本只使用 Python 标准库（3.9+），不需要 Dropbox 凭据，也不访问网络；
只读取输入 JSON，不会修改它。

前缀匹配规则：

- 以“一层目录名”为单位逐段比较，而不是比较字符串开头。前缀和条目路径都按“/”
  切成若干段，只有条目路径的前 N 段与前缀的 N 段逐一相等时才算匹配。
  以 PREFIXES = ["/A/B"] 为例：
      /A/B               匹配（前缀本身）
      /A/B/c.txt         匹配
      /A/B/C/D/e.txt     匹配（任意深度的子孙）
      /A/BBB             不匹配（第二段是“BBB”，不是“B”）
      /A/BBB/c.txt       不匹配
      /A/B.txt           不匹配（第二段是“B.txt”）
      /A                 不匹配（这是前缀的上级目录）
- 不区分大小写：Dropbox 本身不区分大小写（同一目录下不可能同时存在 B 和 b），
  所以 "/a/b" 与 "/A/B" 等价。比较前还会做 Unicode 规范化，避免 macOS 上
  日文浊音（如“が”）等字符因组合形式不同而匹配失败。
  这只影响“是否匹配”，输出时仍保留 JSON 中的原有大小写（见下方输出格式）。
- 写法宽松：开头的“/”可以省略，结尾多余的“/”和中间重复的“/”会被忽略，
  "A/B"、"/A/B/"、"/A//B" 都等价于 "/A/B"。
- "/" 表示根目录，即匹配全部条目。
- 可以写多个前缀，满足任意一个即被选中；每个前缀单独输出一棵树。若某个前缀
  位于另一个前缀之内（如 "/A" 与 "/A/B"），只保留外层的 "/A"，避免重复输出。
- 前缀指向一个文件时，只输出这个文件本身。
- 没有匹配到任何条目的前缀会在终端给出警告，并在输出文件中注明。

输出格式（类似 tree 命令）：

    （开头几行：脚本版本、来源 JSON、快照扫描时间、筛选前缀）

    /A/B
    ├── 子文件夹/
    │   ├── file2.txt
    │   └── file10.txt
    ├── 空文件夹/
    └── c.txt

    2 个文件夹，3 个文件

- 每个前缀一棵树：根节点是该前缀在 Dropbox 中的完整路径，下面各层只显示名称。
- 大小写保持 JSON 中的原样，不会转成小写：显示的名称取自 JSON 记录的 path 字段
  （保留大小写），path_lower 只用于匹配和归并同一目录。即使前缀写成 "/a/b"，
  根节点也显示 JSON 中的 "/A/B"。每个名称取自该条目自身 path 的最后一段，
  因为 Dropbox 只保证这一段的大小写准确，上级目录的大小写在个别条目里可能不一致。
- 文件夹名后加“/”（见 DIR_SUFFIX），以便与文件区分；空文件夹也会显示。
- 同一层按名称排序，不区分大小写，数字按数值大小排列（file2 在 file10 之前）；
  DIRS_FIRST 为 True 时文件夹排在文件前面。
- 统计行不计入根节点本身（与 tree 命令一致）。
- 输出内容只由输入 JSON 和配置决定，同样的输入重复运行得到完全相同的文件。

用法示例：

    # 1. 先用本项目生成快照（默认写到 dropbox_snapshot.py 同目录下）
    python dropbox_snapshot.py

    # 2. 把 PREFIXES 改为 ["/Photos/2024"]，然后筛选
    python filter_snapshot_tree.py --in dropbox_snapshot.json --out photos_2024.txt

    # 输入输出也可以是任意位置的文件
    python filter_snapshot_tree.py --in ~/backup/snapshot.json --out ~/Desktop/tree.txt

退出码：0 成功；2 参数或配置错误；3 输入 JSON 格式不符；4 读写文件失败。
"""

from __future__ import annotations

# ============================== 可配置参数 ==============================

# 要筛选的路径前缀，可写多个；匹配规则见文件开头说明。匹配全部请写 "/"。
PREFIXES: list[str] = [
    # "/A/B",
    "/Apps/ZhonghuiPlayer",
]

# True：同一层中文件夹排在文件前面；False：文件夹与文件混合按名称排序。
DIRS_FIRST = True

# 追加在文件夹名后面的标记，便于与文件区分；设为 "" 则不加。
DIR_SUFFIX = "/"

# True：用纯 ASCII 字符画树（|-- 和 `--），适合不能正常显示制表符的环境。
ASCII_TREE = False

# ======================================================================

import argparse
import json
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 本脚本支持解析的 dropbox_snapshot.py 版本（即其 JSON 中的 info.version）。
SUPPORTED_GENERATOR_VERSION = "1.2.3"
# 本脚本版本 = 所支持的生成脚本版本 + 字母修订号；只改本脚本时递增字母。
VERSION = f"{SUPPORTED_GENERATOR_VERSION}a"
EXPECTED_GENERATOR = "dropbox-snapshot"


class ConfigurationError(RuntimeError):
    """Raised when arguments or the configured prefixes are invalid."""


class InputFormatError(RuntimeError):
    """Raised when the input is not a JSON document from dropbox_snapshot.py."""


@dataclass(frozen=True)
class Prefix:
    text: str
    key: tuple[str, ...]

    def contains(self, key: tuple[str, ...]) -> bool:
        """Return True when ``key`` is this prefix itself or lies beneath it."""

        return key[: len(self.key)] == self.key


@dataclass
class Node:
    name: str
    is_dir: bool = True
    children: dict[str, Node] = field(default_factory=dict)


@dataclass
class Section:
    prefix: Prefix
    root: Node | None = None


@dataclass(frozen=True)
class TreeChars:
    tee: str
    last: str
    pipe: str
    blank: str


UNICODE_CHARS = TreeChars(tee="├── ", last="└── ", pipe="│   ", blank="    ")
ASCII_CHARS = TreeChars(tee="|-- ", last="`-- ", pipe="|   ", blank="    ")


def warn(message: str) -> None:
    print(f"警告：{message}", file=sys.stderr)


def split_path(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def match_key(segment: str) -> str:
    """Canonical caseless form, so that case and NFC/NFD differences compare equal."""

    return unicodedata.normalize(
        "NFC", unicodedata.normalize("NFD", segment).casefold()
    )


def parse_prefixes(raw_prefixes: list[str]) -> list[Prefix]:
    if not raw_prefixes:
        raise ConfigurationError('PREFIXES 为空；请至少配置一个前缀（匹配全部请写 "/"）。')

    kept: list[Prefix] = []
    for raw in raw_prefixes:
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigurationError(f'无效的前缀 {raw!r}；匹配全部请写 "/"。')
        segments = split_path(raw)
        prefix = Prefix(
            text="/" + "/".join(segments),
            key=tuple(match_key(s) for s in segments),
        )

        outer = next((k for k in kept if k.contains(prefix.key)), None)
        if outer is not None:
            warn(f"前缀 {prefix.text!r} 已被 {outer.text!r} 包含，忽略。")
            continue
        for inner in [k for k in kept if prefix.contains(k.key)]:
            warn(f"前缀 {inner.text!r} 已被 {prefix.text!r} 包含，忽略。")
            kept.remove(inner)
        kept.append(prefix)

    return kept


def generator_version(info: dict[str, Any]) -> str:
    version = info.get("version")
    return f"v{version}" if isinstance(version, str) and version else "版本未知"


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InputFormatError(f"无法解析 JSON：{error}") from error

    if not isinstance(data, dict):
        raise InputFormatError("JSON 顶层不是对象。")
    for kind in ("folders", "files"):
        if not isinstance(data.get(kind), list):
            raise InputFormatError(f"JSON 中缺少 {kind!r} 列表。")

    info = data.get("info")
    if not isinstance(info, dict) or info.get("generator") != EXPECTED_GENERATOR:
        warn("输入 JSON 似乎不是 dropbox_snapshot.py 生成的，结果可能不正确。")
    elif info.get("version") != SUPPORTED_GENERATOR_VERSION:
        warn(
            f"输入 JSON 由 dropbox_snapshot.py {generator_version(info)} 生成，"
            f"本脚本 v{VERSION} 只支持 v{SUPPORTED_GENERATOR_VERSION}，"
            "结果可能不正确。"
        )
    else:
        print(
            f"输入 JSON 由 dropbox_snapshot.py v{SUPPORTED_GENERATOR_VERSION} 生成，"
            "版本受支持。",
            file=sys.stderr,
        )
    return data


def record_paths(record: Any, kind: str, index: int) -> tuple[list[str], list[str]]:
    """Return (display segments, lower-case segments) of one JSON record."""

    if not isinstance(record, dict):
        raise InputFormatError(f"{kind}[{index}] 不是对象。")
    path = record.get("path")
    path_lower = record.get("path_lower")
    if not isinstance(path, str) or not isinstance(path_lower, str):
        raise InputFormatError(f"{kind}[{index}] 缺少字符串字段 path 或 path_lower。")

    display = split_path(path)
    lower = split_path(path_lower)
    if not lower or len(display) != len(lower):
        raise InputFormatError(
            f"{kind}[{index}] 的 path 与 path_lower 层级不一致：{path!r}"
        )
    return display, lower


def add_entry(
    section: Section, display: list[str], lower: list[str], is_dir: bool
) -> None:
    depth = len(section.prefix.key)

    if section.root is None:
        # Placeholder label; replaced below once the prefix's own record is seen.
        section.root = Node(name="/" + "/".join(display[:depth]))

    if len(lower) == depth:
        section.root.name = "/" + "/".join(display)
        section.root.is_dir = is_dir
        return

    # Children are keyed by path_lower: Dropbox may report inconsistent casing for
    # ancestor components of path_display, but path_lower is canonical.
    node = section.root
    for low, name in zip(lower[depth:], display[depth:]):
        child = node.children.get(low)
        if child is None:
            child = Node(name=name)
            node.children[low] = child
        node = child

    # Dropbox guarantees the casing of the last component of an entry's own path.
    node.name = display[-1]
    node.is_dir = is_dir


def build_sections(data: dict[str, Any], prefixes: list[Prefix]) -> list[Section]:
    sections = [Section(prefix) for prefix in prefixes]

    for kind, is_dir in (("folders", True), ("files", False)):
        for index, record in enumerate(data[kind]):
            display, lower = record_paths(record, kind, index)
            key = tuple(match_key(s) for s in lower)
            for section in sections:
                if section.prefix.contains(key):
                    add_entry(section, display, lower, is_dir)
                    break

    return sections


def natural_key(name: str) -> list[Any]:
    parts = re.split(r"(\d+)", match_key(name))
    return [int(part) if i % 2 else part for i, part in enumerate(parts)]


def sorted_children(node: Node) -> list[Node]:
    return sorted(
        node.children.values(),
        key=lambda child: (
            DIRS_FIRST and not child.is_dir,
            natural_key(child.name),
            child.name,
        ),
    )


def label(node: Node) -> str:
    return node.name + (DIR_SUFFIX if node.is_dir else "")


def render_tree(root: Node, chars: TreeChars) -> tuple[list[str], int, int]:
    """Render like ``tree``; return (lines, folder count, file count)."""

    lines = [root.name]
    folder_count = 0
    file_count = 0 if root.is_dir else 1

    # Iterative depth-first walk, so very deep folder trees cannot hit the
    # interpreter's recursion limit.
    stack: list[tuple[list[Node], int, str]] = [(sorted_children(root), 0, "")]
    while stack:
        siblings, index, indent = stack.pop()
        if index >= len(siblings):
            continue
        node = siblings[index]
        is_last = index == len(siblings) - 1
        stack.append((siblings, index + 1, indent))

        lines.append(indent + (chars.last if is_last else chars.tee) + label(node))
        if node.is_dir:
            folder_count += 1
        else:
            file_count += 1

        if node.children:
            child_indent = indent + (chars.blank if is_last else chars.pipe)
            stack.append((sorted_children(node), 0, child_indent))

    return lines, folder_count, file_count


def render_document(
    data: dict[str, Any], input_path: Path, sections: list[Section]
) -> str:
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    started = info.get("scan_started_at_utc", "未知")
    completed = info.get("scan_completed_at_utc", "未知")
    chars = ASCII_CHARS if ASCII_TREE else UNICODE_CHARS

    lines = [
        "Dropbox 快照筛选结果",
        f"筛选脚本：filter_snapshot_tree.py v{VERSION}",
        f"来源 JSON：{input_path.name}"
        f"（dropbox_snapshot.py {generator_version(info)}）",
        f"快照扫描时间（UTC）：{started} ～ {completed}",
        "筛选前缀：" + "、".join(section.prefix.text for section in sections),
        "匹配规则：按目录层级逐段匹配，不区分大小写",
    ]

    total_folders = 0
    total_files = 0
    for section in sections:
        lines.append("")
        if section.root is None:
            lines.append(section.prefix.text)
            lines.append("（没有匹配的条目）")
            warn(f"前缀 {section.prefix.text!r} 没有匹配到任何条目。")
            continue

        tree_lines, folders, files = render_tree(section.root, chars)
        lines.extend(tree_lines)
        lines.append("")
        lines.append(f"{folders} 个文件夹，{files} 个文件")
        total_folders += folders
        total_files += files
        print(
            f"{section.prefix.text}：{folders} 个文件夹，{files} 个文件",
            file=sys.stderr,
        )

    if len(sections) > 1:
        lines.append("")
        lines.append(f"合计：{total_folders} 个文件夹，{total_files} 个文件")

    return "\n".join(lines) + "\n"


def write_text(path: Path, text: str) -> None:
    """Write atomically, so an existing output is never left half-written."""

    temporary_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary_path = Path(temporary_file.name)
    try:
        with temporary_file as stream:
            stream.write(text)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def check_paths(input_path: Path, output_path: Path) -> None:
    if not input_path.is_file():
        raise ConfigurationError(f"输入文件不存在：{input_path}")
    if not output_path.parent.is_dir():
        raise ConfigurationError(f"输出目录不存在：{output_path.parent}")
    if output_path.is_dir():
        raise ConfigurationError(f"输出路径是目录：{output_path}")
    if output_path.exists() and os.path.samefile(input_path, output_path):
        raise ConfigurationError("--out 与 --in 是同一个文件，拒绝覆盖输入 JSON。")


def run(input_path: Path, output_path: Path, raw_prefixes: list[str]) -> None:
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    check_paths(input_path, output_path)
    prefixes = parse_prefixes(raw_prefixes)

    print(f"读取：{input_path}", file=sys.stderr)
    data = load_snapshot(input_path)
    sections = build_sections(data, prefixes)
    text = render_document(data, input_path, sections)
    write_text(output_path, text)
    print(f"已写入：{output_path}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "从 dropbox_snapshot.py 生成的 JSON 中按路径前缀（PREFIXES，在脚本开头配置）"
            "筛选文件和文件夹，以目录树形式输出到文本文件。"
        )
    )
    parser.add_argument(
        "--in",
        dest="input",
        type=Path,
        required=True,
        metavar="JSON",
        help="dropbox_snapshot.py 生成的 JSON 文件",
    )
    parser.add_argument(
        "--out",
        dest="output",
        type=Path,
        required=True,
        metavar="TXT",
        help="输出的文本文件（已存在则覆盖）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(
        f"Filter Snapshot Tree v{VERSION}：开始按前缀筛选快照 JSON。",
        file=sys.stderr,
    )
    try:
        run(args.input, args.output, PREFIXES)
        return 0
    except KeyboardInterrupt:
        print("\n已中止。", file=sys.stderr)
        return 130
    except ConfigurationError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    except InputFormatError as error:
        print(f"输入格式错误：{error}", file=sys.stderr)
        return 3
    except OSError as error:
        print(f"读写文件失败：{error}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
