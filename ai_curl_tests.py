#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 接口测试脚本 - 自动生成测试数据并验证接口

功能特性:
1. 自动检测 Controller 变更
2. 智能生成测试数据（根据字段名和类型推断）
3. 支持嵌套对象和循环引用检测
4. 支持从配置文件自定义测试数据
5. 自动等待服务部署完成
6. 执行 HTTP 请求并验证结果

测试数据生成规则:
- 根据字段名智能推断（如 email -> test@example.com, id -> 1）
- 支持常见 Java 类型（Date, LocalDateTime, BigDecimal 等）
- 支持嵌套 DTO 对象
- 可通过 test_data_config.json 自定义测试数据模板

配置文件示例 (test_data_config.json):
{
    "field_patterns": {
        ".*[Ii]d$": 100,
        ".*[Ee]mail$": "custom@test.com"
    },
    "dto_templates": {
        "UserDTO": {
            "name": "张三",
            "age": 25,
            "email": "zhangsan@example.com"
        }
    }
}
"""

import subprocess
import os
import re
import time
import json
import builtins
import copy
from typing import List, Set, Dict, Optional, Tuple
try:
    import requests
except ImportError:
    print("⚠️  警告: 未安装 requests 库，请运行: pip install requests")
    requests = None

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
            new_part = line.split()[2]  # +20,2
            start = int(new_part[1:].split(",")[0])
            length = int(new_part.split(",")[1]) if "," in new_part else 1
            for i in range(length):
                changed.add(start + i)
    return changed

# ================== Java 解析 ==================

MAPPING_RE = re.compile(
    r'@(GetMapping|PostMapping|PutMapping|DeleteMapping|RequestMapping)'
    r'(?:\s*\(\s*((?:[^()"]+|"[^"]*")+)?\))?',
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
    "BigDecimal": "100.00",
    "BigInteger": "100",
}

# 根据字段名推断测试数据的规则
FIELD_NAME_PATTERNS = {
    # ID 相关
    r'.*[Ii]d$': lambda: 1,
    r'^[Ii]d$': lambda: 1,
    
    # 邮箱
    r'.*[Ee]mail$': lambda: "test@example.com",
    r'^[Ee]mail$': lambda: "test@example.com",
    
    # 电话
    r'.*[Pp]hone$': lambda: "13800138000",
    r'.*[Mm]obile$': lambda: "13800138000",
    r'.*[Tt]el$': lambda: "010-12345678",
    
    # 名称
    r'.*[Nn]ame$': lambda: "测试名称",
    r'.*[Tt]itle$': lambda: "测试标题",
    
    # 代码
    r'.*[Cc]ode$': lambda: "TEST001",
    
    # 描述
    r'.*[Dd]esc.*': lambda: "测试描述",
    r'.*[Dd]escription$': lambda: "测试描述",
    
    # 地址
    r'.*[Aa]ddress$': lambda: "测试地址",
    
    # 日期时间
    r'.*[Dd]ate$': lambda: "2024-01-01",
    r'.*[Tt]ime$': lambda: "12:00:00",
    r'.*[Dd]atetime$': lambda: "2024-01-01 12:00:00",
    
    # 状态
    r'.*[Ss]tatus$': lambda: "ACTIVE",
    r'.*[Ss]tate$': lambda: "NORMAL",
    
    # 数量
    r'.*[Cc]ount$': lambda: 10,
    r'.*[Qq]uantity$': lambda: 10,
    r'.*[Nn]um.*': lambda: 100,
    
    # 价格/金额
    r'.*[Pp]rice$': lambda: 99.99,
    r'.*[Aa]mount$': lambda: 1000.00,
    r'.*[Mm]oney$': lambda: 500.00,
    
    # URL
    r'.*[Uu]rl$': lambda: "https://example.com",
    r'.*[Uu]ri$': lambda: "/api/test",
    
    # 备注
    r'.*[Rr]emark$': lambda: "测试备注",
    r'.*[Nn]ote$': lambda: "测试备注",
    
    # 排序
    r'.*[Oo]rder$': lambda: 1,
    r'.*[Ss]ort$': lambda: 1,
    
    # 是否
    r'.*[Ii]s[A-Z].*': lambda: True,  # is开头的是布尔值
    r'.*[Hh]as[A-Z].*': lambda: True,  # has开头的是布尔值
}

# Java 类型到测试数据的映射（包含更多类型）
JAVA_TYPE_SAMPLES = {
    **PRIMITIVE_SAMPLES,
    "Date": "2024-01-01T00:00:00",
    "LocalDate": "2024-01-01",
    "LocalDateTime": "2024-01-01T12:00:00",
    "LocalTime": "12:00:00",
    "Timestamp": "2024-01-01 12:00:00",
    "BigDecimal": "100.00",
    "BigInteger": "100",
    "UUID": "550e8400-e29b-41d4-a716-446655440000",
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
    field_inject_re = re.compile(
        r'(?:@Autowired|@Resource)?\s*(?:private|protected|public|final)?\s*([\w<>]+Mapper)\s+(\w+)\s*;',
        re.S,
    )
    for impl_path in java_index["service_impls"]:
        impl_name = os.path.splitext(os.path.basename(impl_path))[0]
        impl_content = by_simple.get(impl_name, {}).get("content", "")
        mappers: Set[str] = set()

        for m in field_inject_re.finditer(impl_content):
            type_name = m.group(1)
            simple = type_name.split("<")[0].strip()
            mappers.add(simple)

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
    """按方法粒度截取包含任意关键字的代码片段"""
    try:
        content = open(file_path, encoding="utf-8", errors="ignore").read()
    except Exception:
        return ""

    methods = list(METHOD_RE.finditer(content))
    snippets = []
    for m in methods:
        if len(snippets) >= max_methods:
            break
        start = m.start()
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
    if not changed_lines:
        return {}

    methods = list(METHOD_RE.finditer(content))
    changed_methods: Dict[str, str] = {}
    for m in methods:
        method_name = m.group(1)
        start = m.start()
        end = find_method_body_end(content, start)
        if end == -1:
            continue
        method_line = line_no(content, start)
        method_end_line = line_no(content, end)
        if any(method_line <= l <= method_end_line for l in changed_lines):
            block = content[start : end + 1]
            changed_methods[method_name] = block
    return changed_methods


def _find_controller_mappings_for_service_method(
    service_method_name: str,
    java_index,
) -> List[Dict[str, str]]:
    """
    查找在 Controller 中调用了指定 Service 方法的方法及其 HTTP Mapping。
    返回列表元素结构:
      {"controller": ..., "controller_method": ..., "http_method": ..., "path": ...}
    """
    results: List[Dict[str, str]] = []
    for ctrl_path in java_index.get("controllers", []):
        info = None
        # 通过 path 找 simple name 和内容
        for simple, meta in java_index["by_simple"].items():
            if meta["path"] == ctrl_path:
                info = meta
                ctrl_simple = simple
                break
        if not info:
            continue
        content = info["content"]
        class_prefix = ""
        class_mapping = CLASS_MAPPING_RE.search(content)
        if class_mapping:
            class_prefix = class_mapping.group(1)

        mappings = list(MAPPING_RE.finditer(content))
        methods = list(METHOD_RE.finditer(content))

        for m in mappings:
            method = find_next_method(m.end(), methods)
            if not method:
                continue
            start = method.start()
            end = find_method_body_end(content, start)
            if end == -1:
                continue
            body = content[start : end + 1]
            if service_method_name not in body:
                continue

            http_method = m.group(1).replace("Mapping", "").upper()
            path = parse_mapping_path(m.group(2))
            full_path = combine_path(class_prefix, path)
            controller_method_name = method.group(1)

            results.append(
                {
                    "controller": ctrl_simple,
                    "controller_method": controller_method_name,
                    "http_method": http_method,
                    "path": full_path or "/",
                }
            )
    return results


def _find_mapper_methods_for_service_method(
    impl_name: str,
    method_code: str,
    impl_to_mappers: Dict[str, Set[str]],
    java_index,
    xml_index,
) -> Dict[str, Dict[str, Dict[str, List[str]]]]:
    """
    在 ServiceImpl 的方法代码中，基于 impl_to_mappers 推断可能调用的 Mapper 方法及 XML SQL。
    返回:
      {
        mapperSimple: {
           "methods": [java 方法签名片段...],
           "xml_sql": [xml 片段...]
        },
        ...
      }
    """
    result: Dict[str, Dict[str, Dict[str, List[str]]]] = {}
    mappers = impl_to_mappers.get(impl_name, set())
    if not mappers:
        return {}

    for mapper_simple in mappers:
        java_meta = java_index["by_simple"].get(mapper_simple)
        if not java_meta:
            continue
        java_content = java_meta["content"]
        java_lines = java_content.splitlines()

        # 猜测变量名（简单驼峰）
        camel = mapper_simple[0].lower() + mapper_simple[1:] if mapper_simple else ""
        method_names: Set[str] = set()
        # 搜索 mapperVariable.method(...)
        if camel:
            pattern_var = re.compile(rf'\b{re.escape(camel)}\.(\w+)\s*\(')
            for mm in pattern_var.finditer(method_code):
                method_names.add(mm.group(1))
        # 搜索 MapperClass.method(...)
        pattern_cls = re.compile(rf'\b{re.escape(mapper_simple)}\.(\w+)\s*\(')
        for mm in pattern_cls.finditer(method_code):
            method_names.add(mm.group(1))

        if not method_names:
            continue

        java_snippets: List[str] = []
        for mn in sorted(method_names):
            # 在 Mapper.java 中找到方法名出现的位置，截取附近若干行
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
                # 简单匹配 id="methodName" 的 SQL 片段
                m_tag = re.search(
                    rf'<(select|insert|update|delete)[^>]*\sid\s*=\s*"{re.escape(mn)}"[^>]*>',
                    xml_content,
                )
                if not m_tag:
                    continue
                start = m_tag.start()
                # 粗略找到对应结束标签
                end_tag = f"</{m_tag.group(1)}>"
                end = xml_content.find(end_tag, start)
                if end == -1:
                    end = start + 400
                else:
                    end += len(end_tag)
                xml_snippet = xml_content[start:end]
                xml_snippets.append(xml_snippet.strip())

        if java_snippets or xml_snippets:
            result[mapper_simple] = {
                "methods": {"snippets": java_snippets},
                "xml_sql": {"snippets": xml_snippets},
            }

    return result


def generate_impact_report(
    changed: Dict[str, Set[str]],
    affected_controller_files: Set[str],
    forced_controller_files: Set[str],
    affected_reason: Dict[str, List[str]],
    java_index,
    xml_index,
    impl_to_mappers: Dict[str, Set[str]],
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

                # 对应 Controller 接口
                ctrl_mappings = _find_controller_mappings_for_service_method(
                    method_name, java_index
                )
                if ctrl_mappings:
                    lines.append("**对应 Controller 接口：**")
                    for m in ctrl_mappings:
                        lines.append(
                            f"- `{m['http_method']} {m['path']}` "
                            f"(`{m['controller']}.{m['controller_method']}`)"
                        )
                else:
                    lines.append("- 未找到直接调用该 Service 方法的 Controller 接口")

                # 对应 Mapper / XML
                mapper_info = _find_mapper_methods_for_service_method(
                    impl_simple, method_code, impl_to_mappers, java_index, xml_index
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
                    lines.append("- 未能解析出该方法中使用的 Mapper 方法 / SQL")

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

# ================== 测试数据配置 ==================

# 测试数据模板文件路径（可选）
TEST_DATA_CONFIG_FILE = os.path.join(PROJECT_DIR, "test_data_config.json")

def load_test_data_config() -> Dict:
    """
    从配置文件加载测试数据模板
    配置文件格式示例:
    {
        "field_patterns": {
            ".*[Ii]d$": 100,
            ".*[Ee]mail$": "custom@test.com"
        },
        "dto_templates": {
            "UserDTO": {
                "name": "张三",
                "age": 25
            }
        }
    }
    """
    config = {}
    if os.path.exists(TEST_DATA_CONFIG_FILE):
        try:
            with open(TEST_DATA_CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)
            print(f"  📋 已加载测试数据配置: {TEST_DATA_CONFIG_FILE}")
        except Exception as e:
            print(f"  ⚠️  加载测试数据配置失败: {e}")
    return config

# 全局测试数据配置
_test_data_config = None

def get_test_data_config() -> Dict:
    """获取测试数据配置（懒加载）"""
    global _test_data_config
    if _test_data_config is None:
        _test_data_config = load_test_data_config()
    return _test_data_config

# ================== RequestBody 示例生成 ==================

def find_dto_file(dto_name: str) -> Optional[str]:
    # 简单按类名查找文件
    try:
        output = run(f'find src -name "{dto_name}.java"')
    except Exception:
        return None
    return output.splitlines()[0] if output else None

def guess_sample_value(java_type: str, field_name: str = "") -> object:
    """
    根据Java类型和字段名生成测试数据
    优先级：配置文件 > 字段名模式 > Java类型
    """
    java_type = java_type.strip()
    base = java_type.replace("[]", "")
    base = base.split("<")[0].strip()
    
    config = get_test_data_config()
    
    # 1. 首先检查配置文件中的字段模式匹配（最高优先级）
    if field_name and "field_patterns" in config:
        for pattern, value in config["field_patterns"].items():
            if re.match(pattern, field_name):
                return value
    
    # 2. 根据字段名推断（使用内置模式）
    if field_name:
        for pattern, generator in FIELD_NAME_PATTERNS.items():
            if re.match(pattern, field_name):
                try:
                    return generator()
                except:
                    pass
    
    # 3. 根据Java类型生成
    if base in JAVA_TYPE_SAMPLES:
        return JAVA_TYPE_SAMPLES[base]
    
    # List/Set/Collection 类型
    if any(k in base for k in ("List", "Set", "Collection")):
        # 尝试提取泛型类型
        generic_match = re.search(r'<([^>]+)>', java_type)
        if generic_match:
            generic_type = generic_match.group(1).strip()
            # 生成一个元素的列表
            sample = guess_sample_value(generic_type, "")
            return [sample] if not isinstance(sample, str) or not sample.startswith("<") else []
        return []
    
    # Map 类型
    if "Map" in base:
        return {}
    
    # 枚举类型（通常以Enum结尾或全大写）
    if base.endswith("Enum") or base.isupper():
        return f"{base}_VALUE"
    
    # 其他自定义类型，返回占位符
    return f"<{base}>"

def build_body_dict(dto_name: Optional[str], visited: Optional[Set[str]] = None) -> dict:
    """
    构建请求体字典（用于实际请求）
    支持嵌套对象，避免循环引用
    支持从配置文件读取模板
    """
    if visited is None:
        visited = set()
    
    if not dto_name:
        return {"example": "replace_with_body"}
    
    # 避免循环引用
    if dto_name in visited:
        return {"_ref": dto_name}
    
    config = get_test_data_config()
    
    # 1. 优先使用配置文件中的 DTO 模板
    if "dto_templates" in config and dto_name in config["dto_templates"]:
        template = config["dto_templates"][dto_name]
        # 深拷贝模板，避免修改原始配置
        return copy.deepcopy(template)
    
    dto_file = find_dto_file(dto_name)
    if not dto_file:
        return {"example": f"replace_with_{dto_name}"}
    
    try:
        content = open(dto_file, encoding="utf-8", errors="ignore").read()
    except Exception:
        return {"example": f"replace_with_{dto_name}"}

    visited.add(dto_name)
    fields = FIELD_RE.findall(content)
    body_obj = {}
    
    for _, ftype, fname in fields:
        # 检查是否是自定义类型（非基本类型、非集合）
        base_type = ftype.strip().replace("[]", "").split("<")[0].strip()
        
        # 如果是基本类型或已知类型，直接生成
        if base_type in JAVA_TYPE_SAMPLES or any(k in base_type for k in ("List", "Set", "Collection", "Map")):
            body_obj[fname] = guess_sample_value(ftype, fname)
        else:
            # 可能是嵌套对象，尝试递归生成
            # 先尝试生成基本值
            sample = guess_sample_value(ftype, fname)
            if isinstance(sample, str) and sample.startswith("<") and sample.endswith(">"):
                # 是占位符，尝试作为嵌套对象处理
                nested_type = sample[1:-1]
                nested_obj = build_body_dict(nested_type, visited.copy())
                body_obj[fname] = nested_obj
            else:
                body_obj[fname] = sample
    
    visited.remove(dto_name)
    
    if not body_obj:
        return {"example": f"replace_with_{dto_name}"}
    return body_obj

def build_body_sample(dto_name: Optional[str]) -> str:
    """
    构建请求体 JSON 字符串（用于 curl 命令显示）
    """
    body_dict = build_body_dict(dto_name)
    
    # 序列化为 JSON 字符串
    items = []
    for k, v in body_dict.items():
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
        return '{"example": "<replace_with_body>"}'
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

# ================== 部署检测 ==================

def wait_for_deployment() -> bool:
    """
    等待服务部署完成，通过健康检查接口验证
    返回: True 表示服务已就绪，False 表示超时
    """
    if not requests:
        print("⚠️  requests 库未安装，跳过部署检测")
        return False
    
    # 支持多个健康检查路径（按优先级尝试）
    health_paths = [
        HEALTH_CHECK_PATH,
        "/health",
        "/actuator/health",
        "/"
    ]
    
    print(f"\n【部署检测】等待服务启动: {BASE_URL}")
    print(f"  最大等待时间: {DEPLOYMENT_WAIT_MAX} 秒")
    print(f"  检查间隔: {DEPLOYMENT_CHECK_INTERVAL} 秒")
    
    start_time = time.time()
    attempt = 0
    last_print_time = 0
    
    while time.time() - start_time < DEPLOYMENT_WAIT_MAX:
        attempt += 1
        elapsed = time.time() - start_time
        
        # 每10秒或前3次尝试时打印进度
        should_print = (elapsed - last_print_time >= 10) or (attempt <= 3)
        
        # 尝试所有健康检查路径
        for health_path in health_paths:
            health_url = f"{BASE_URL}{health_path}"
            try:
                response = requests.get(health_url, timeout=3)
                if response.status_code == 200:
                    print(f"  ✅ 服务已就绪 (耗时: {elapsed:.1f} 秒, 尝试: {attempt} 次)")
                    print(f"     健康检查路径: {health_path}")
                    return True
            except requests.exceptions.ConnectionError:
                # 连接错误，继续尝试下一个路径
                continue
            except requests.exceptions.Timeout:
                # 超时，继续尝试下一个路径
                continue
            except requests.exceptions.RequestException:
                # 其他请求异常，继续尝试下一个路径
                continue
        
        # 打印进度信息
        if should_print:
            print(f"  ⏳ 等待中... (已等待: {elapsed:.0f} 秒, 尝试: {attempt} 次)")
            last_print_time = elapsed
        
        time.sleep(DEPLOYMENT_CHECK_INTERVAL)
    
    print(f"  ❌ 服务启动超时 (已等待: {DEPLOYMENT_WAIT_MAX} 秒, 尝试: {attempt} 次)")
    print(f"     已尝试的健康检查路径: {', '.join(health_paths)}")
    return False

# ================== HTTP 请求测试 ==================

def generate_param_value(param_name: str, param_type: str = "String") -> str:
    """
    根据参数名和类型生成测试值
    """
    # 根据参数名推断
    for pattern, generator in FIELD_NAME_PATTERNS.items():
        if re.match(pattern, param_name):
            try:
                value = generator()
                # 转换为字符串
                if isinstance(value, bool):
                    return str(value).lower()
                return str(value)
            except:
                pass
    
    # 根据类型生成
    if "id" in param_name.lower():
        return "1"
    elif "code" in param_name.lower():
        return "TEST001"
    elif "name" in param_name.lower() or "title" in param_name.lower():
        return "测试值"
    elif "email" in param_name.lower():
        return "test@example.com"
    elif "phone" in param_name.lower() or "mobile" in param_name.lower():
        return "13800138000"
    elif "date" in param_name.lower():
        return "2024-01-01"
    elif "time" in param_name.lower():
        return "12:00:00"
    elif param_type.lower() in ("int", "integer", "long"):
        return "1"
    elif param_type.lower() in ("double", "float", "bigdecimal"):
        return "1.0"
    elif param_type.lower() == "boolean":
        return "true"
    else:
        return "test"

def build_request_data(http_method: str, full_path: str, params: Dict[str, object]) -> Tuple[str, Optional[dict], Optional[dict]]:
    """
    构建请求数据
    返回: (url, query_params, body_data)
    """
    path = full_path
    path_params = {}
    
    # 处理路径参数（根据参数名生成更合理的值）
    for pv in params["path"]:
        placeholder = "{" + pv + "}"
        if placeholder in path:
            # 根据参数名生成测试值
            path_params[pv] = generate_param_value(pv)
            path = path.replace(placeholder, path_params[pv])
    
    # 构建查询参数（根据参数名生成更合理的值）
    query_params = {}
    for q in params["query"]:
        query_params[q] = generate_param_value(q)
    
    url = f"{BASE_URL}{path}"
    if query_params:
        query_string = "&".join(f"{k}={v}" for k, v in query_params.items())
        url = f"{url}?{query_string}"
    
    # 构建请求体
    body_data = None
    if params["body"] and http_method in ("POST", "PUT", "PATCH"):
        body_data = build_body_dict(params["body"])
    
    return url, query_params if query_params else None, body_data

def send_test_request(http_method: str, full_path: str, params: Dict[str, object], method_name: str) -> Dict:
    """
    发送测试请求并返回结果
    返回: {
        "success": bool,
        "status_code": int,
        "response_time": float,
        "error": str,
        "response_preview": str
    }
    """
    if not requests:
        return {
            "success": False,
            "status_code": 0,
            "response_time": 0,
            "error": "requests 库未安装",
            "response_preview": ""
        }
    
    url, query_params, body_data = build_request_data(http_method, full_path, params)
    
    try:
        start_time = time.time()
        
        # 根据 HTTP 方法发送请求
        if http_method == "GET":
            response = requests.get(url, timeout=10)
        elif http_method == "POST":
            response = requests.post(url, json=body_data, timeout=10)
        elif http_method == "PUT":
            response = requests.put(url, json=body_data, timeout=10)
        elif http_method == "PATCH":
            response = requests.patch(url, json=body_data, timeout=10)
        elif http_method == "DELETE":
            response = requests.delete(url, timeout=10)
        else:
            return {
                "success": False,
                "status_code": 0,
                "response_time": 0,
                "error": f"不支持的 HTTP 方法: {http_method}",
                "response_preview": ""
            }
        
        response_time = (time.time() - start_time) * 1000  # 转换为毫秒
        
        # 截取响应预览（前200字符）
        try:
            response_preview = response.text[:200]
            if len(response.text) > 200:
                response_preview += "..."
        except:
            response_preview = "<无法读取响应>"
        
        # 判断是否成功（2xx 和 3xx 都视为可能成功，4xx/5xx 需要人工判断）
        is_success = 200 <= response.status_code < 400
        
        return {
            "success": is_success,
            "status_code": response.status_code,
            "response_time": response_time,
            "error": None,
            "response_preview": response_preview
        }
        
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "status_code": 0,
            "response_time": 0,
            "error": "请求超时",
            "response_preview": ""
        }
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "status_code": 0,
            "response_time": 0,
            "error": "连接失败，服务可能未启动",
            "response_preview": ""
        }
    except Exception as e:
        return {
            "success": False,
            "status_code": 0,
            "response_time": 0,
            "error": f"请求异常: {str(e)}",
            "response_preview": ""
        }

# ================== 核心解析逻辑 ==================

def parse_controller_precise(
    controllers: List[str],
    force_all_controllers: Optional[Set[str]] = None,
) -> List[Dict]:
    """
    解析 Controller 并返回测试用例列表
    返回: [{
        "file_path": str,
        "http_method": str,
        "full_path": str,
        "method_name": str,
        "params": Dict,
        "curl_cmd": str
    }, ...]
    """
    base = os.getenv("BASE_COMMIT")
    head = os.getenv("HEAD_COMMIT")
    test_cases: List[Dict] = []
    force_all_controllers = force_all_controllers or set()

    for file_path in controllers:
        print(f"\n【解析】{file_path}")

        content = open(file_path, encoding="utf-8", errors="ignore").read()
        if file_path in force_all_controllers:
            changed_lines: Set[int] = set()
            print("  ℹ️  此 Controller 由 Service/Mapper/XML 变更牵连，强制全量接口测试")
        else:
            changed_lines = get_changed_lines(file_path, base, head)

        class_prefix = ""
        class_mapping = CLASS_MAPPING_RE.search(content)
        if class_mapping:
            class_prefix = class_mapping.group(1)

        mappings = list(MAPPING_RE.finditer(content))
        methods = list(METHOD_RE.finditer(content))

        affected = []

        if file_path in force_all_controllers:
            # 强制全量：所有 Mapping 对应的方法都纳入
            for m in mappings:
                method = find_next_method(m.end(), methods)
                if method:
                    affected.append((m, method))
        else:
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
            
            # 添加到测试用例列表
            test_cases.append({
                "file_path": file_path,
                "http_method": http_method,
                "full_path": full_path or "/",
                "method_name": method_name,
                "params": params,
                "curl_cmd": test_cmd
            })
    
    return test_cases

# ================== 自动测试执行 ==================

def run_tests(test_cases: List[Dict]) -> Dict:
    """
    执行所有测试用例
    返回: {
        "total": int,
        "success": int,
        "failed": int,
        "results": List[Dict]
    }
    """
    if not test_cases:
        print("\n【测试执行】无测试用例")
        return {"total": 0, "success": 0, "failed": 0, "results": []}
    
    print(f"\n【测试执行】开始执行 {len(test_cases)} 个测试用例")
    print("=" * 60)
    
    results = []
    success_count = 0
    failed_count = 0
    
    for idx, test_case in enumerate(test_cases, 1):
        http_method = test_case["http_method"]
        full_path = test_case["full_path"]
        method_name = test_case["method_name"]
        params = test_case["params"]
        
        print(f"\n[{idx}/{len(test_cases)}] {http_method} {full_path}")
        print(f"  方法: {method_name}")
        
        result = send_test_request(http_method, full_path, params, method_name)
        results.append({
            **test_case,
            **result
        })
        
        if result["success"]:
            success_count += 1
            print(f"  ✅ 成功 - 状态码: {result['status_code']}, 响应时间: {result['response_time']:.0f}ms")
        else:
            failed_count += 1
            status_info = f"状态码: {result['status_code']}" if result['status_code'] > 0 else ""
            error_info = f"错误: {result['error']}" if result['error'] else ""
            print(f"  ❌ 失败 - {status_info} {error_info}".strip())
        
        if result.get("response_preview"):
            preview = result["response_preview"].replace("\n", " ")
            print(f"  响应预览: {preview[:100]}...")
    
    print("\n" + "=" * 60)
    print(f"【测试总结】")
    print(f"  总计: {len(test_cases)}")
    print(f"  成功: {success_count}")
    print(f"  失败: {failed_count}")
    print(f"  成功率: {success_count / len(test_cases) * 100:.1f}%")
    
    return {
        "total": len(test_cases),
        "success": success_count,
        "failed": failed_count,
        "results": results
    }

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
        service_to_controllers,
        base,
        head,
    )
    print(f"【影响链路报告】路径: {IMPACT_REPORT_FILE}")

    # 阈值与退化策略决定最终要测试的 Controller
    if len(affected_controllers) > AI_MAX_CONTROLLERS:
        print(f"  ⚠️ 受影响 Controller 数 {len(affected_controllers)} 超过阈值 {AI_MAX_CONTROLLERS}，启用退化策略")
        fallback_needed = True

    if not affected_controllers:
        # 没有通过依赖图推导出任何 Controller，也走退化策略
        print("  ⚠️ 未能推导出受影响 Controller，将根据退化策略决定范围")
        fallback_needed = True

    if fallback_needed:
        if AI_FALLBACK_MODE == "changed_only":
            final_controllers = sorted(changed.get("controller", []))
            force_all = set(final_controllers)
            print(f"  ℹ️  退化为仅测试直接变更的 Controller，共 {len(final_controllers)} 个")
        else:
            # all
            final_controllers = list_all_controllers(java_index)
            force_all = set(final_controllers)
            print(f"  ℹ️  退化为全量 Controller 测试，共 {len(final_controllers)} 个")
    else:
        final_controllers = sorted(affected_controllers)
        # 由 service/mapper/XML 间接影响的 controller 强制全量
        force_all = set(forced_controllers)
        print(f"  ✅ 本次受影响 Controller 文件数: {len(final_controllers)}")

    if not final_controllers:
        print("【AI 测试】未找到需要测试的 Controller，退出")
        return

    print("\n【阶段 1】最终待测 Controller 列表：")
    for c in final_controllers:
        mark = " (force_all)" if c in force_all else ""
        print(f"  - {c}{mark}")

    print("\n【阶段 2】方法 / Mapping / Body 级精准定位")
    test_cases = parse_controller_precise(final_controllers, force_all)

    if not test_cases:
        print("\n【AI 测试】未找到需要测试的接口")
        return

    if len(test_cases) > AI_MAX_TESTCASES:
        print(f"  ⚠️ 生成的测试用例数 {len(test_cases)} 超过阈值 {AI_MAX_TESTCASES}，将仅执行前 {AI_MAX_TESTCASES} 条")
        test_cases = test_cases[:AI_MAX_TESTCASES]

    print(f"\n【阶段 3】等待部署完成")
    # 检查是否跳过部署等待（通过环境变量控制）
    skip_deployment_check = os.getenv("SKIP_DEPLOYMENT_CHECK", "").lower() in ("true", "1", "yes")
    
    if skip_deployment_check:
        print("  ⏭️  跳过部署检测（SKIP_DEPLOYMENT_CHECK=true）")
    else:
        if not wait_for_deployment():
            print("\n⚠️  警告: 服务未就绪")
            # 非交互模式下自动继续，交互模式下询问用户
            if os.getenv("CI") or os.getenv("NON_INTERACTIVE"):
                print("  非交互模式: 继续执行测试（可能会失败）")
            else:
                user_input = input("是否继续执行测试? (y/n): ").strip().lower()
                if user_input != 'y':
                    print("测试已取消")
                    return

    print("\n【阶段 4】执行自动测试")
    test_results = run_tests(test_cases)

    print("\n✅ 测试流程完成")
    
    # 如果有失败的测试，返回非零退出码
    if test_results["failed"] > 0:
        exit(1)

if __name__ == "__main__":
    main()