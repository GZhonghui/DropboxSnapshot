#!/usr/bin/env python3
"""Export all Dropbox file metadata to one deterministic JSON document.

基本用法：

1. Dropbox App 使用 Full Dropbox，授权所需 scope：files.metadata.read。
2. 创建环境（只需一次）：
       conda env create -f environment.yml
3. 激活环境：
       conda activate dropbox-snapshot
4. 设置长期凭据：
       export DROPBOX_APP_KEY='...'
       export DROPBOX_APP_SECRET='...'
       export DROPBOX_REFRESH_TOKEN='...'
   三项均为必填；脚本不尝试其他授权模式。
5. 运行：
       python dropbox_snapshot.py

默认输出：与本脚本同目录的 dropbox_snapshot.json
指定输出：python dropbox_snapshot.py --output /path/to/snapshot.json
查看版本：python dropbox_snapshot.py --version

JSON 结构约定：

- info：全局信息对象，不是 list；记录程序版本、schema 版本、
  扫描时间、请求次数以及文件/文件夹数量等汇总信息。
- folders：文件夹的扁平 list；所有文件夹并列存放，包括空文件夹。
- files：文件的扁平 list；所有文件并列存放。
- folders 和 files 不嵌套保存原始文件树，均按 path_lower 排序。需要树形结构时，
  使用每条记录的 path 按“/”分段重建，不会丢失目录归属。

每个文件记录以下字段：

- id：Dropbox 文件 ID，同一文件移动后通常保持不变。
- path：保留大小写的完整路径。
- path_lower：Dropbox 返回的规范化小写路径，用于稳定排序和匹配；
  不能通过对 path 调用 lower() 可靠重建。
- size：文件大小，单位为字节。
- content_hash：Dropbox content hash；不可下载文件可能为 null。
- rev：Dropbox 文件修订版本号。
- client_modified：客户端修改时间（UTC）。
- server_modified：Dropbox 服务端修改时间（UTC）。

每个文件夹将记录以下字段：

- id：Dropbox 文件夹 ID；用于识别同一文件夹的移动或重命名。
- path：保留大小写的完整路径，也是重建文件树的依据。
- path_lower：Dropbox 返回的规范化小写路径，用于稳定排序和匹配；
  不能通过对 path 调用 lower() 可靠重建。

正确性与时间边界：

- 为获得稳定的全量清单，从程序开始到结束期间，不要新增、修改、删除、
  移动或重命名 Dropbox 中的文件和文件夹；同时避免其他设备、共享成员或
  自动上传程序写入 Dropbox。
- 只有所有 Dropbox 分页都成功处理，且临时数据通过内部一致性检查后，
  程序才会原子替换 JSON；任何错误都会退出并保留原有 JSON。
- Dropbox 的递归分页不提供单一时点的原子快照。JSON 记录的是
  scan_started_at_utc 到 scan_completed_at_utc 扫描窗口内 API 返回并完成对账的结果。
  如果遍历期间条目发生移动，API 可能重复返回，也可能不在本次遍历中返回；
  程序会对已返回的重复 ID 进行对账，但无法弥补 API 未返回的条目。

脚本只读取元数据，不下载或修改 Dropbox 文件；凭据不会写入输出或日志。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import dropbox
import requests
from dropbox import files
from dropbox.exceptions import ApiError, AuthError, DropboxException


VERSION = "1.2.3"
SCHEMA_VERSION = 4
PAGE_LIMIT = 2_000
NETWORK_RETRIES = 5
CURSOR_RESET_RESTARTS = 2
DEFAULT_OUTPUT = Path(__file__).resolve().with_name("dropbox_snapshot.json")

T = TypeVar("T")


class ConfigurationError(RuntimeError):
    """Raised when credentials or local output settings are invalid."""


class CursorResetError(RuntimeError):
    """Raised when Dropbox asks the client to restart a traversal."""


class SnapshotIntegrityError(RuntimeError):
    """Raised when temporary storage or JSON output fails a consistency check."""


@dataclass
class ScanProgress:
    request_count: int = 0
    entry_count: int = 0
    restart_count: int = 0


@dataclass(frozen=True)
class SnapshotSummary:
    file_count: int
    folder_count: int
    total_size: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def dropbox_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def build_client() -> dropbox.Dropbox:
    credentials = {
        "DROPBOX_APP_KEY": os.environ.get("DROPBOX_APP_KEY", "").strip(),
        "DROPBOX_APP_SECRET": os.environ.get("DROPBOX_APP_SECRET", "").strip(),
        "DROPBOX_REFRESH_TOKEN": os.environ.get(
            "DROPBOX_REFRESH_TOKEN", ""
        ).strip(),
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        raise ConfigurationError(
            "未设置以下必需环境变量：" + "、".join(missing) + "。"
        )

    common: dict[str, Any] = {
        "timeout": 120,
        "max_retries_on_error": 5,
        "max_retries_on_rate_limit": 8,
        "user_agent": f"dropbox-snapshot/{VERSION}",
    }

    return dropbox.Dropbox(
        oauth2_refresh_token=credentials["DROPBOX_REFRESH_TOKEN"],
        app_key=credentials["DROPBOX_APP_KEY"],
        app_secret=credentials["DROPBOX_APP_SECRET"],
        **common,
    )


def call_api(operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Retry transport failures; the SDK handles HTTP 429 and 5xx responses."""

    for attempt in range(NETWORK_RETRIES + 1):
        try:
            return operation(*args, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt == NETWORK_RETRIES:
                raise
            delay = min(2**attempt, 30)
            print(
                f"网络连接失败，{delay} 秒后重试（{attempt + 1}/{NETWORK_RETRIES}）……",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise AssertionError("unreachable")


def create_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute("PRAGMA synchronous = FULL")
    connection.executescript(
        """
        CREATE TABLE files (
            path_lower TEXT PRIMARY KEY COLLATE BINARY,
            id TEXT NOT NULL UNIQUE,
            path_display TEXT NOT NULL,
            size INTEGER NOT NULL CHECK (size >= 0),
            content_hash TEXT,
            rev TEXT NOT NULL,
            client_modified TEXT NOT NULL,
            server_modified TEXT NOT NULL
        );

        CREATE TABLE folders (
            id TEXT PRIMARY KEY,
            path_lower TEXT NOT NULL UNIQUE COLLATE BINARY,
            path_display TEXT NOT NULL
        );
        """
    )
    connection.commit()
    return connection


def clear_database(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute("DELETE FROM files")
        connection.execute("DELETE FROM folders")


def require_path(entry: files.Metadata) -> str:
    if not entry.path_lower or not entry.path_display:
        raise ValueError(f"Dropbox 返回了缺少完整路径的条目：{entry.name!r}")
    return entry.path_lower


def delete_subtree(connection: sqlite3.Connection, path_lower: str) -> None:
    """Delete one path and descendants using an index-friendly prefix range."""

    descendant_start = f"{path_lower}/"
    descendant_end = f"{path_lower}0"
    parameters = (path_lower, descendant_start, descendant_end)
    connection.execute(
        """
        DELETE FROM files
        WHERE path_lower = ? OR (path_lower >= ? AND path_lower < ?)
        """,
        parameters,
    )
    connection.execute(
        """
        DELETE FROM folders
        WHERE path_lower = ? OR (path_lower >= ? AND path_lower < ?)
        """,
        parameters,
    )


def apply_file(connection: sqlite3.Connection, entry: files.FileMetadata) -> None:
    path_lower = require_path(entry)

    # The same Dropbox ID may have moved, and a file may replace a folder tree.
    connection.execute("DELETE FROM files WHERE id = ?", (entry.id,))
    delete_subtree(connection, path_lower)
    connection.execute(
        """
        INSERT INTO files (
            path_lower, id, path_display, size, content_hash, rev,
            client_modified, server_modified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            path_lower,
            entry.id,
            entry.path_display,
            entry.size,
            entry.content_hash,
            entry.rev,
            dropbox_datetime(entry.client_modified),
            dropbox_datetime(entry.server_modified),
        ),
    )


def apply_folder(connection: sqlite3.Connection, entry: files.FolderMetadata) -> None:
    path_lower = require_path(entry)
    previous = connection.execute(
        "SELECT path_lower FROM folders WHERE id = ?", (entry.id,)
    ).fetchone()

    if previous and previous[0] != path_lower:
        delete_subtree(connection, previous[0])

    # A folder at this path replaces a file, but retains existing children.
    connection.execute("DELETE FROM files WHERE path_lower = ?", (path_lower,))
    connection.execute(
        """
        INSERT OR REPLACE INTO folders (id, path_lower, path_display)
        VALUES (?, ?, ?)
        """,
        (entry.id, path_lower, entry.path_display),
    )


def apply_entry(connection: sqlite3.Connection, entry: files.Metadata) -> None:
    if isinstance(entry, files.FileMetadata):
        apply_file(connection, entry)
    elif isinstance(entry, files.FolderMetadata):
        apply_folder(connection, entry)
    elif isinstance(entry, files.DeletedMetadata):
        delete_subtree(connection, require_path(entry))
    else:
        raise TypeError(f"无法识别的 Dropbox 元数据类型：{type(entry).__name__}")


def apply_page(connection: sqlite3.Connection, entries: list[files.Metadata]) -> None:
    with connection:
        for entry in entries:
            apply_entry(connection, entry)


def is_cursor_reset(error: ApiError) -> bool:
    predicate = getattr(error.error, "is_reset", None)
    return bool(predicate and predicate())


def request_page(
    progress: ScanProgress,
    endpoint: str,
    operation: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    progress.request_count += 1
    print(
        f"发起列表请求 #{progress.request_count}：{endpoint}",
        file=sys.stderr,
    )
    return call_api(operation, *args, **kwargs)


def log_page(
    progress: ScanProgress,
    page_number: int,
    entries: list[files.Metadata],
    file_count: int,
    folder_count: int,
    has_more: bool,
) -> None:
    page_files = sum(isinstance(entry, files.FileMetadata) for entry in entries)
    page_folders = sum(isinstance(entry, files.FolderMetadata) for entry in entries)
    page_deleted = sum(isinstance(entry, files.DeletedMetadata) for entry in entries)
    progress.entry_count += len(entries)
    print(
        f"列表请求 #{progress.request_count} 完成（本轮第 {page_number} 页）："
        f"本页 {len(entries)} 个条目（文件 {page_files}、目录 {page_folders}、"
        f"删除 {page_deleted}），本轮累计条目 {progress.entry_count}，"
        f"当前文件总数 {file_count}、文件夹总数 {folder_count}，"
        f"has_more={str(has_more).lower()}",
        file=sys.stderr,
    )


def scan_once(
    client: dropbox.Dropbox,
    connection: sqlite3.Connection,
    progress: ScanProgress,
) -> None:
    result = request_page(
        progress,
        "files/list_folder",
        client.files_list_folder,
        path="",
        recursive=True,
        include_deleted=False,
        include_mounted_folders=True,
        include_non_downloadable_files=True,
        limit=PAGE_LIMIT,
    )
    page_number = 1

    while True:
        apply_page(connection, result.entries)
        file_count, folder_count = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM files),
                (SELECT COUNT(*) FROM folders)
            """
        ).fetchone()
        log_page(
            progress,
            page_number,
            result.entries,
            file_count,
            folder_count,
            result.has_more,
        )

        if not result.has_more:
            return

        try:
            result = request_page(
                progress,
                "files/list_folder/continue",
                client.files_list_folder_continue,
                result.cursor,
            )
        except ApiError as error:
            if is_cursor_reset(error):
                raise CursorResetError from error
            raise

        page_number += 1


def scan(client: dropbox.Dropbox, connection: sqlite3.Connection) -> ScanProgress:
    progress = ScanProgress()
    for restart in range(CURSOR_RESET_RESTARTS + 1):
        clear_database(connection)
        progress.entry_count = 0
        progress.restart_count = restart
        try:
            scan_once(client, connection, progress)
            return progress
        except CursorResetError:
            if restart == CURSOR_RESET_RESTARTS:
                raise
            print(
                "Dropbox 要求重置分页游标，将从根目录重新扫描……",
                file=sys.stderr,
            )

    raise AssertionError("unreachable")


def validate_database(connection: sqlite3.Connection) -> SnapshotSummary:
    integrity_rows = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        details = "; ".join(integrity_rows[:5])
        raise SnapshotIntegrityError(f"临时数据库完整性检查失败：{details}")

    collision = connection.execute(
        """
        SELECT files.path_lower
        FROM files
        INNER JOIN folders USING (path_lower)
        LIMIT 1
        """
    ).fetchone()
    if collision:
        raise SnapshotIntegrityError(
            f"同一路径同时被识别为文件和目录：{collision[0]}"
        )

    file_count, folder_count, total_size = connection.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM files),
            (SELECT COUNT(*) FROM folders),
            (SELECT COALESCE(SUM(size), 0) FROM files)
        """
    ).fetchone()
    if file_count < 0 or folder_count < 0 or total_size < 0:
        raise SnapshotIntegrityError("文件数量、文件夹数量或总大小无效")

    return SnapshotSummary(
        file_count=file_count,
        folder_count=folder_count,
        total_size=total_size,
    )


def write_json(
    connection: sqlite3.Connection,
    output_path: Path,
    scan_started_at: str,
    scan_completed_at: str,
    scan_result: ScanProgress,
    summary: SnapshotSummary,
) -> None:
    temporary_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary_file.name)

    try:
        with temporary_file as stream:
            stream.write("{\n")
            stream.write('  "info": {\n')
            stream.write('    "generator": "dropbox-snapshot",\n')
            stream.write(f'    "version": "{VERSION}",\n')
            stream.write(f'    "schema_version": {SCHEMA_VERSION},\n')
            stream.write(
                '    "scan_started_at_utc": '
                + json.dumps(scan_started_at)
                + ",\n"
            )
            stream.write(
                '    "scan_completed_at_utc": '
                + json.dumps(scan_completed_at)
                + ",\n"
            )
            stream.write(
                f'    "list_request_count": {scan_result.request_count},\n'
            )
            stream.write(f'    "entries_processed": {scan_result.entry_count},\n')
            stream.write(f'    "restart_count": {scan_result.restart_count},\n')
            stream.write(f'    "file_count": {summary.file_count},\n')
            stream.write(f'    "folder_count": {summary.folder_count},\n')
            stream.write(f'    "total_size_bytes": {summary.total_size}\n')
            stream.write('  },\n')
            stream.write('  "folders": [')

            written_folder_count = 0
            folder_rows = connection.execute(
                """
                SELECT id, path_display, path_lower
                FROM folders
                ORDER BY path_lower
                """
            )
            for index, row in enumerate(folder_rows):
                record = {
                    "id": row[0],
                    "path": row[1],
                    "path_lower": row[2],
                }
                stream.write("," if index else "")
                stream.write("\n    ")
                stream.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
                written_folder_count += 1

            if written_folder_count:
                stream.write("\n  ")
            stream.write("],\n")
            stream.write('  "files": [')

            written_count = 0
            written_size = 0
            rows = connection.execute(
                """
                SELECT id, path_display, path_lower, size, content_hash, rev,
                       client_modified, server_modified
                FROM files
                ORDER BY path_lower
                """
            )
            for index, row in enumerate(rows):
                record = {
                    "id": row[0],
                    "path": row[1],
                    "path_lower": row[2],
                    "size": row[3],
                    "content_hash": row[4],
                    "rev": row[5],
                    "client_modified": row[6],
                    "server_modified": row[7],
                }
                stream.write("," if index else "")
                stream.write("\n    ")
                stream.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    )
                )
                written_count += 1
                written_size += row[3]

            if written_count:
                stream.write("\n  ")
            stream.write("]\n}\n")

            if written_count != summary.file_count:
                raise SnapshotIntegrityError(
                    "JSON 写出条数与扫描结果不一致："
                    f"{written_count} != {summary.file_count}"
                )
            if written_folder_count != summary.folder_count:
                raise SnapshotIntegrityError(
                    "JSON 写出文件夹条数与扫描结果不一致："
                    f"{written_folder_count} != {summary.folder_count}"
                )
            if written_size != summary.total_size:
                raise SnapshotIntegrityError(
                    "JSON 写出文件大小总和与扫描结果不一致："
                    f"{written_size} != {summary.total_size}"
                )

            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 Full Dropbox 中的全部文件元数据导出为一个 JSON 文件。"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"输出路径（默认：{DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return parser.parse_args()


def prepare_output_path(path: Path) -> Path:
    output_path = path.expanduser().resolve()
    if not output_path.parent.is_dir():
        raise ConfigurationError(f"输出目录不存在：{output_path.parent}")
    if output_path.exists() and output_path.is_dir():
        raise ConfigurationError(f"输出路径是目录：{output_path}")
    return output_path


def main() -> int:
    database_path: Path | None = None
    connection: sqlite3.Connection | None = None

    try:
        args = parse_args()
        output_path = prepare_output_path(args.output)
        client = build_client()

        database_file = tempfile.NamedTemporaryFile(
            prefix=".dropbox-snapshot.",
            suffix=".sqlite3",
            dir=output_path.parent,
            delete=False,
        )
        database_path = Path(database_file.name)
        database_file.close()
        connection = create_database(database_path)

        scan_started_at = utc_now()
        print(
            f"Dropbox Snapshot v{VERSION}：开始读取全盘文件元数据。",
            file=sys.stderr,
        )
        print(
            "注意：扫描完成前请不要新增、修改、删除、移动或重命名 "
            "Dropbox 中的文件/文件夹，并避免其他设备、共享成员或"
            "自动上传程序写入。",
            file=sys.stderr,
        )
        print(
            f"请求策略：单次递归遍历、每页最多约 {PAGE_LIMIT} 个条目、"
            "串行分页、不发起逐文件 metadata 请求。",
            file=sys.stderr,
        )
        with client:
            scan_result = scan(client, connection)
        scan_completed_at = utc_now()

        print("所有分页已完成，开始校验临时数据的内部一致性……", file=sys.stderr)
        summary = validate_database(connection)
        print(
            f"内部一致性检查通过：{summary.file_count} 个文件、"
            f"{summary.folder_count} 个文件夹，"
            f"{summary.total_size} 字节。",
            file=sys.stderr,
        )

        write_json(
            connection,
            output_path,
            scan_started_at,
            scan_completed_at,
            scan_result,
            summary,
        )
        print(
            f"完成：{summary.file_count} 个文件、"
            f"{summary.folder_count} 个文件夹、{summary.total_size} 字节，"
            f"共 {scan_result.request_count} 次列表请求，"
            f"重启遍历 {scan_result.restart_count} 次。",
            file=sys.stderr,
        )
        print(f"JSON：{output_path}", file=sys.stderr)
        return 0

    except KeyboardInterrupt:
        print("\n已中止；原有 JSON 输出未被破坏。", file=sys.stderr)
        return 130
    except ConfigurationError as error:
        print(f"配置错误，未生成或替换 JSON：{error}", file=sys.stderr)
        return 2
    except AuthError:
        print(
            "Dropbox 授权失败，未生成或替换 JSON：请检查令牌是否有效，并确认已授权 "
            "files.metadata.read。",
            file=sys.stderr,
        )
        return 3
    except CursorResetError:
        print(
            "Dropbox 分页游标连续失效；扫描不完整，未生成或替换 JSON。"
            "请稍后重试。",
            file=sys.stderr,
        )
        return 4
    except (ApiError, DropboxException, requests.RequestException) as error:
        print(
            f"Dropbox 请求失败；扫描不完整，未生成或替换 JSON：{error}",
            file=sys.stderr,
        )
        return 5
    except (
        OSError,
        sqlite3.Error,
        SnapshotIntegrityError,
        ValueError,
        TypeError,
    ) as error:
        print(
            f"本地处理或内部一致性检查失败，未生成或替换 JSON：{error}",
            file=sys.stderr,
        )
        return 6
    except Exception as error:
        print(
            "发生未预期错误；扫描结果不可信，未生成或替换 JSON："
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 7
    finally:
        if connection is not None:
            connection.close()
        if database_path is not None:
            database_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
