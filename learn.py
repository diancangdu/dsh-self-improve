# -*- coding: utf-8 -*-
"""dsh-self-improve CLI 入口.

用法:
  python learn.py add learn --title "..." --body "..."
  python learn.py add error --title "..." --body "..."
  python learn.py query "关键词"
  python learn.py review
  python learn.py status

任何工作目录下运行都有效 (路径基于技能目录解析).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

from learn_parser import cli_main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(cli_main())
