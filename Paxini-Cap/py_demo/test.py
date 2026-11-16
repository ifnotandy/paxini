from pxhandsdk import pxcap3

# 最小只读示例：连接、版本、SN、故障、一次关节角与 10 路合力。
# 不启动连续采集，不写任何文件。串口按实际枚举到的 /dev/ttyACM* 修改。
PORT = '/dev/ttyACM0'
SLOTS = ('thumb_one', 'thumb_two', 'index_zero', 'index_one',
         'middle_zero', 'middle_one', 'middle_two', 'middle_three',
         'palm_zero', 'palm_one')


def main():
    hand = pxcap3.PxCap3()
    rc = hand.connect_device(PORT)
    if rc != 0:
        print(hand.get_last_error())
        return 1

    rc, version = hand.get_sdk_version()
    if rc == 0:
        print('SDK 版本:', version)
    rc, firmware = hand.get_firmware_versions()
    if rc == 0:
        print('主控固件:', firmware.controller_version)
        print('传感器模组:', firmware.sensor_versions[:firmware.sensor_version_count])
    rc, sn = hand.get_sn()
    if rc == 0:
        print('SN:', sn)
    rc, fault = hand.get_fault_code()
    if rc == 0:
        print('故障: 传感器', fault.sensor_fault_count,
              '项 / 磁编码器', fault.magnetic_encoder_fault_count, '项')
    rc, angles = hand.get_encoder_angles()
    if rc == 0:
        print('关节角(度):', angles.index_proximal, angles.index_distal,
              angles.middle_proximal, angles.middle_distal)
    rc, force = hand.get_sensor_resultant_force()
    if rc == 0:
        for name in SLOTS:
            point = getattr(force, name)
            print(f'{name}: ({point.x}, {point.y}, {point.z}) 0.1N')

    hand.disconnect_device()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
