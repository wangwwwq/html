#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 接口影响链路分析脚本 - 生成影响链路报告并上传

功能特性:
1. 自动检测 Controller 变更
2. 分析代码依赖关系
3. 生成影响链路报告
4. 上传影响链路报告到指定接口
"""

import subprocess
import os
import re
import time
import json
import builtins
from typing import List, Set, Dict, Optional, Tuple
try:
    import requests
except ImportError:
    print("⚠️  警告: 未安装 requests 库，请运行: pip install requests")
    requests = None

try:
    import javalang
except ImportError:
    javalang = None
    print("⚠️  警告: 未安装 javalang，将使用正则解析 Java 代码（pip install javalang 可启用 AST 解析）")

print("【AI 测试】脚本已启动")

# ================== 项目目录 & 基本配置 ==================
# 可通过环境变量 PROJECT_DIR / BASE_URL 覆盖默认值
PROJECT_DIR = "/home/gitlab-runner/builds/_TQ32fEV/0/wuzhuoyan/ocr-customs-java"
BASE_URL = "http://localhost:9979/ocr-service"  # 可按需修改
HEALTH_CHECK_PATH = "/actuator/health"  # 健康检查路径，可根据实际情况修改
DEPLOYMENT_WAIT_MAX = 300  # 最大等待部署时间（秒）
DEPLOYMENT_CHECK_INTERVAL = 5  # 检查间隔（秒）
LOG_DIR = "/home/gitlab-runner/running/ocr-customs-java"
LOG_FILE = os.getenv("AI_TEST_LOG_FILE", os.path.join(LOG_DIR, "ai_test.log"))

# ===== 阈值与退化策略配置 =====
AI_MAX_CHANGED_FILES = int(os.getenv("AI_MAX_CHANGED_FILES", "30"))
AI_MAX_CONTROLLERS = int(os.getenv("AI_MAX_CONTROLLERS", "50"))
AI_MAX_TESTCASES = int(os.getenv("AI_MAX_TESTCASES", "200"))
AI_FALLBACK_MODE = os.getenv("AI_FALLBACK_MODE", "all").lower()  # all | changed_only
AI_IMPACT_OUTPUT_MODE = os.getenv("AI_IMPACT_OUTPUT_MODE", "snippet").lower()  # snippet | full
# 影响链路报告写到 running 目录，便于 CI 收集
IMPACT_REPORT_FILE = os.path.join(LOG_DIR, "ai_impact_report.md")

# ================== 日志输出 ==================

def setup_logging():
    """
    将所有 print 同步输出到本地日志文件
    """
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    real_print = builtins.print

    def tee_print(*args, **kwargs):
        real_print(*args, **kwargs)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                real_print(*args, **kwargs, file=f)
        except Exception:
            # 日志写入失败时不中断流程
            pass

    builtins.print = tee_print
    real_print(f"【AI 测试】日志输出路径: {LOG_FILE}")


setup_logging()
os.chdir(PROJECT_DIR)
print(f"【AI 测试】已切换到项目目录：{PROJECT_DIR}")

# ================== 基础工具 ==================

def run(cmd: str) -> str:
    return subprocess.check_output(
        cmd, shell=True, text=True, stderr=subprocess.DEVNULL
    ).strip()

def line_no(content: str, pos: int) -> int:
    return content.count("\n", 0, pos) + 1

# ================== git diff & 变更收集 ==================

def git_diff_files(base: str, head: str) -> List[str]:
    """
    返回 base..head 之间变更的文件列表（相对 PROJECT_DIR）
    """
    if not base or not head:
        print("  ⚠️ BASE_COMMIT/HEAD_COMMIT 未设置，无法从 git diff 推导变更文件")
        return []
    try:
        diff_output = run(f"git diff --name-only {base}..{head}")
    except Exception as e:
        print(f"  ⚠️ 执行 git diff 失败: {e}")
        return []
    files = [f.strip() for f in diff_output.splitlines() if f.strip()]
    print(f"  🔍 git diff 变更文件数: {len(files)}")
    return files


def classify_changed_files(diff_files: List[str]) -> Dict[str, Set[str]]:
    """
    阶段 A：收集变更文件并分类
    返回:
        {
          "controller": set(),
          "service_impl": set(),
          "service": set(),
          "mapper": set(),
          "mapper_xml": set(),
          "other": set(),
        }
    """
    changed = {
        "controller": set(),
        "service_impl": set(),
        "service": set(),
        "mapper": set(),
        "mapper_xml": set(),
        "other": set(),
    }

    for rel_path in diff_files:
        path = rel_path.replace("\\", "/")
        full_path = os.path.join(PROJECT_DIR, rel_path)
        ext = os.path.splitext(path)[1].lower()

        if ext == ".java":
            try:
                content = open(full_path, encoding="utf-8", errors="ignore").read()
            except Exception:
                content = ""

            simple_name = os.path.basename(path)

            # Controller
            if "@RestController" in content or "@Controller" in content:
                changed["controller"].add(full_path)
                continue

            # ServiceImpl
            if simple_name.endswith("ServiceImpl.java") or "/service/impl/" in path:
                changed["service_impl"].add(full_path)
                continue

            # Service 接口
            if simple_name.endswith("Service.java") or "/service/" in path:
                changed["service"].add(full_path)
                continue

            # Mapper 接口
            if simple_name.endswith("Mapper.java") or "/mapper/" in path:
                changed["mapper"].add(full_path)
                continue

            changed["other"].add(full_path)

        elif ext == ".xml":
            simple_name = os.path.basename(path)
            if simple_name.endswith("Mapper.xml") or "/mapper/" in path or "/mybatis/" in path:
                changed["mapper_xml"].add(full_path)
            else:
                changed["other"].add(full_path)
        else:
            changed["other"].add(full_path if os.path.isabs(rel_path) else full_path)

    print("【阶段 A】变更文件分类结果：")
    for k, v in changed.items():
        print(f"  - {k}: {len(v)} 个")
    return changed


def get_changed_lines(file_path: str, base: str, head: str) -> Set[int]:
    """
    返回某 Java 文件在 base..head 之间变更的行号集合
    """
    try:
        rel = os.path.relpath(file_path, PROJECT_DIR)
        diff = run(f"git diff -U0 {base}..{head} -- {rel}")
    except Exception:
        return set()

    changed: Set[int] = set()
    for line in diff.splitlines():
        if line.startswith("@@"):
            new_part = line.split()[2]  # +20,2 或 +20,0（删除时）
            start = int(new_part[1:].split(",")[0])
            length = int(new_part.split(",")[1]) if "," in new_part else 1

            # 当 length = 0 时（纯删除操作），也需要标记该位置
            if length == 0:
                # 删除操作：标记删除发生的位置
                changed.add(start)
            else:
                for i in range(length):
                    changed.add(start + i)
    return changed

# ================== Java 解析 ==================

FIELD_RE = re.compile(r'^\s*(private|protected|public)\s+([\w<>?,\s\[\]]+)\s+(\w+)\s*;', re.M)
PARAM_SPLIT_RE = re.compile(r',(?![^()]*\))')

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


def parse_request_mapping_http_method(raw_args: str) -> str:
    if not raw_args:
        return "GET"
    ms = re.findall(r'RequestMethod\.(GET|POST|PUT|DELETE|PATCH)', raw_args)
    return ms[0] if ms else "GET"

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


# 根据行号和列号计算在整个文件字符串中的偏移量
def _offset_from_line_col(content: str, line: Optional[int], col: Optional[int]) -> int:
    if not line or line <= 0:
        return 0
    lines = content.splitlines(keepends=True)
    if line - 1 >= len(lines):
        return len(content)
    offset = sum(len(l) for l in lines[: line - 1])
    if col and col > 0:
        offset += col - 1
    return offset


# ================== DTO/VO Body 结构解析 ==================

def _is_custom_dto_type(type_name: str, java_index) -> bool:
    """判断类型是否为自定义 DTO/VO/Entity 类型"""
    basic_types = {
        "String", "Integer", "Long", "Double", "Float", "Boolean", "Byte", "Short",
        "int", "long", "double", "float", "boolean", "byte", "short", "char",
        "BigDecimal", "BigInteger", "Date", "LocalDate", "LocalDateTime", "LocalTime",
        "Timestamp", "Object", "Map", "HashMap", "MultipartFile",
    }
    if type_name in basic_types:
        return False
    return type_name in java_index.get("by_simple", {})


def _parse_dto_structure(
    dto_name: str,
    java_index,
    visited: Optional[Set[str]] = None,
    max_depth: int = 5,
) -> Optional[Dict]:
    """
    递归解析 DTO/VO 类的字段结构，支持 List 嵌套。
    """
    if visited is None:
        visited = set()

    if dto_name in visited or max_depth <= 0:
        return {"class_name": dto_name, "fields": [], "note": "循环引用或递归深度限制"}

    visited.add(dto_name)

    meta = java_index["by_simple"].get(dto_name)
    if not meta:
        return None

    content = meta.get("content", "")
    if not content:
        return None

    result = {"class_name": dto_name, "fields": []}

    if javalang is not None:
        try:
            tree = javalang.parse.parse(content)
            for _, cls in tree.filter(javalang.tree.ClassDeclaration):
                if getattr(cls, "name", None) != dto_name:
                    continue
                for field in getattr(cls, "fields", []):
                    field_type = getattr(field, "type", None)
                    if not field_type:
                        continue

                    type_name = getattr(field_type, "name", "") or ""
                    is_list = type_name in ("List", "ArrayList", "Set", "HashSet", "Collection")

                    inner_type = None
                    type_args = getattr(field_type, "arguments", None)
                    if type_args:
                        for arg in type_args:
                            inner_type = getattr(arg, "name", None)
                            break

                    for decl in getattr(field, "declarators", []):
                        field_name = getattr(decl, "name", "")
                        if not field_name:
                            continue

                        actual_type = inner_type if is_list and inner_type else type_name

                        nested = None
                        if actual_type and _is_custom_dto_type(actual_type, java_index):
                            nested = _parse_dto_structure(
                                actual_type, java_index, visited.copy(), max_depth - 1
                            )

                        result["fields"].append({
                            "name": field_name,
                            "type": actual_type,
                            "is_list": is_list,
                            "nested": nested,
                        })
                break
        except Exception as e:
            print(f"  ⚠️ AST 解析 DTO {dto_name} 失败: {e}")

    # AST 解析失败或无结果时，使用正则兜底
    if not result["fields"]:
        result["fields"] = _parse_dto_fields_regex(content, dto_name, java_index, visited, max_depth)

    return result


def _parse_dto_fields_regex(
    content: str,
    dto_name: str,
    java_index,
    visited: Set[str],
    max_depth: int,
) -> List[Dict]:
    """使用正则表达式解析 DTO 字段（AST 解析失败时的兜底方案）"""
    fields = []

    # 匹配字段: private List<ItemVO> items; 或 private String name;
    field_pattern = re.compile(
        r'(?:private|protected|public)\s+'
        r'(List|ArrayList|Set|HashSet|Collection)?\s*'
        r'(?:<\s*(\w+)\s*>)?\s*'
        r'(\w+)\s+(\w+)\s*;',
        re.M
    )

    for m in field_pattern.finditer(content):
        collection_type = m.group(1)
        generic_type = m.group(2)
        type_name = m.group(3)
        field_name = m.group(4)

        is_list = collection_type is not None
        actual_type = generic_type if is_list and generic_type else type_name

        nested = None
        if actual_type and _is_custom_dto_type(actual_type, java_index):
            nested = _parse_dto_structure(
                actual_type, java_index, visited.copy(), max_depth - 1
            )

        fields.append({
            "name": field_name,
            "type": actual_type,
            "is_list": is_list,
            "nested": nested,
        })

    return fields


def _format_dto_structure_markdown(dto_struct: Optional[Dict], indent: int = 0) -> str:
    """将 DTO 结构格式化为 Markdown 输出"""
    if not dto_struct:
        return ""

    lines = []
    prefix = "  " * indent
    class_name = dto_struct.get("class_name", "Unknown")

    if indent == 0:
        lines.append(f"**{class_name}** 字段结构：")

    fields = dto_struct.get("fields", [])
    if not fields:
        note = dto_struct.get("note", "无字段信息")
        lines.append(f"{prefix}  - ({note})")
        return "\n".join(lines)

    for field in fields:
        name = field.get("name", "")
        ftype = field.get("type", "")
        is_list = field.get("is_list", False)
        nested = field.get("nested")

        type_display = f"List<{ftype}>" if is_list else ftype
        lines.append(f"{prefix}  - `{name}`: `{type_display}`")

        if nested and nested.get("fields"):
            nested_md = _format_dto_structure_markdown(nested, indent + 2)
            lines.append(nested_md)

    return "\n".join(lines)


# ================== Java AST 解析辅助 ==================

def _ast_get_annotation_name(ann) -> str:
    """获取注解简单名，如 'GetMapping' 或 'RequestMapping'"""
    name = getattr(ann, "name", "") or ""
    return name.split(".")[-1]


def _ast_get_literal_string(val) -> Optional[str]:
    """
    从 javalang 的 Literal 或其他简单节点中提取字符串值（去掉引号）。
    """
    if val is None:
        return None
    # javalang.tree.Literal 一般有 .value，如 "\"/path\""
    v = getattr(val, "value", None)
    if isinstance(v, str):
        return v.strip('"').strip("'")
    # 兜底：如果本身就是字符串
    if isinstance(val, str):
        return val
    return None


def _ast_get_annotation_attr(ann, attr_name: str):
    """
    从注解里取指定属性（如 value/path/method）对应的 AST 节点。
    支持 @Xxx("...") 和 @Xxx(value="...") 两种形式。
    """
    # @RequestMapping("/foo")
    element = getattr(ann, "element", None)
    if element is not None:
        return element

    # @RequestMapping(value="/foo", method=...)
    for pair in getattr(ann, "element_pairs", []):
        if getattr(pair, "name", None) == attr_name:
            return getattr(pair, "value", None)
    return None


def _ast_extract_mapping_from_annotation(ann) -> Optional[Tuple[str, str]]:
    """
    从一个方法或类上的 Mapping 注解中解析出 (http_method, path)；
    http_method 可能是 "GET"/"POST"/"REQUEST" 等。
    """
    name = _ast_get_annotation_name(ann)

    # 统一获取 path/value
    val_node = _ast_get_annotation_attr(ann, "value") or _ast_get_annotation_attr(ann, "path")
    path_str = _ast_get_literal_string(val_node) or "/"

    if name == "RequestMapping":
        # 类似原正则逻辑，先用占位 "REQUEST"，后续再根据 method 属性或默认 GET 处理
        http_method = "REQUEST"

        # 尝试从 method=RequestMethod.POST 等中解析出具体方法
        method_node = _ast_get_annotation_attr(ann, "method")
        # 可能是单个，也可能是数组
        candidates: List[str] = []
        if method_node is not None:
            vals = getattr(method_node, "values", None) or [method_node]
            for v in vals:
                qualifier = getattr(v, "qualifier", "") or ""
                member = getattr(v, "member", "") or ""
                if member in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    candidates.append(member)
                elif qualifier.endswith("RequestMethod") and member:
                    candidates.append(member)
        if candidates:
            http_method = candidates[0]

        return http_method, path_str or "/"

    if name.endswith("Mapping"):
        http_method = name.replace("Mapping", "").upper()  # GetMapping -> GET
        return http_method, path_str or "/"

    return None


def _ast_extract_method_params(method) -> Dict[str, object]:
    """
    尽量保持和 parse_params 输出一致：
      { "body": Optional[str], "query": [str], "path": [str] }
    """
    result: Dict[str, object] = {"body": None, "query": [], "path": []}

    for p in getattr(method, "parameters", []):
        name = getattr(p, "name", None)
        if not name:
            continue

        type_name = ""
        if getattr(p, "type", None) is not None:
            type_name = getattr(p.type, "name", "") or ""

        ann_names = {_ast_get_annotation_name(a) for a in getattr(p, "annotations", [])}

        if "RequestBody" in ann_names:
            result["body"] = type_name or None
        elif "PathVariable" in ann_names:
            result["path"].append(name)
        else:
            result["query"].append(name)

    return result


def parse_controller_with_ast(file_path: str) -> Optional[List[Dict]]:
    """
    使用 javalang AST 解析 Controller，返回 test_cases；
    若 javalang 不可用或解析失败，返回 None（由上层回退到正则解析）。
    """
    if javalang is None:
        return None

    try:
        content = open(file_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None

    try:
        tree = javalang.parse.parse(content)
    except Exception as e:
        print(f"  ⚠️ AST 解析失败，跳过该 Controller: {e}")
        return []

    test_cases: List[Dict] = []

    for _, cls in tree.filter(javalang.tree.ClassDeclaration):
        # 是否 Controller
        ann_names = {_ast_get_annotation_name(a) for a in getattr(cls, "annotations", [])}
        if "RestController" not in ann_names and "Controller" not in ann_names:
            continue

        # 类级别前缀（@RequestMapping）
        class_prefix = ""
        for ann in getattr(cls, "annotations", []):
            mapping = _ast_extract_mapping_from_annotation(ann)
            if not mapping:
                continue
            _, p = mapping
            class_prefix = p or class_prefix

        # 方法级别
        for method in getattr(cls, "methods", []):
            method_mappings: List[Tuple[str, str]] = []
            for ann in getattr(method, "annotations", []):
                mapping = _ast_extract_mapping_from_annotation(ann)
                if mapping:
                    method_mappings.append(mapping)

            if not method_mappings:
                continue

            params = _ast_extract_method_params(method)

            for http_method, path in method_mappings:
                # 处理 RequestMapping 默认方法
                if http_method == "REQUEST":
                    http_method = "GET"

                full_path = combine_path(class_prefix, path or "/")
                method_name = getattr(method, "name", "")

                print(f"  ✅ [AST] [{http_method}] {full_path or '/'}")
                print(f"     方法名 : {method_name}")
                if params["body"]:
                    print(f"     Body   : {params['body']}")
                else:
                    print(f"     参数   : 无")

                test_cases.append(
                    {
                        "file_path": file_path,
                        "http_method": http_method,
                        "full_path": full_path or "/",
                        "method_name": method_name,
                        "params": params,
                    }
                )

    return test_cases

# ================== 项目索引 & 依赖关系 ==================

JAVA_CLASS_RE = re.compile(r'\b(class|interface)\s+(\w+)\b')
JAVA_PACKAGE_RE = re.compile(r'package\s+([\w\.]+)\s*;')
XML_MAPPER_NS_RE = re.compile(r'<mapper[^>]*\snamespace\s*=\s*"([^"]+)"', re.S)

_java_index_cache = None
_xml_index_cache = None


def build_project_index():
    """
    阶段 B：扫描 src/main/java 与 src/main/resources，建立一次性索引缓存
    """
    global _java_index_cache, _xml_index_cache
    if _java_index_cache is not None and _xml_index_cache is not None:
        return _java_index_cache, _xml_index_cache

    java_root = os.path.join(PROJECT_DIR, "src", "main", "java")
    xml_root = os.path.join(PROJECT_DIR, "src", "main", "resources")

    java_index = {
        "by_simple": {},
        "controllers": [],
        "service_impls": [],
        "mappers": [],
    }
    xml_index = {
        "by_namespace_simple": {},
        "by_filename": {},
    }

    print("【阶段 B】扫描 Java 文件建立索引...")
    if os.path.isdir(java_root):
        for root, _, files in os.walk(java_root):
            for fname in files:
                if not fname.endswith(".java"):
                    continue
                full_path = os.path.join(root, fname)
                try:
                    content = open(full_path, encoding="utf-8", errors="ignore").read()
                except Exception:
                    content = ""

                m_pkg = JAVA_PACKAGE_RE.search(content)
                pkg = m_pkg.group(1) if m_pkg else ""
                m_cls = JAVA_CLASS_RE.search(content)
                if not m_cls:
                    continue
                simple_name = m_cls.group(2)

                java_index["by_simple"][simple_name] = {
                    "path": full_path,
                    "content": content,
                    "package": pkg,
                }

                rel_path = full_path.replace("\\", "/")
                if "@RestController" in content or "@Controller" in content:
                    java_index["controllers"].append(full_path)
                if fname.endswith("ServiceImpl.java") or "/service/impl/" in rel_path:
                    java_index["service_impls"].append(full_path)
                if fname.endswith("Mapper.java") or "/mapper/" in rel_path:
                    java_index["mappers"].append(full_path)
    else:
        print(f"  ⚠️ Java 源码目录不存在: {java_root}")

    print("【阶段 B】扫描 XML Mapper 建立索引...")
    if os.path.isdir(xml_root):
        for root, _, files in os.walk(xml_root):
            for fname in files:
                if not fname.endswith(".xml"):
                    continue
                full_path = os.path.join(root, fname)
                try:
                    content = open(full_path, encoding="utf-8", errors="ignore").read()
                except Exception:
                    content = ""

                m_ns = XML_MAPPER_NS_RE.search(content)
                if m_ns:
                    ns = m_ns.group(1)
                    simple = ns.split(".")[-1]
                    xml_index["by_namespace_simple"].setdefault(simple, []).append(full_path)

                xml_index["by_filename"].setdefault(fname, []).append(full_path)
    else:
        print(f"  ⚠️ 资源目录不存在: {xml_root}")

    _java_index_cache = java_index
    _xml_index_cache = xml_index
    return java_index, xml_index


def build_dependency_graphs(java_index):
    """
    阶段 C：构建依赖图（ServiceImpl <-> Service, ServiceImpl <-> Mapper, Controller <-> Service/Impl）
    """
    impl_to_services: Dict[str, Set[str]] = {}
    service_to_impls: Dict[str, Set[str]] = {}
    impl_to_mappers: Dict[str, Set[str]] = {}
    mapper_to_impls: Dict[str, Set[str]] = {}
    controller_to_services: Dict[str, Set[str]] = {}
    service_to_controllers: Dict[str, Set[str]] = {}

    by_simple = java_index["by_simple"]

    implements_re = re.compile(r'class\s+(\w+)\s+[^{]*\bimplements\s+([^<{]+)[{]', re.S)
    ctor_re_template = r'%s\s*\(([^)]*)\)'

    # ServiceImpl -> Service
    for impl_path in java_index["service_impls"]:
        impl_name = os.path.splitext(os.path.basename(impl_path))[0]
        impl_content = by_simple.get(impl_name, {}).get("content", "")
        services: Set[str] = set()

        for m in implements_re.finditer(impl_content):
            if m.group(1) != impl_name:
                continue
            impl_list = m.group(2)
            for item in impl_list.split(","):
                svc = item.strip().split("<")[0].strip()
                if svc:
                    services.add(svc)

        if not services and impl_name.endswith("ServiceImpl"):
            cand = impl_name[:-len("ServiceImpl")] + "Service"
            if cand in by_simple:
                services.add(cand)

        if services:
            impl_to_services.setdefault(impl_name, set()).update(services)
            for s in services:
                service_to_impls.setdefault(s, set()).add(impl_name)

    # ServiceImpl -> Mapper
    # 1) 字段/构造器注入 Mapper
    field_inject_re = re.compile(
        r'(?:@Autowired|@Resource)?\s*(?:private|protected|public|final)?\s*([\w<>]+Mapper)\s+(\w+)\s*;',
        re.S,
    )
    # 2) MyBatis-Plus 等基类继承：class XxxServiceImpl extends ServiceImpl<SomeMapper, Entity>
    extends_mapper_re = re.compile(
        r'class\s+(\w+)\s+extends\s+[A-Za-z0-9_$.]+\s*<\s*([\w<>]+Mapper)\b',
        re.S,
    )

    for impl_path in java_index["service_impls"]:
        impl_name = os.path.splitext(os.path.basename(impl_path))[0]
        impl_content = by_simple.get(impl_name, {}).get("content", "")
        mappers: Set[str] = set()

        # 1) 字段注入 Mapper
        for m in field_inject_re.finditer(impl_content):
            type_name = m.group(1)
            simple = type_name.split("<")[0].strip()
            mappers.add(simple)

        # 2) 构造器注入 Mapper
        ctor_re = re.compile(ctor_re_template % impl_name)
        for m in ctor_re.finditer(impl_content):
            params_text = m.group(1)
            for p in params_text.split(","):
                p = p.strip()
                if not p:
                    continue
                tokens = p.split()
                if len(tokens) < 2:
                    continue
                p_type = tokens[-2]
                if "Mapper" in p_type:
                    simple = p_type.split("<")[0].strip()
                    mappers.add(simple)

        # 3) 继承 ServiceImpl<XXXMapper, Entity> 形式的 Mapper 关联
        for m in extends_mapper_re.finditer(impl_content):
            cls_name = m.group(1)
            if cls_name != impl_name:
                continue
            type_name = m.group(2)
            simple = type_name.split("<")[0].strip()
            if simple:
                mappers.add(simple)

        if mappers:
            impl_to_mappers.setdefault(impl_name, set()).update(mappers)
            for mm in mappers:
                mapper_to_impls.setdefault(mm, set()).add(impl_name)

    # Controller -> Service / Impl
    field_service_re = re.compile(
        r'(?:@Autowired|@Resource)?\s*(?:private|protected|public|final)?\s*([\w<>]+Service(?:Impl)?)\s+(\w+)\s*;',
        re.S,
    )
    for ctrl_path in java_index["controllers"]:
        ctrl_name = os.path.splitext(os.path.basename(ctrl_path))[0]
        ctrl_content = by_simple.get(ctrl_name, {}).get("content", "")
        services: Set[str] = set()

        for m in field_service_re.finditer(ctrl_content):
            t = m.group(1)
            simple = t.split("<")[0].strip()
            services.add(simple)

        ctor_re = re.compile(ctor_re_template % ctrl_name)
        for m in ctor_re.finditer(ctrl_content):
            params_text = m.group(1)
            for p in params_text.split(","):
                p = p.strip()
                if not p:
                    continue
                tokens = p.split()
                if len(tokens) < 2:
                    continue
                p_type = tokens[-2]
                if "Service" in p_type:
                    simple = p_type.split("<")[0].strip()
                    services.add(simple)

        if services:
            controller_to_services.setdefault(ctrl_name, set()).update(services)
            for s in services:
                service_to_controllers.setdefault(s, set()).add(ctrl_name)

    print("【阶段 C】依赖图构建完成：")
    print(f"  - impl_to_services: {len(impl_to_services)} 条")
    print(f"  - impl_to_mappers: {len(impl_to_mappers)} 条")
    print(f"  - controller_to_services: {len(controller_to_services)} 条")

    return (
        impl_to_services,
        service_to_impls,
        impl_to_mappers,
        mapper_to_impls,
        controller_to_services,
        service_to_controllers,
    )


def resolve_affected_controllers(
    changed: Dict[str, Set[str]],
    java_index,
    xml_index,
    impl_to_services,
    service_to_impls,
    impl_to_mappers,
    mapper_to_impls,
    controller_to_services,
    service_to_controllers,
):
    """
    阶段 D：根据变更 + 依赖图推导受影响 Controller
    """
    by_simple = java_index["by_simple"]

    # 受影响 Mapper simpleName
    affected_mapper_simple: Set[str] = set()
    for path in changed["mapper"]:
        simple = os.path.splitext(os.path.basename(path))[0]
        affected_mapper_simple.add(simple)
    for path in changed["mapper_xml"]:
        fname = os.path.basename(path)
        simple_from_name = os.path.splitext(fname)[0]
        if simple_from_name.endswith("Mapper"):
            affected_mapper_simple.add(simple_from_name)
        # 通过 namespace 匹配
        for simple, paths in xml_index.get("by_namespace_simple", {}).items():
            if path in paths:
                affected_mapper_simple.add(simple)

    # 受影响 ServiceImpl
    affected_impl: Set[str] = set()
    for path in changed["service_impl"]:
        impl = os.path.splitext(os.path.basename(path))[0]
        affected_impl.add(impl)
    for m in affected_mapper_simple:
        for impl in mapper_to_impls.get(m, []):
            affected_impl.add(impl)

    # 受影响 Service
    affected_services: Set[str] = set()
    for impl in affected_impl:
        for s in impl_to_services.get(impl, []):
            affected_services.add(s)
    for path in changed["service"]:
        s = os.path.splitext(os.path.basename(path))[0]
        affected_services.add(s)

    # 受影响 Controller
    affected_controllers_simple: Set[str] = set()
    forced_controllers_simple: Set[str] = set()
    reasons: Dict[str, List[str]] = {}

    for path in changed["controller"]:
        simple = os.path.splitext(os.path.basename(path))[0]
        affected_controllers_simple.add(simple)
        reasons.setdefault(simple, []).append("Controller 文件自身发生变更")

    for svc in affected_services:
        for ctrl in service_to_controllers.get(svc, []):
            affected_controllers_simple.add(ctrl)
            forced_controllers_simple.add(ctrl)
            reasons.setdefault(ctrl, []).append(
                f"Service {svc} 受影响，注入到 Controller {ctrl}"
            )

    for impl in affected_impl:
        for ctrl, svcs in controller_to_services.items():
            if impl in svcs:
                affected_controllers_simple.add(ctrl)
                forced_controllers_simple.add(ctrl)
                reasons.setdefault(ctrl, []).append(
                    f"ServiceImpl {impl} 受影响，被 Controller {ctrl} 直接注入"
                )

    affected_controller_files: Set[str] = set()
    forced_controller_files: Set[str] = set()
    for simple, info in by_simple.items():
        if info["path"] in java_index["controllers"] and simple in affected_controllers_simple:
            affected_controller_files.add(info["path"])
            if simple in forced_controllers_simple:
                forced_controller_files.add(info["path"])

    print("【阶段 D】受影响 Controller 推导结果：")
    print(f"  - 受影响 Mapper: {sorted(affected_mapper_simple)}")
    print(f"  - 受影响 ServiceImpl: {sorted(affected_impl)}")
    print(f"  - 受影响 Service: {sorted(affected_services)}")
    print(f"  - 受影响 Controller 文件: {len(affected_controller_files)} 个")

    return affected_controller_files, forced_controller_files, reasons


def list_all_controllers(java_index) -> List[str]:
    """返回项目中所有 Controller 文件路径"""
    return list(java_index.get("controllers", []))


# ================== 代码片段提取 & 影响报告 ==================

def extract_method_snippets_by_keywords(
    file_path: str,
    keywords: List[str],
    max_methods: int = 20,
) -> str:
    """按方法粒度截取包含任意关键字的代码片段（基于 AST）。"""
    try:
        content = open(file_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""

    if javalang is None:
        lines = content.splitlines()
        return "\n".join(lines[:80])

    try:
        tree = javalang.parse.parse(content)
    except Exception:
        lines = content.splitlines()
        return "\n".join(lines[:80])

    snippets: List[str] = []

    for _, cls in tree.filter(javalang.tree.ClassDeclaration):
        for method in getattr(cls, "methods", []):
            if len(snippets) >= max_methods:
                break
            pos = getattr(method, "position", None)
            if not pos:
                continue
            start = _offset_from_line_col(content, pos.line, pos.column)
            end = find_method_body_end(content, start)
            if end == -1:
                continue
            block = content[start : end + 1]
            if any(k in block for k in keywords):
                snippets.append(block.strip() + "\n")

    if not snippets:
        lines = content.splitlines()
        return "\n".join(lines[:80])
    return "\n\n".join(snippets)


def extract_mapper_java_snippet(file_path: str, max_lines: int = 120) -> str:
    try:
        content = open(file_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""
    lines = content.splitlines()
    return "\n".join(lines[:max_lines])


def extract_xml_snippet(file_path: str, max_lines: int = 120) -> str:
    try:
        content = open(file_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""
    lines = content.splitlines()
    return "\n".join(lines[:max_lines])


def _find_changed_methods_in_impl(
    impl_path: str, content: str, base: str, head: str
) -> Dict[str, str]:
    """
    返回 ServiceImpl 中发生变更的方法:
      { method_name -> 方法源码块 }
    """
    changed_lines = get_changed_lines(impl_path, base, head)
    if not changed_lines or javalang is None:
        return {}

    try:
        tree = javalang.parse.parse(content)
    except Exception as e:
        print(f"  ⚠️ AST 解析 ServiceImpl 失败，跳过方法级影响分析: {e}")
        return {}

    impl_simple = os.path.splitext(os.path.basename(impl_path))[0]
    changed_methods: Dict[str, str] = {}

    for _, cls in tree.filter(javalang.tree.ClassDeclaration):
        if getattr(cls, "name", None) != impl_simple:
            continue
        for method in getattr(cls, "methods", []):
            pos = getattr(method, "position", None)
            if not pos:
                continue
            start = _offset_from_line_col(content, pos.line, pos.column)
            end = find_method_body_end(content, start)
            if end == -1:
                continue
            method_line = line_no(content, start)
            method_end_line = line_no(content, end)
            if any(method_line <= l <= method_end_line for l in changed_lines):
                block = content[start : end + 1]
                changed_methods[getattr(method, "name", "")] = block

    return changed_methods


def _find_controller_mappings_for_service_method(
    impl_name: str,
    service_method_name: str,
    java_index,
    controller_to_services,
    service_to_controllers,
) -> List[Dict[str, str]]:
    """
    查找在“注入了指定 Service/Impl”的 Controller 中，
    调用了指定 Service 方法的方法及其 HTTP Mapping 与请求体信息。
    返回列表元素结构:
      {"controller": ..., "controller_method": ..., "http_method": ..., "path": ..., "body": Optional[str]}
    """
    results: List[Dict[str, str]] = []

    # 1. 找到 impl 对应的 Service 接口名集合
    services: Set[str] = set()
    # impl_name 本身可能直接被注入到 Controller 中
    # 这里不依赖 service_to_impls，而是直接使用 controller_to_services 做一次遍历
    for ctrl, svcs in controller_to_services.items():
        if impl_name in svcs:
            services.update(svcs)
    # 如果阶段 C 中已推导出 impl_to_services，可以通过它补充 services
    # 但此函数当前未直接持有 impl_to_services，为保持简单仅依赖 controller_to_services + service_to_controllers

    # 2. 基于 Service 接口名，通过 service_to_controllers 找候选 Controller simple name
    candidate_controllers: Set[str] = set()
    for svc in services:
        candidate_controllers |= service_to_controllers.get(svc, set())

    # 3. 如果依赖图没有给出候选 Controller，则退化为全量 Controller 扫描
    if not candidate_controllers:
        for simple, meta in java_index["by_simple"].items():
            if meta["path"] in java_index.get("controllers", []):
                candidate_controllers.add(simple)

    if javalang is None:
        return results

    for ctrl_simple in candidate_controllers:
        info = java_index["by_simple"].get(ctrl_simple)
        if not info:
            continue
        content = info["content"]

        try:
            tree = javalang.parse.parse(content)
        except Exception as e:
            print(f"  ⚠️ AST 解析 Controller {ctrl_simple} 失败，跳过: {e}")
            continue

        for _, cls in tree.filter(javalang.tree.ClassDeclaration):
            if getattr(cls, "name", None) != ctrl_simple:
                continue

            # 类级别前缀
            class_prefix = ""
            for ann in getattr(cls, "annotations", []):
                mapping = _ast_extract_mapping_from_annotation(ann)
                if mapping:
                    _, p = mapping
                    class_prefix = p or class_prefix

            for method in getattr(cls, "methods", []):
                # 是否调用了目标 Service 方法
                invoked = False
                for _, inv in method.filter(javalang.tree.MethodInvocation):
                    if getattr(inv, "member", None) == service_method_name:
                        invoked = True
                        break
                if not invoked:
                    continue

                method_mappings: List[Tuple[str, str]] = []
                for ann in getattr(method, "annotations", []):
                    mapping = _ast_extract_mapping_from_annotation(ann)
                    if mapping:
                        method_mappings.append(mapping)

                if not method_mappings:
                    continue

                # 提取方法上的参数信息（包括 @RequestBody DTO 名）
                params = _ast_extract_method_params(method)
                body_type = params.get("body")

                for http_method, path in method_mappings:
                    if http_method == "REQUEST":
                        http_method = "GET"
                    full_path = combine_path(class_prefix, path or "/")
                    controller_method_name = getattr(method, "name", "")

                    results.append(
                        {
                            "controller": ctrl_simple,
                            "controller_method": controller_method_name,
                            "http_method": http_method,
                            "path": full_path or "/",
                            # 链路解析时也能拿到 RequestBody DTO，例如 CabinetInformationVO
                            "body": body_type or None,
                        }
                    )
    return results


def _find_mapper_methods_for_service_method(
    impl_name: str,
    service_method_name: str,
    impl_to_mappers: Dict[str, Set[str]],
    java_index,
    xml_index,
) -> Dict[str, Dict[str, Dict[str, List[str]]]]:
    """
    使用 AST 在 ServiceImpl 的指定方法中查找 Mapper 方法调用，并关联到 Mapper 接口与 XML SQL。
    若 AST 精确映射失败，会回退到基于方法名交集的正则兜底逻辑。
    返回:
      {
        mapperSimple: {
           "methods": {"snippets": [java 方法签名片段...]},
           "xml_sql": {"snippets": [xml 片段...]}
        },
        ...
      }
    """
    result: Dict[str, Dict[str, Dict[str, List[str]]]] = {}
    mapper_types = impl_to_mappers.get(impl_name, set())
    if not mapper_types:
        return {}

    impl_meta = java_index["by_simple"].get(impl_name)
    if not impl_meta:
        return {}

    content = impl_meta.get("content") or ""
    if not content:
        return {}

    ast_ok = javalang is not None

    mapper_to_methods: Dict[str, Set[str]] = {}

    if ast_ok:
        try:
            tree = javalang.parse.parse(content)
        except Exception as e:
            print(f"  ⚠️ AST 解析 ServiceImpl {impl_name} 失败，将回退到正则 Mapper 分析: {e}")
            ast_ok = False

    if ast_ok:
        # 1. 在 ServiceImpl 类中找到 mapper 字段名 -> mapper 类型 的映射
        var_to_mapper_type: Dict[str, str] = {}

        for _, cls in tree.filter(javalang.tree.ClassDeclaration):
            if getattr(cls, "name", None) != impl_name:
                continue
            for field in getattr(cls, "fields", []):
                t = getattr(field, "type", None)
                type_name = getattr(t, "name", "") if t else ""
                simple_type = type_name.split("<")[0] if type_name else ""
                if simple_type not in mapper_types:
                    continue
                for decl in getattr(field, "declarators", []):
                    var_name = getattr(decl, "name", None)
                    if var_name:
                        var_to_mapper_type[var_name] = simple_type

            # 2. 在目标 Service 方法中查找对这些 mapper 字段的调用
            for method in getattr(cls, "methods", []):
                if getattr(method, "name", None) != service_method_name:
                    continue
                for _, inv in method.filter(javalang.tree.MethodInvocation):
                    qualifier = getattr(inv, "qualifier", None)
                    member = getattr(inv, "member", None)
                    if not member:
                        continue
                    mapper_simple = var_to_mapper_type.get(qualifier) if qualifier else None
                    if not mapper_simple:
                        # qualifier 无法解析到字段时，暂不使用 AST 精确映射
                        continue
                    mapper_to_methods.setdefault(mapper_simple, set()).add(member)

            break  # 只处理匹配的 impl_name 类

    # 若 AST 未找到任何 mapper 调用，回退到旧的“方法名交集”逻辑
    if not mapper_to_methods:
        # 在整个 impl 的文本中用旧逻辑辅助一把
        for mapper_simple in mapper_types:
            java_meta = java_index["by_simple"].get(mapper_simple)
            if not java_meta:
                continue
            java_content = java_meta["content"]
            called_methods = set(re.findall(r'\b(\w+)\s*\(', content))
            mapper_methods = set(re.findall(r'\b(\w+)\s*\(', java_content))
            method_names = called_methods & mapper_methods
            if method_names:
                mapper_to_methods[mapper_simple] = method_names

    # 3. 基于 mapper_to_methods 构造 Java / XML 片段
    for mapper_simple, method_names in mapper_to_methods.items():
        java_meta = java_index["by_simple"].get(mapper_simple)
        if not java_meta:
            continue
        java_content = java_meta["content"]
        java_lines = java_content.splitlines()

        java_snippets: List[str] = []
        for mn in sorted(method_names):
            for idx, line in enumerate(java_lines):
                if mn in line and "(" in line:
                    start = max(0, idx - 2)
                    end = min(len(java_lines), idx + 8)
                    java_snippets.append("\n".join(java_lines[start:end]))
                    break

        xml_snippets: List[str] = []
        xml_paths = xml_index.get("by_namespace_simple", {}).get(mapper_simple, [])
        for xml_path in xml_paths:
            try:
                xml_content = open(xml_path, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for mn in method_names:
                m_tag = re.search(
                    rf'<(select|insert|update|delete)[^>]*\sid\s*=\s*"{re.escape(mn)}"[^>]*>',
                    xml_content,
                )
                if not m_tag:
                    continue
                start = m_tag.start()
                end_tag = f"</{m_tag.group(1)}>"
                end = xml_content.find(end_tag, start)
                if end == -1:
                    end = start + 400
                else:
                    end += len(end_tag)
                xml_snippet = xml_content[start:end]
                xml_snippets.append(xml_snippet.strip())

            if not xml_snippets:
                lines = xml_content.splitlines()
                xml_snippets.append("\n".join(lines[:80]))

        if java_snippets or xml_snippets:
            result[mapper_simple] = {
                "methods": {"snippets": java_snippets},
                "xml_sql": {"snippets": xml_snippets},
            }

    return result


def _extract_controller_method_code(
    controller_simple: str,
    method_name: str,
    java_index,
) -> str:
    """
    基于 AST 从 Controller 源码中提取指定方法的完整代码块。
    """
    if javalang is None:
        return ""

    meta = java_index["by_simple"].get(controller_simple)
    if not meta:
        return ""

    content = meta.get("content") or ""
    if not content:
        return ""

    try:
        tree = javalang.parse.parse(content)
    except Exception:
        return ""

    for _, cls in tree.filter(javalang.tree.ClassDeclaration):
        if getattr(cls, "name", None) != controller_simple:
            continue
        for method in getattr(cls, "methods", []):
            if getattr(method, "name", None) != method_name:
                continue
            pos = getattr(method, "position", None)
            if not pos:
                continue
            start = _offset_from_line_col(content, pos.line, pos.column)
            end = find_method_body_end(content, start)
            if end == -1:
                continue
            return content[start : end + 1].strip()

    return ""


def generate_impact_report(
    changed: Dict[str, Set[str]],
    affected_controller_files: Set[str],
    forced_controller_files: Set[str],
    affected_reason: Dict[str, List[str]],
    java_index,
    xml_index,
    impl_to_mappers: Dict[str, Set[str]],
    controller_to_services: Dict[str, Set[str]],
    service_to_controllers: Dict[str, Set[str]],
    base_commit: Optional[str],
    head_commit: Optional[str],
):
    """阶段 E：生成影响链路 markdown 报告（仅输出方法级影响链路）"""
    mode = AI_IMPACT_OUTPUT_MODE
    print(f"【阶段 E】生成影响链路报告 ({mode}) -> {IMPACT_REPORT_FILE}")

    lines: List[str] = []
    lines.append("# AI 接口方法级影响链路报告")
    lines.append("")

    if not (base_commit and head_commit):
        lines.append("⚠️ 未提供 BASE_COMMIT / HEAD_COMMIT，无法计算方法级变更")
    else:
        for svc_path in sorted(changed.get("service_impl", [])):
            impl_simple = os.path.splitext(os.path.basename(svc_path))[0]
            meta = java_index["by_simple"].get(impl_simple)
            if not meta:
                continue
            impl_content = meta["content"]
            changed_methods = _find_changed_methods_in_impl(
                svc_path, impl_content, base_commit, head_commit
            )
            if not changed_methods:
                continue

            rel_svc = os.path.relpath(svc_path, PROJECT_DIR)
            for method_name, method_code in changed_methods.items():
                lines.append(
                    f"## Service 方法: `{impl_simple}.{method_name}` (`{rel_svc}`)"
                )

                # 对应 Controller 接口（包含请求体 DTO 信息与完整 JSON 请求体）
                ctrl_mappings = _find_controller_mappings_for_service_method(
                    impl_simple, method_name, java_index, controller_to_services, service_to_controllers
                )
                if ctrl_mappings:
                    lines.append("**对应 Controller 接口：**")
                    for m in ctrl_mappings:
                        body_info = m.get("body")
                        if body_info:
                            lines.append(
                                f"- `{m['http_method']} {m['path']}` "
                                f"(`{m['controller']}.{m['controller_method']}`) "
                                f"Body: `{body_info}`"
                            )
                            # 解析并输出 Body 结构
                            dto_struct = _parse_dto_structure(body_info, java_index)
                            if dto_struct and dto_struct.get("fields"):
                                body_md = _format_dto_structure_markdown(dto_struct)
                                lines.append("")
                                lines.append(body_md)
                        else:
                            lines.append(
                                f"- `{m['http_method']} {m['path']}` "
                                f"(`{m['controller']}.{m['controller_method']}`)"
                            )
                    # 输出 Controller 方法代码
                    lines.append("")
                    lines.append("**Controller 方法代码：**")
                    for m in ctrl_mappings:
                        ctrl_code = _extract_controller_method_code(
                            m["controller"], m["controller_method"], java_index
                        )
                        if not ctrl_code:
                            continue
                        lines.append(f"```java")
                        lines.append(ctrl_code)
                        lines.append("```")
                else:
                    lines.append("- 未能通过静态分析定位 Controller（可能原因：接口注入 / 间接调用 / 方法封装）")

                # 对应 Mapper / XML
                mapper_info = _find_mapper_methods_for_service_method(
                    impl_simple, method_name, impl_to_mappers, java_index, xml_index
                )
                if mapper_info:
                    lines.append("**对应 Mapper 接口与 SQL：**")
                    for mapper_simple, payload in mapper_info.items():
                        lines.append(f"- Mapper: `{mapper_simple}`")
                        java_snips = payload["methods"].get("snippets") or []
                        xml_snips = payload["xml_sql"].get("snippets") or []
                        for js in java_snips:
                            lines.append("```java")
                            lines.append(js)
                            lines.append("```")
                        for xs in xml_snips:
                            lines.append("```xml")
                            lines.append(xs)
                            lines.append("```")
                else:
                    lines.append("- 未能通过静态分析解析出该方法中使用的 Mapper 方法 / SQL")

                # Service 方法代码本身
                lines.append("**Service 方法代码：**")
                lines.append("```java")
                lines.append(method_code.strip())
                lines.append("```")
                lines.append("")


    try:
        with open(IMPACT_REPORT_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"  ✅ 影响链路报告已生成: {IMPACT_REPORT_FILE}")
    except Exception as e:
        print(f"  ⚠️ 写入影响链路报告失败: {e}")


# ================== 上传影响链路报告 ==================

def upload_impact_report(report_file_path: str) -> bool:
    """
    上传影响链路报告到指定接口
    返回: True 表示上传成功，False 表示上传失败
    """
    if not requests:
        print("⚠️  requests 库未安装，无法上传影响链路报告")
        return False
    
    if not os.path.exists(report_file_path):
        print(f"⚠️  影响链路报告文件不存在: {report_file_path}")
        return False
    
    upload_url = "https://6166qiyy8859.vicp.fun/bailian/automaticTesting"
    
    try:
        print(f"\n【上传影响链路报告】")
        print(f"  文件路径: {report_file_path}")
        print(f"  上传地址: {upload_url}")
        
        # 读取报告文件
        with open(report_file_path, "rb") as f:
            files = {
                "files": (os.path.basename(report_file_path), f, "text/markdown")
            }
            
            # 发送 POST 请求上传文件
            response = requests.post(upload_url, files=files, timeout=30)
            
            if response.status_code == 200:
                print(f"  ✅ 影响链路报告上传成功")
                return True
            else:
                print(f"  ❌ 影响链路报告上传失败，状态码: {response.status_code}")
                try:
                    error_msg = response.text[:200]
                    print(f"  错误信息: {error_msg}")
                except:
                    pass
                return False
                
    except requests.exceptions.Timeout:
        print(f"  ❌ 上传超时")
        return False
    except requests.exceptions.ConnectionError:
        print(f"  ❌ 连接失败，无法连接到服务器")
        return False
    except Exception as e:
        print(f"  ❌ 上传异常: {str(e)}")
        return False




# ================== 主流程 ==================

def main():
    print("【阶段 0】读取 git 变更 & 基本信息")
    base = os.getenv("BASE_COMMIT")
    head = os.getenv("HEAD_COMMIT")
    print(f"  BASE_COMMIT={base}")
    print(f"  HEAD_COMMIT={head}")
    print(f"  AI_MAX_CHANGED_FILES={AI_MAX_CHANGED_FILES}, AI_MAX_CONTROLLERS={AI_MAX_CONTROLLERS}, AI_MAX_TESTCASES={AI_MAX_TESTCASES}")
    print(f"  AI_FALLBACK_MODE={AI_FALLBACK_MODE}, AI_IMPACT_OUTPUT_MODE={AI_IMPACT_OUTPUT_MODE}")

    diff_files = git_diff_files(base, head)
    if not diff_files:
        print("【AI 测试】未检测到 git 变更文件，退出")
        return

    # 是否需要退化（可能由多种原因叠加触发）
    fallback_needed = False

    if len(diff_files) > AI_MAX_CHANGED_FILES:
        print(f"  ⚠️ 变更文件数 {len(diff_files)} 超过阈值 {AI_MAX_CHANGED_FILES}，将触发退化策略: {AI_FALLBACK_MODE}")
        fallback_needed = True

    # 阶段 A：分类
    changed = classify_changed_files(diff_files)

    # 阶段 B：索引
    java_index, xml_index = build_project_index()

    # 阶段 C：依赖图
    (
        impl_to_services,
        service_to_impls,
        impl_to_mappers,
        mapper_to_impls,
        controller_to_services,
        service_to_controllers,
    ) = build_dependency_graphs(java_index)

    # 阶段 D：受影响 Controller 推导
    try:
        affected_controllers, forced_controllers, affected_reason = resolve_affected_controllers(
            changed,
            java_index,
            xml_index,
            impl_to_services,
            service_to_impls,
            impl_to_mappers,
            mapper_to_impls,
            controller_to_services,
            service_to_controllers,
        )
    except Exception as e:
        print(f"  ⚠️ 解析依赖失败，将触发退化策略: {e}")
        affected_controllers = set()
        forced_controllers = set()
        affected_reason = {}

    # 阶段 E：生成影响报告
    generate_impact_report(
        changed,
        affected_controllers,
        forced_controllers,
        affected_reason,
        java_index,
        xml_index,
        impl_to_mappers,
        controller_to_services,
        service_to_controllers,
        base,
        head,
    )
    print(f"【影响链路报告】路径: {IMPACT_REPORT_FILE}")

    # 上传影响链路报告到指定接口
    print(f"\n【阶段 F】上传影响链路报告")
    upload_success = upload_impact_report(IMPACT_REPORT_FILE)
    
    if upload_success:
        print("\n✅ 影响链路报告已上传完成")
    else:
        print("\n⚠️  影响链路报告上传失败，但报告文件已生成")
        print(f"   报告文件路径: {IMPACT_REPORT_FILE}")

if __name__ == "__main__":
    main()