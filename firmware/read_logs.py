"""COM6 (USB-Serial-JTAG) 被动读日志。

esphome logs 在这个环境里因为 protobuf 版本冲突起不来（aioesphomeapi 导入失败），
而且它默认会通过 API 连接板子。这里只做串口透传：
- dtr/rts 保持 False，避免 USB-Serial-JTAG 复位进下载模式
- --reset 时用 RTS 手动复位一次，从复位那一刻开始抓，不丢 boot 段
- 顺手把颜色转义、\\r、日志行里的源位置 [file:line] 去掉，方便 grep

用法: python read_logs.py [COM6] [秒数] [输出文件] [--reset]
"""
import os
import re
import sys
import threading
import time
import urllib.request

import serial

ANSI = re.compile('\x1b' + r'\[[0-9;]*m')
LOC = re.compile(r'\[\[([IWEDC])\]\[[^\]]*\]:')

port = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].isdigit() else 'COM6'
secs = int(next((a for a in sys.argv[1:] if a.isdigit()), 120))
out = next((a for a in sys.argv[2:] if not a.isdigit() and a != 'COM6' and not a.startswith('--')),
           'serial_capture.log')
reset = '--reset' in sys.argv
capture = next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == '--capture' and i + 1 < len(sys.argv)),
               None)
files = re.compile(r'WAVB:(\w+\.wav)')
seen = set()


def _grab(name):
    if name in seen:
        return
    seen.add(name)
    os.makedirs(capture, exist_ok=True)
    dst = os.path.join(capture, name)
    for _ in range(10):
        try:
            with urllib.request.urlopen(f'http://{host}/fn/{name}', timeout=15) as r:
                data = r.read()
            if data:
                with open(dst, 'wb') as f:
                    f.write(data)
                print(f'[capture] {dst} ({len(data)} bytes)', flush=True)
                return
        except Exception as e:  # 板子刚写完文件/尚未起 HTTP 时重试
            last = e
            time.sleep(0.5)
    print(f'[capture] FAILED {name}: {last}', flush=True)


host = os.environ.get('NODE_HOST', 'xiaochen-node1.local')

s = serial.Serial(port, 115200)
s.dtr = False
s.rts = False
if reset:
    # USB-Serial-JTAG 的 RTS 是复位线（esptool 用它做 hard reset）
    time.sleep(0.5)
    s.rts = True
    time.sleep(0.1)
    s.rts = False
    time.sleep(0.1)
t0 = time.time()
with open(out, 'w', encoding='utf-8', errors='replace') as f:
    while time.time() - t0 < secs:
        data = s.read(s.in_waiting or 1)
        if not data:
            continue
        text = data.decode('utf-8', 'replace')
        text = ANSI.sub('', text).replace('\r', '')
        text = LOC.sub(lambda m: '[' + m.group(1) + ']:', text)
        f.write(text)
        f.flush()
        if capture:
            for m in files.findall(text):
                threading.Thread(target=_grab, args=(m,), daemon=True).start()
        sys.stdout.write(text)
        sys.stdout.flush()
s.close()
