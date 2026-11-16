"""连续采集演示：每 0.5 秒打印一次状态，结束时给实际频率与帧间隔分布。

用法：python3 stream_demo.py [秒数]
"""

import time

from pxhandsdk import pxcap3

PORT = '/dev/ttyACM0'
RATE_HZ = 120
DURATION_S = 5.0
INTERVAL_FIELDS = ['index_proximal', 'index_distal',
                   'middle_proximal', 'middle_distal']

frames = 0
intervals_ns = []
latest = {}


def on_frame(frame):
    # 回调内只做复制：不读设备、不写文件、不阻塞。
    global frames
    frames += 1
    previous = latest.get('monotonic_ns')
    if previous is not None:
        intervals_ns.append(frame.timestamp_monotonic_ns - previous)
    latest['monotonic_ns'] = frame.timestamp_monotonic_ns
    latest['angles'] = [getattr(frame.joint_angles, name)
                        for name in INTERVAL_FIELDS]
    thumb = frame.resultant_force.thumb_one
    latest['thumb'] = (thumb.x, thumb.y, thumb.z)


def report():
    if not latest:
        print('  尚未收到帧')
        return
    angles = ' '.join(f'{value:+.1f}' for value in latest['angles'])
    thumb = latest['thumb']
    print(f'  已收 {frames} 帧 | 关节角 {angles} | '
          f'拇指合力 ({thumb[0]}, {thumb[1]}, {thumb[2]}) 0.1N')


def distribution(values):
    ordered = sorted(values)
    count = len(ordered)

    def pick(ratio):
        return ordered[min(count - 1, int(ratio * count))] / 1e6

    return (ordered[0] / 1e6, sum(ordered) / count / 1e6,
            pick(0.5), pick(0.95), ordered[-1] / 1e6)


def main():
    hand = pxcap3.PxCap3()
    rc = hand.connect_device(PORT)
    if rc != 0:
        print('连接失败:', hand.get_last_error())
        return 1

    started = time.monotonic()
    rc = hand.start_collection(RATE_HZ, on_frame)
    if rc != 0:
        print('启动采集失败:', hand.get_last_error())
        hand.disconnect_device()
        return 1

    print(f'连续采集 {DURATION_S:g} 秒，目标 {RATE_HZ} Hz，按 Ctrl+C 提前结束')
    try:
        while time.monotonic() - started < DURATION_S:
            time.sleep(0.5)
            report()
    except KeyboardInterrupt:
        print('\n已中断')
    finally:
        hand.stop_collection()
        hand.disconnect_device()

    elapsed = time.monotonic() - started
    print(f'\n实际频率: {frames / elapsed:.1f} Hz（{frames} 帧 / {elapsed:.2f} 秒）')
    if intervals_ns:
        minimum, mean, median, p95, maximum = distribution(intervals_ns)
        print(f'帧间隔(ms): 最小 {minimum:.2f} 平均 {mean:.2f} '
              f'中位 {median:.2f} p95 {p95:.2f} 最大 {maximum:.2f}')
    else:
        print('帧间隔(ms): 样本不足')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
