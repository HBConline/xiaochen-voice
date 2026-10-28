# -*- coding: utf-8 -*-
"""One-off: copy WeChat m4a (CJK path) to ASCII tmp and run cut_real_pos.py."""
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"D:\wxxxdaataa\xwechat_files\wxid_igymei9xkkm822_0c54\msg\file\2026-10\2026年10月25日 16点34分.m4a"
TMP_IN = r"D:\mww\tmp\real_r9_in.m4a"

os.makedirs(r"D:\mww\tmp", exist_ok=True)
shutil.copyfile(SRC, TMP_IN)
print("[copy]", TMP_IN, os.path.getsize(TMP_IN), flush=True)

os.environ["CUT_PREFIX"] = "r9_p"
r = subprocess.run([sys.executable, r"D:\mww\cut_real_pos.py", TMP_IN], cwd=r"D:\mww")
sys.exit(r.returncode)
