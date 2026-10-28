# -*- coding: utf-8 -*-
import json
nb = json.load(open(r"D:\mww\microWakeWord\notebooks\basic_training_notebook.ipynb", encoding="utf8"))
for c in nb["cells"]:
    s = "".join(c["source"])
    if "training_config" in s or "training_parameters" in s or "mixednet" in s:
        print("---")
        print(s[:900])
