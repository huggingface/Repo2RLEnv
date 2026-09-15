"""Retained SCALER generator preparation helpers; see provenance.md."""

from __future__ import annotations


def fix_newlines_in_python_strings(code: str) -> str:
    """
    将 Python 源码中【字符串内部】的真实换行符替换为字面量 '\\n'，
    以修复像 print("Hello\nWorld") 被错误写成 print("Hello
    World") 的情况。
    仅处理字符串，不动原本的 \\n、\\t、外部换行、注释等。
    支持单引号和双引号字符串。
    """
    out = []
    in_str = False  # 是否在字符串内部
    in_single_quote = False  # 是否在单引号字符串内部
    in_double_quote = False  # 是否在双引号字符串内部
    escaped = False  # 上一个字符是否是反斜杠（处理 \' \" 等）
    i = 0

    while i < len(code):
        ch = code[i]

        if in_str:
            if escaped:
                # 前一个字符是反斜杠，当前字符原样放入（保持已有的转义如 \'、\"、\\n）
                out.append(ch)
                escaped = False
            else:
                if ch == "\\":
                    out.append(ch)
                    escaped = True
                elif ch == "'" and in_single_quote:  # 结束单引号字符串
                    out.append(ch)
                    in_single_quote = False
                    in_str = False
                elif ch == '"' and in_double_quote:  # 结束双引号字符串
                    out.append(ch)
                    in_double_quote = False
                    in_str = False
                elif ch == "\r":  # 处理 \r\n 或单独 \r
                    # 丢弃 \r，自行判断下一位是否 \n
                    if i + 1 < len(code) and code[i + 1] == "\n":
                        # 将 CRLF 作为一个换行处理
                        out.append("\\n")
                        i += 1  # 跳过 \n
                    else:
                        out.append("\\n")
                elif ch == "\n":
                    # 这是不合法的：字符串内部的真实换行，替换为字面量 \n
                    out.append("\\n")
                else:
                    out.append(ch)

        else:
            if ch == '"':  # 进入双引号字符串
                out.append(ch)
                in_str = True
                in_double_quote = True
                escaped = False
            elif ch == "'":  # 进入单引号字符串
                out.append(ch)
                in_str = True
                in_single_quote = True
                escaped = False
            else:
                out.append(ch)

        i += 1

    # 保证文件末尾有换行
    if not out or out[-1] != "\n":
        out.append("\n")
    return "".join(out)


def import_needed_module_for_python(code_str):
    wrapped_code = f"""
import traceback
from string import *
from re import *
from datetime import *
from collections import *
from heapq import *
from bisect import *
from copy import *
from math import *
from random import *
from statistics import *
from itertools import *
from functools import *
from operator import *
from io import *
from sys import *
from json import *
from builtins import *
from typing import *
import string
import re
import datetime
import collections
import heapq
import bisect
import copy
import math
import random
import statistics
import itertools
import functools
import operator
import io
import sys
import json
{code_str}
"""
    return wrapped_code
