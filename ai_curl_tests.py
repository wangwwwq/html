#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import subprocess
import os
import re
from typing import List, Set, Dict, Optional

print("【AI 测试】脚本已启动")

# ================== 项目目录 & 基本配置 ==================
PROJECT_DIR = "/home/wangweiqing/ocrai/ocr-customs-java"
BASE_URL = "http://localhost:8080"  # 可按需修改
os.chdir(PROJECT_DIR)
print(f"【AI 测试】已切换到项目目录：{PROJECT_DIR}")

# ================== 基础工具 ==================

def run(cmd: str) -> str:
    return subprocess.check_output(
        cmd, shell=True, text=True, stderr=subprocess.DEVNULL
    ).strip()

def line_no(content: str, pos: int) -> int:
    return content.count("\n", 0, pos) + 1

# ================== git diff 工具 ==================

def find_changed_controllers() -> List[str]:
    base = os.getenv("BASE_COMMIT")
    head = os.getenv("HEAD_COMMIT")

    print(f"【AI 测试】BASE_COMMIT={base}")
    print(f"【AI 测试】HEAD_COMMIT={head}")

    diff_files = run(f"git diff --name-only {base}..{head}").splitlines()
    controllers: List[str] = []

    for f in diff_files:
        if not f.endswith(".java"):
            continue
        content = open(f, encoding="utf-8", errors="ignore").read()
        if "@RestController" in content or "@Controller" in content:
            controllers.append(f)

    return controllers

def get_changed_lines(file_path: str, base: str, head: str) -> Set[int]:
    try:
        diff = run(f"git diff -U0 {base}..{head} -- {file_path}")
    except Exception:
        return set()

    changed: Set[int] = set()
    for line in diff.splitlines():
        if line.startswith("@@"):
            new_part = line.split()[2]  # +20,2
            start = int(new_part[1:].split(",")[0])
            length = int(new_part.split(",")[1]) if "," in new_part else 1
            for i in range(length):
                changed.add(start + i)
    return changed

# ================== Java 解析 ==================

