#!/bin/bash
# 编译 xiaochen-node1：esphome 生成代码（GitHub 封网时编译失败没关系），
# 手动拷头文件进 build/src（esphome 不重拷 includes），pio-core 直编。
cd /d/esphome-xiaochen || exit 1
# 统一走项目内 pio-core（工具已齐），避免 esphome 内部 pio 走默认 core 裸奔 GitHub 下载
export PLATFORMIO_CORE_DIR="D:\\esphome-xiaochen\\.esphome\\build\\xiaochen-node1\\pio-core"
echo "=== step1: esphome compile (codegen) ==="
/d/Miniconda3/envs/mww/Scripts/esphome.exe compile xiaochen-node1.yaml
echo "=== step1 rc=$? （platform 下载失败=预期内，看 main.cpp 是否已生成） ==="
grep -n 'set_broker_address' .esphome/build/xiaochen-node1/src/main.cpp
echo "=== step2: copy includes ==="
cp -v node_audio.h wozai_pcm.h .esphome/build/xiaochen-node1/src/
cp -v micro_wake_word.cpp.mwwprobe .esphome/build/xiaochen-node1/src/esphome/components/micro_wake_word/micro_wake_word.cpp
echo "=== step3: pio build ==="
cd .esphome/build/xiaochen-node1 || exit 1
export PLATFORMIO_CORE_DIR="D:\\esphome-xiaochen\\.esphome\\build\\xiaochen-node1\\pio-core"
/d/Miniconda3/envs/mww/python.exe -m platformio run
echo "=== step3 rc=$? ==="
echo "=== firmware.bin timestamps ==="
find . -name firmware.bin -newer /d/esphome-xiaochen/xiaochen-node1.yaml -exec ls -l {} \;
ls -l .pioenvs/xiaochen-node1/firmware.bin .pio/build/xiaochen-node1/firmware.bin 2>/dev/null
date
echo "=== done ==="
