"""Push finished recordings to a ModelScope dataset repo.

The recording directory stays the single source of truth: this script only reads
``Recordings/data_*``, mirrors them into a staging directory, and uploads from
there. It never edits or moves the capture output.

Design notes
------------
* No packaging by default. Each ``data_*`` directory is hard-linked into
  staging and uploaded as a plain tree, so a consumer can pull one file
  (``data.h5``, one camera's mkv) instead of the whole session. ``--zip``
  produces ``data_x.zip`` for people who prefer a single object.
* Dry run is the default. ``--upload`` is required to touch the network, so a
  typo cannot publish half a session.
* A recording is gated on the same evidence the recorder itself writes:
  ``verify.row_mismatch`` empty, glove rows present, every camera error null.
  A session that lost a camera still uploads, but says so loudly.
* Zips, when requested, are stored uncompressed: the payload is Matroska/MJPEG
  and HDF5, both already compressed, so deflate buys ~2% for a lot of CPU.
* ``SHA256SUMS.txt`` lists one entry per uploaded file in ``sha256sum -c``
  format, so the download side can verify with one command.
* ``modelscope`` is imported lazily, so a dry run works on a machine that never
  installed it (the capture box, for instance).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_REPO = "yanglab/paxini"
STAGING_NAME = ".upload_staging"
SUMS_NAME = "SHA256SUMS.txt"
CHUNK = 1 << 20


def log(message: str) -> None:
    print(message, flush=True)


def warn(message: str) -> None:
    print(f"[警告] {message}", file=sys.stderr, flush=True)


def error(message: str) -> None:
    print(f"[错误] {message}", file=sys.stderr, flush=True)


def find_recordings(root: Path) -> list[Path]:
    """Return ``data_*`` directories that actually contain a manifest."""
    if not root.is_dir():
        raise SystemExit(f"采集目录不存在：{root}")
    found = [
        path
        for path in sorted(root.glob("data_*"))
        if path.is_dir() and (path / "manifest.json").is_file()
    ]
    return found


def load_manifest(recording: Path) -> dict:
    path = recording / "manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{recording.name} 的 manifest.json 读不出来：{exc}") from exc


def gate(recording: Path, manifest: dict) -> tuple[list[str], dict]:
    """Return (blocking problems, summary facts) for one recording."""
    problems: list[str] = []
    facts: dict = {
        "rows": int(manifest.get("rows") or 0),
        "quest_rows": int(manifest.get("quest_rows") or 0),
        "duration_s": float(manifest.get("duration_s") or 0.0),
        "rate_hz": float(manifest.get("actual_rate_hz") or 0.0),
        "cameras": {},
    }

    if not (recording / "data.h5").is_file():
        problems.append("缺 data.h5")
    if manifest.get("error"):
        problems.append(f"manifest 记了启动/运行错误：{manifest['error']}")

    verify = manifest.get("verify") or {}
    mismatch = verify.get("row_mismatch") or []
    if mismatch:
        problems.append(f"row_mismatch 非空（{len(mismatch)} 组），下游按行取数会错位")

    if facts["rows"] <= 0:
        problems.append("手套 0 行")
    elif not 50.0 <= facts["rate_hz"] <= 200.0:
        problems.append(f"手套实际频率 {facts['rate_hz']:.1f} Hz 不在 50~200 之间")

    per_camera = (manifest.get("cameras") or {}).get("per_camera") or {}
    for camera_id, entry in per_camera.items():
        frames = int(entry.get("frames") or 0)
        facts["cameras"][camera_id] = frames
        if entry.get("error"):
            problems.append(f"{camera_id} 记了错误：{entry['error']}")
        if frames <= 0:
            problems.append(f"{camera_id} 0 帧")
        mkv = recording / "cameras" / f"{camera_id}.mkv"
        if not mkv.is_file():
            problems.append(f"缺 cameras/{camera_id}.mkv")
    return problems, facts


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def zip_is_current(recording: Path, archive: Path) -> bool:
    """True when the zip is newer than every file inside the recording."""
    if not archive.is_file():
        return False
    newest = 0.0
    for path in recording.rglob("*"):
        if path.is_file():
            newest = max(newest, path.stat().st_mtime)
    return archive.stat().st_mtime >= newest


def build_zip(recording: Path, archive: Path) -> None:
    tmp = archive.with_suffix(".zip.part")
    count = 0
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as bundle:
        for path in sorted(recording.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix in (".log", ".progress"):
                continue
            bundle.write(path, arcname=f"{recording.name}/{path.relative_to(recording)}")
            count += 1
    tmp.replace(archive)
    log(f"  打包 {archive.name}：{count} 个文件，{archive.stat().st_size / 2**20:.1f} MB")


def link_tree(recording: Path, target: Path) -> int:
    """Mirror one recording into staging using hard links (copy as a fallback).

    Hard links keep staging free of an 80 MB duplicate per session while still
    giving ``upload_folder`` a plain directory tree to walk.
    """
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(recording.rglob("*")):
        if not path.is_file() or path.suffix in (".log", ".progress"):
            continue
        dest = target / path.relative_to(recording)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        try:
            os.link(path, dest)
        except OSError:
            shutil.copy2(path, dest)
        count += 1
    return count


def tree_sums(recording: Path, target: Path) -> dict[str, str]:
    """sha256 of every staged file, keyed by its repo-relative path."""
    return {
        f"{recording.name}/{path.relative_to(target).as_posix()}": sha256_file(path)
        for path in sorted(target.rglob("*"))
        if path.is_file()
    }


def write_sums(staging: Path, entries: dict[str, str]) -> Path:
    path = staging / SUMS_NAME
    lines = [f"{digest}  {name}" for name, digest in sorted(entries.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"  写入 {SUMS_NAME}（{len(lines)} 条：{', '.join(sorted(entries))}）")
    return path


def local_sums(archives: list[Path]) -> dict[str, str]:
    return {archive.name: sha256_file(archive) for archive in archives}


def remote_sums(api: Any, repo_id: str, token: str) -> dict[str, str]:
    """Best-effort sha256 of what the repo already holds, as ``{path: sha}``.

    ``SHA256SUMS.txt`` describes the whole repository, but a run only knows what
    it just staged. Without this merge, uploading one session would shrink the
    manifest to the current batch and silently drop every earlier entry.

    Covers both shapes on purpose: ``data_x.zip`` (``--zip`` uploads) and
    ``data_x/...`` (the default directory uploads).
    """
    try:
        items = api.get_dataset_files(repo_id, recursive=True, token=token)
    except Exception as exc:  # noqa: BLE001 - the merge is a nice-to-have
        warn(f"读不到远端文件列表（{exc}），本次 {SUMS_NAME} 只含本批文件")
        return {}
    skip = {SUMS_NAME, "README.md", ".gitattributes"}
    sums: dict[str, str] = {}
    for item in items:
        name = item.get("Path") if isinstance(item, dict) else getattr(item, "Path", None)
        digest = item.get("Sha256") if isinstance(item, dict) else getattr(item, "Sha256", None)
        if not name or not digest or str(name) in skip or str(name).endswith("/"):
            continue
        sums[str(name)] = str(digest)
    return sums


def do_upload(
    staging: Path,
    repo_id: str,
    token: str,
    commit_message: str,
    local_entries: dict[str, str],
) -> None:
    try:
        from modelscope.hub.api import HubApi
    except ImportError:
        raise SystemExit(
            "没装 modelscope。先 `python -m pip install modelscope`，"
            "或把 zip 目录交给一台装了的机器执行。"
        )

    api = HubApi()
    try:
        # login() 在 1.40 里必须拿到 token：~/.modelscope/credentials 下只有
        # cookies / session / git_token（截断版），不能直接当 SDK 令牌用。
        api.login(token)
    except Exception as exc:  # noqa: BLE001 - surface whatever the SDK said
        raise SystemExit(f"ModelScope 登录失败：{exc}") from exc

    merged = remote_sums(api, repo_id, token)
    before = len(merged)
    merged.update(local_entries)
    if before:
        log(f"  远端已有 {before} 个文件，合并进 {SUMS_NAME}")
    write_sums(staging, merged)

    log(f"[上传] {repo_id} ← {staging}")
    api.upload_folder(
        repo_id=repo_id,
        folder_path=str(staging),
        repo_type="dataset",
        commit_message=commit_message,
        # modelscope 会在本地目录里落一个 .ms_upload_cache，别把它当数据推上去。
        ignore_patterns=[".ms_upload_cache", ".ms_upload_cache/**", "*.log", "*.part"],
    )
    log("[上传] 完成")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "把 Recordings/data_* 上传到 ModelScope 数据集仓。默认直接传目录树、"
            "只演练不联网；--zip 才打包，--upload 才真传。"
        )
    )
    parser.add_argument(
        "--recordings",
        default=str(Path(__file__).resolve().parents[1] / "Recordings"),
        help="采集目录根（默认 <Paxini-Cap>/Recordings）",
    )
    parser.add_argument(
        "--staging",
        default=None,
        help=f"打包输出目录（默认 <recordings>/{STAGING_NAME}）",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="NAME",
        help="只处理这些目录（可重复）；默认处理全部合格的",
    )
    parser.add_argument("--repo-id", default=DEFAULT_REPO, help=f"默认 {DEFAULT_REPO}")
    parser.add_argument(
        "--commit-message",
        default=None,
        help="上传提交信息（默认带时间戳）",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MODELSCOPE_API_TOKEN"),
        help="访问令牌；也可以用环境变量 MODELSCOPE_API_TOKEN",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="真的上传；不加这个只是演练",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="已有的 zip 比目录旧也重新打包；只在 --zip 模式下有意义",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help=(
            "打包成 <目录名>.zip 再传；默认不打包，直接把 data_* 目录树传上去"
        ),
    )
    parser.add_argument(
        "--allow-broken",
        action="store_true",
        help="有阻塞问题的目录也上传（不建议）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.recordings).expanduser().resolve()
    staging = Path(args.staging).expanduser().resolve() if args.staging else root / STAGING_NAME
    staging.mkdir(parents=True, exist_ok=True)

    recordings = find_recordings(root)
    if not recordings:
        error(f"{root} 下没有可用的 data_* 目录")
        return 2
    if args.only:
        wanted = set(args.only)
        recordings = [path for path in recordings if path.name in wanted]
        if not recordings:
            error(f"没找到 {', '.join(sorted(wanted))}")
            return 2

    log(f"采集目录：{root}")
    log(f"打包目录：{staging}")
    log(f"目标仓库：{args.repo_id}（repo_type=dataset）")
    log("")

    ready: list[tuple[Path, dict]] = []
    blocked: list[str] = []
    for recording in recordings:
        manifest = load_manifest(recording)
        problems, facts = gate(recording, manifest)
        cam_text = "，".join(
            f"{name}={frames}帧" for name, frames in facts["cameras"].items()
        ) or "无相机"
        head = (
            f"{recording.name}：手套 {facts['rows']} 行 / {facts['rate_hz']:.1f} Hz / "
            f"{facts['duration_s']:.1f} s，{cam_text}，Quest {facts['quest_rows']} 行"
        )
        if problems:
            blocked.append(recording.name)
            warn(f"{head}\n        阻塞：{'；'.join(problems)}")
            if not args.allow_broken:
                continue
        else:
            log(head)
        ready.append((recording, facts))

    if not ready:
        error("没有可以上传的目录（有阻塞项时用 --allow-broken 强制）")
        return 2

    local_entries: dict[str, str] = {}
    archives: list[Path] = []
    if args.zip:
        for recording, _facts in ready:
            stale_dir = staging / recording.name
            if stale_dir.exists():
                shutil.rmtree(stale_dir)
            archive = staging / f"{recording.name}.zip"
            if args.rebuild or not zip_is_current(recording, archive):
                build_zip(recording, archive)
            else:
                log(f"  复用 {archive.name}（{archive.stat().st_size / 2**20:.1f} MB）")
            archives.append(archive)
        local_entries = local_sums(archives)
    else:
        for recording, _facts in ready:
            target = staging / recording.name
            # 换模式重跑时，上一次留下的另一种形态必须清掉，否则两种都会传上去。
            if target.exists():
                shutil.rmtree(target)
            stale_zip = staging / f"{recording.name}.zip"
            if stale_zip.exists():
                stale_zip.unlink()
            count = link_tree(recording, target)
            local_entries.update(tree_sums(recording, target))
            log(f"  直接上传 {recording.name}/：{count} 个文件（硬链接，不占额外空间）")

    write_sums(staging, local_entries)

    if archives:
        staged = [archive.stem for archive in archives]
    else:
        staged = [recording.name for recording, _ in ready]
    commit_message = args.commit_message or (
        "上传 " + ", ".join(staged) + f"（{datetime.now().strftime('%Y-%m-%d %H:%M')}）"
    )
    if not args.upload:
        log("")
        log("[演练] 未联网。确认无误后加 --upload 真正上传。")
        visible = [
            path.name + ("/" if path.is_dir() else "")
            for path in sorted(staging.iterdir())
            if not path.name.startswith(".")
        ]
        log(f"[演练] 将上传：{', '.join(visible)}")
        return 0

    token = args.token or os.environ.get("MODELSCOPE_API_TOKEN") or ""
    if not token:
        error(
            "没有访问令牌。用 --token 或 export MODELSCOPE_API_TOKEN=... 提供，"
            "在 https://modelscope.cn/my/myaccesstoken 获取（别写进仓库）。"
        )
        return 2
    do_upload(staging, args.repo_id, token.strip(), commit_message, local_entries)
    log(f"上传内容：{', '.join(staged)} + {SUMS_NAME}")
    if blocked:
        log(f"未上传（有阻塞）：{', '.join(blocked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