MAPPING_RE = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping)\s*\(\s*((?:[^()"]+|"[^"]*")+)?\)',
    re.S,
)

CLASS_MAPPING_RE = re.compile(
    r'@RequestMapping\s*\(\s*(?:value\s*=\s*|path\s*=\s*)?"([^"]*)"?',
    re.S,
)

METHOD_RE = re.compile(
    r'public\s+[^{(]+\s+(\w+)\s*\(([^)]*)\)\s*(?:throws\s+[^{]+)?\{',
    re.S,
)

PARAM_SPLIT_RE = re.compile(r',(?![^()]*\))')

FIELD_RE = re.compile(r'^\s*(private|protected|public)\s+([\w<>?,\s\[\]]+)\s+(\w+)\s*;', re.M)

PRIMITIVE_SAMPLES = {
    "int": 1,
    "long": 1,
    "double": 1.0,
    "float": 1.0,
    "boolean": True,
    "Boolean": True,
    "Integer": 1,
    "Long": 1,
    "Double": 1.0,
    "Float": 1.0,
    "String": "string",
}

def parse_params(param_text: str) -> Dict[str, object]:
    """
    返回:
      body: Optional[str]  # RequestBody 类型名
      query: List[str]
      path: List[str]
    """
    result = {"body": None, "query": [], "path": []}
    for p in PARAM_SPLIT_RE.split(param_text):
        p = p.strip()
        if not p:
            continue
        tokens = p.split()
        if not tokens:
            continue
        name = tokens[-1]
        if "@RequestBody" in p:
            if len(tokens) >= 2:
                result["body"] = tokens[-2].replace("[]", "").split("<")[0]
        elif "@PathVariable" in p:
            result["path"].append(name)
        else:
            result["query"].append(name)
    return result

def parse_mapping_path(raw: str) -> str:
    if not raw:
        return "/"
    path_match = re.search(r'(?:value|path)\s*=\s*"([^"]*)"', raw)
    if path_match:
        return path_match.group(1) or "/"
    str_match = re.search(r'"([^"]*)"', raw)
    if str_match:
        return str_match.group(1) or "/"
    return "/"

def find_next_method(mapping_end: int, methods):
    for m in methods:
        if m.start() > mapping_end:
            return m
    return None

def find_method_body_end(content: str, start_pos: int) -> int:
    brace = 0
    for i in range(start_pos, len(content)):
        if content[i] == "{":
            brace += 1
        elif content[i] == "}":
            brace -= 1
            if brace == 0:
                return i
    return -1

def combine_path(prefix: str, path: str) -> str:
    if not prefix:
        return path or "/"
    if not path or path == "/":
        return prefix
    return "/".join(
        [seg for seg in (prefix.rstrip("/"), path.lstrip("/")) if seg]
    ) or "/"

# ================== RequestBody 示例生成 ==================

def find_dto_file(dto_name: str) -> Optional[str]:
    # 简单按类名查找文件
    try:
        output = run(f'find src -name "{dto_name}.java"')
    except Exception:
        return None
    return output.splitlines()[0] if output else None

def guess_sample_value(java_type: str):
    java_type = java_type.strip()
    base = java_type.replace("[]", "")
    base = base.split("<")[0].strip()
    if base in PRIMITIVE_SAMPLES:
        return PRIMITIVE_SAMPLES[base]
    # List/Set/Map 默认空
    if any(k in base for k in ("List", "Set", "Collection")):
        return []
    if "Map" in base:
        return {}
    return f"<{base}>"

def build_body_sample(dto_name: Optional[str]) -> str:
    if not dto_name:
        return '{"example": "<replace_with_body>"}'
    dto_file = find_dto_file(dto_name)
    if not dto_file:
        return f'{{"example": "<replace_with_{dto_name}>"}}'
    try:
        content = open(dto_file, encoding="utf-8", errors="ignore").read()
    except Exception:
        return f'{{"example": "<replace_with_{dto_name}>"}}'

    fields = FIELD_RE.findall(content)
    body_obj = {}
    for _, ftype, fname in fields:
        body_obj[fname] = guess_sample_value(ftype)

    # 简单序列化为 JSON 字符串（不依赖 json 模块，避免排序差异）
    items = []
    for k, v in body_obj.items():
        if isinstance(v, str):
            items.append(f'"{k}": "{v}"')
        elif isinstance(v, bool):
            items.append(f'"{k}": {str(v).lower()}')
        elif isinstance(v, (int, float)):
            items.append(f'"{k}": {v}')
        elif isinstance(v, list):
            items.append(f'"{k}": []')
        elif isinstance(v, dict):
            items.append(f'"{k}": {{}}')
        else:
            items.append(f'"{k}": "{v}"')
    if not items:
        return f'{{"example": "<replace_with_{dto_name}>"}}'
    return "{" + ", ".join(items) + "}"

# ================== curl 生成 ==================

def build_test_request(http_method: str, full_path: str, params: Dict[str, object]) -> str:
    path = full_path
    for pv in params["path"]:
        path = path.replace("{" + pv + "}", f"<{pv}>")

    query = ""
    if params["query"]:
        query = "?" + "&".join(f"{q}=<{q}>" for q in params["query"])

    url = f"{BASE_URL}{path}{query}"

    if http_method in ("GET", "DELETE"):
        return f'curl -X {http_method} "{url}"'

    body_json = build_body_sample(params["body"])
    return f'curl -X {http_method} "{url}" -H "Content-Type: application/json" -d \'{body_json}\''

# ================== 核心解析逻辑 ==================

def parse_controller_precise(controllers: List[str]):
    base = os.getenv("BASE_COMMIT")
    head = os.getenv("HEAD_COMMIT")

    for file_path in controllers:
        print(f"\n【解析】{file_path}")

        content = open(file_path, encoding="utf-8", errors="ignore").read()
        changed_lines = get_changed_lines(file_path, base, head)

        class_prefix = ""
        class_mapping = CLASS_MAPPING_RE.search(content)
        if class_mapping:
            class_prefix = class_mapping.group(1)

        mappings = list(MAPPING_RE.finditer(content))
        methods = list(METHOD_RE.finditer(content))

        affected = []

        for m in mappings:
            mapping_line = line_no(content, m.start())
            method = find_next_method(m.end(), methods)
            if not method:
                continue

            method_start = method.start()
            method_end = find_method_body_end(content, method_start)

            method_line = line_no(content, method_start)
            method_end_line = line_no(content, method_end) if method_end != -1 else method_line

            hit = (
                mapping_line in changed_lines
                or method_line in changed_lines
                or any(method_line <= l <= method_end_line for l in changed_lines)
            )

            if hit:
                affected.append((m, method))

        if not affected and changed_lines:
            print("  ⚠️ Controller 有修改，但未精确命中接口，判定：全接口受影响")
            for m in mappings:
                method = find_next_method(m.end(), methods)
                if method:
                    affected.append((m, method))

        # 去掉同一方法上的 REQUEST（RequestMapping）占位，如果已有具体映射
        pruned = []
        method_has_specific = {}
        for m, method in affected:
            http_method = m.group(1).replace("Mapping", "").upper()
            if http_method != "REQUEST":
                method_has_specific[method.start()] = True
        for m, method in affected:
            http_method = m.group(1).replace("Mapping", "").upper()
            if http_method == "REQUEST" and method_has_specific.get(method.start()):
                continue
            pruned.append((m, method))
        affected = pruned

        for m, method in affected:
            http_method = m.group(1).replace("Mapping", "").upper()
            path = parse_mapping_path(m.group(2))
            full_path = combine_path(class_prefix, path)
            method_name = method.group(1)
            params = parse_params(method.group(2))

            print(f"  ✅ [{http_method}] {full_path or '/'}")
            print(f"     方法名 : {method_name}")
            if params["body"]:
                print(f"     Body   : {params['body']}")
            else:
                print(f"     参数   : 无")

            test_cmd = build_test_request(http_method, full_path or "/", params)
            print(f"     测试   : {test_cmd}")

# ================== 主流程 ==================

def main():
    print("【阶段 1】检测 Controller 变更")
    controllers = find_changed_controllers()

    if not controllers:
        print("【AI 测试】无 Controller 变更")
        return

    for c in controllers:
        print("  -", c)

    print("\n【阶段 2/3】方法 / Mapping / Body 级精准定位")
    parse_controller_precise(controllers)

    print("\n✅ 精准解析完成")

if __name__ == "__main__":
    main()