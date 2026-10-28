"""在香橙派上跑命令: python sshop.py 'command'  (密码在环境变量 OP_PASS)"""
import os, sys, paramiko
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("192.168.207.150", username="orangepi", password=os.environ.get("OP_PASS", ""), timeout=10)
_, out, err = c.exec_command(sys.argv[1], timeout=30)
print(out.read().decode("utf-8", "replace"))
e = err.read().decode("utf-8", "replace")
if e: print("STDERR:", e)
c.close()
