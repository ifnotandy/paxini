"""相机演示：枚举 V4L2 节点，用 ffmpeg 录 N 秒，再用 ffprobe 回读逐帧墙钟。

用法：python3 camera.py [秒数]
"""

import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# 复用采集程序的 camera_ffmpeg（2026-11-16 起它在 Cap_Quest/capture/ 下）
sys.path.insert(0, str(SCRIPT_DIR.parent / 'Cap_Quest' / 'capture'))

from camera_ffmpeg import (  # noqa: E402
    FfmpegCamera,
    discover_video_nodes,
    ffmpeg_version,
    find_ffmpeg,
)

DURATION_S = 3.0


def stamp(ns):
    seconds = ns / 1e9
    local = datetime.fromtimestamp(seconds).strftime('%H:%M:%S.%f')
    return f'{seconds:.6f} ({local})'


def main(argv):
    try:
        duration = float(argv[1]) if len(argv) > 1 else DURATION_S
    except ValueError:
        print(f'秒数必须是数字：{argv[1]}')
        return 2
    if duration <= 0:
        print(f'秒数必须大于 0：{duration:g}')
        return 2

    nodes = discover_video_nodes()
    print('发现 V4L2 采集节点：')
    if not nodes:
        print('  未发现')
        return 1
    for node in nodes:
        print(f'  {node}')

    try:
        ffmpeg = find_ffmpeg()
    except RuntimeError as exc:
        print(exc)
        return 1
    print(f'ffmpeg：{ffmpeg_version(ffmpeg)}（{ffmpeg}）')

    with tempfile.TemporaryDirectory(prefix='pxcap-camera-') as tmp:
        output_dir = Path(tmp)
        cameras = [
            FfmpegCamera(f'cam{index}', node, output_dir, 1280, 720, 30.0,
                         'copy', ffmpeg)
            for index, node in enumerate(nodes)
        ]
        for camera in cameras:
            camera.open()
        for camera in cameras:
            camera.start()
        print(f'录制 {duration:g} 秒 ...')
        time.sleep(duration)
        for camera in cameras:
            camera.stop()

        for camera in cameras:
            video = camera.probe()
            intervals = [
                (video.timestamps_ns[index + 1] - video.timestamps_ns[index]) / 1e6
                for index in range(video.frames - 1)
            ]
            size_bytes = video.path.stat().st_size
            span_s = max(video.duration_s, 1e-9)
            mb_per_min = size_bytes / span_s * 60 / 1e6
            print(f'\n[{video.camera_id}] {camera.device} -> {video.path.name}')
            print(f'  帧数: {video.frames}')
            print(f'  分辨率: {video.width}x{video.height}')
            print(f'  实际 fps: {video.actual_fps:.2f}（容器标称 {video.avg_fps:g}）')
            if intervals:
                print(f'  帧间隔(ms): 最小 {min(intervals):.2f} '
                      f'平均 {sum(intervals) / len(intervals):.2f} '
                      f'最大 {max(intervals):.2f}')
            print(f'  首帧墙钟: {stamp(video.timestamps_ns[0])}')
            print(f'  末帧墙钟: {stamp(video.timestamps_ns[-1])}')
            print(f'  体积: {size_bytes / 1e6:.1f} MB / {span_s:.1f} 秒 = '
                  f'{mb_per_min:.1f} MB/分钟 ≈ {mb_per_min * 60 / 1024:.1f} GB/小时')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
