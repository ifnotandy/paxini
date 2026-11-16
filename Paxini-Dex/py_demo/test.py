from pxhandsdk import pxdex3

hand = pxdex3.PxDex3()
rc = hand.connect_device('/dev/ttyUSB0')
if rc == 0:
    rc, version = hand.get_firmware_versions()
    if rc == 0:
        print(version.firmware_version)
    rc, position = hand.get_current_motor_position()
    if rc == 0:
        print(position.index_proximal_pos, position.index_distal_pos)
    hand.disconnect_device()
else:
    print(hand.get_last_error())
