
import cv2
import time

# 使用 by-path 设备节点，避免重启后 /dev/videoN 编号变化导致左右相机互换
CAMERA_1 = (
    "/dev/v4l/by-path/"
    "pci-0000:00:14.0-usb-0:1.3.1:1.0-video-index0"
)
CAMERA_2 = (
    "/dev/v4l/by-path/"
    "pci-0000:00:14.0-usb-0:1.3.2:1.0-video-index0"
)

# 最终显示窗口的尺寸
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 480

def open_camera(device):
    cap = cv2.VideoCapture(device, cv2.CAP_V4L2)

    # 设置 MJPEG 格式
    cap.set(cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc(*'MJPG'))

    # 设置分辨率
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    # 设置帧率
    cap.set(cv2.CAP_PROP_FPS, 30)

    if not cap.isOpened():
        print(f"无法打开摄像头: {device}")
        return None

    print(f"摄像头已打开: {device}")
    print("实际分辨率:",
          cap.get(cv2.CAP_PROP_FRAME_WIDTH),
          cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    return cap


# 初始化两个摄像头
cap1 = open_camera(CAMERA_1)
cap2 = open_camera(CAMERA_2)

if cap1 is None or cap2 is None:
    if cap1 is not None:
        cap1.release()
    if cap2 is not None:
        cap2.release()
    raise SystemExit("摄像头初始化失败，请检查设备连接")

print("双摄像头启动成功，按 Q 退出")

try:
    while True:
        ret1, frame1 = cap1.read()
        ret2, frame2 = cap2.read()

        if not ret1 or not ret2:
            print("图像读取失败")
            break

        # 将两个摄像头画面统一缩放到相同尺寸
        frame1 = cv2.resize(frame1, (640, 360))
        frame2 = cv2.resize(frame2, (640, 360))

        # 在画面上添加摄像头标识
        cv2.putText(frame1, "Camera 1", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1, (0, 255, 0), 2)

        cv2.putText(frame2, "Camera 2", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    1, (0, 255, 0), 2)

        # 横向拼接两个画面
        combined_frame = cv2.hconcat([frame1, frame2])

        # 缩放到最终显示尺寸
        combined_frame = cv2.resize(
            combined_frame,
            (WINDOW_WIDTH, WINDOW_HEIGHT)
        )

        # 在同一个窗口中显示
        cv2.imshow("PXDex Dual Camera", combined_frame)

        # 按 Q 退出
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

finally:
    cap1.release()
    cap2.release()
    cv2.destroyAllWindows()
