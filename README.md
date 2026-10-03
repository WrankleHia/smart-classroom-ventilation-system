# 大教室智能通风控制系统

这是一个结合 ESP32 传感器终端、Python Flask 管理平台和 YOLOv5 人数识别的教室环境监测与通风控制项目。系统可采集 CO₂ 浓度和温度，统计教室人数，显示通风状态，并提供设备管理、阈值设置、SD 卡日志浏览及数据分析功能。

本项目于 2025 年 9 月 14 日 完成，是一项参赛获奖作品。仓库保留了参赛版本的主要程序、模型文件与技术说明，供学习、展示和后续改进使用。

## 主要功能

- ESP32 采集 CO₂ 与 DHT11 温度数据
- TFT 屏幕循环显示环境数据和连接状态
- 摄像头或视频文件中的人员检测与计数
- 浏览器端实时监控、趋势图和通风建议
- 用户注册、登录及多设备管理
- 远程设置设备 CO₂ 告警阈值
- 浏览、下载和删除 ESP32 SD 卡日志
- 生成日、周、月统计分析
- ESP32 首次启动时通过热点页面配置 Wi-Fi 和服务器信息

## 项目结构

```text
.
├── serverfile.py                 # Flask 服务端与 YOLOv5 人数识别
├── sketch_sep12a/
│   └── sketch_sep12a.ino         # ESP32 主程序
├── yolov5s.pt                    # 默认 YOLOv5 模型权重
├── LiquidCrystal_I2C.*           # LCD 库源码
└── requirements.txt              # Python 依赖
```

> 注：示例视频 `classroom_video.mp4` 体积约 566 MB，超过 GitHub 普通文件限制，因此没有上传。运行服务端时可改用电脑摄像头，或自行在项目根目录放置同名测试视频。可选的 `yolov5m.pt` 权重也未通过网页端上传，当前代码默认使用已包含的 `yolov5s.pt`，不受影响。

## 硬件与接线

代码中使用的主要引脚如下：

| 模块 | ESP32 引脚 |
| --- | --- |
| CO₂ 传感器 UART RX | GPIO 16 |
| CO₂ 传感器 UART TX | GPIO 17 |
| DHT11 数据 | GPIO 5 |
| 状态 LED | GPIO 22 |
| 摇杆按键 | GPIO 4 |
| SD 卡 SCK | GPIO 14 |
| SD 卡 MISO | GPIO 13 |
| SD 卡 MOSI | GPIO 12 |
| SD 卡 CS | GPIO 26 |

TFT 屏幕由 `TFT_eSPI` 驱动，其引脚需要在该库的配置文件中按实际硬件设置。

## 服务端运行方法

建议使用 Python 3.9 或更高版本。

1. 克隆仓库并进入目录。

   ```bash
   git clone https://github.com/WrankleHia/smart-classroom-ventilation-system
   cd <目录>
   ```

2. 创建并启用虚拟环境。

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   Windows PowerShell 使用：

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

3. 安装依赖。

   ```bash
   pip install -r requirements.txt
   ```

4. 选择视频来源。

   - 使用电脑摄像头：将 `serverfile.py` 中的 `USE_REAL_CAMERA` 改为 `True`，并确认 `CAMERA_INDEX = 0`。
   - 使用测试视频：保持 `USE_REAL_CAMERA = False`，并把视频命名为 `classroom_video.mp4` 放在项目根目录。

5. 启动服务。

   ```bash
   python serverfile.py
   ```

6. 终端会显示访问地址，通常为 `http://本机局域网IP:5933`。首次使用先注册账户，再登录管理页面。

首次启动会自动创建本地 `users.db` 数据库。该文件包含账户和设备信息。

## ESP32 固件使用方法

1. 使用 Arduino IDE 或 PlatformIO 打开 `sketch_sep12a/sketch_sep12a.ino`。
2. 安装所需库：
   - TFT_eSPI
   - DHT sensor library
   - ArduinoJson
   - ESP32 开发板自带的 WiFi、WebServer、DNSServer、Preferences、HTTPClient、SD 等库
3. 根据硬件修改 `DEVICE_SERIAL_NUMBER`，确保每台设备的序列号唯一。
4. 在 `TFT_eSPI` 的配置中设置屏幕型号与引脚，然后选择正确的 ESP32 开发板和串口并上传。
5. 设备没有保存配置时会建立配置热点。连接该热点并打开配置页面，填写：
   - 教室 Wi-Fi 名称和密码
   - 运行 Flask 服务端电脑的局域网 IP（只填 IP，不含端口）
   - 已在服务端注册的用户名和密码
6. 保存后设备会重启，连接服务端的 `5933` 端口并自动注册。

ESP32 与服务端电脑必须能在同一网络中互相访问。同时请允许防火墙放行 TCP 5933 端口。

## 使用流程

1. 先启动 Python 服务端并注册用户。
2. 给 ESP32 上电，通过配置热点填写服务端信息和同一用户的账号。
3. 登录网页首页查看人数、CO₂、温度、通风状态和实时曲线。
4. 在“设备管理”中切换设备、调整 CO₂ 阈值、维护联系人，并管理 SD 卡数据。
5. 需要恢复配置时，按固件中的按键逻辑执行恢复出厂设置，然后重新配置网络。

## 注意事项

- 当前 Flask `secret_key` 每次启动都会随机生成，重启服务后浏览器需要重新登录。
- 服务端默认监听所有网卡，仅适合可信局域网或开发环境，部署到公网前应使用反向代理、HTTPS、固定密钥和更严格的访问控制。
- YOLOv5 首次加载可能需要联网获取代码或依赖，模型推理速度取决于电脑性能。
- 本项目用于学习和原型验证，接入真实通风设备前应增加继电器保护、故障检测和人工应急控制。
