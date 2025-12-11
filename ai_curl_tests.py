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
PROJECT_DIR = "/home/gitlab-runner/builds/_TQ32fEV/0/wuzhuoyan/ocr-customs-java"
BASE_URL = "http://localhost:9979/ocr-service"  # 可按需修改
HEALTH_CHECK_PATH = "/actuator/health"  # 健康检查路径，可根据实际情况修改
DEPLOYMENT_WAIT_MAX = 300  # 最大等待部署时间（秒）
DEPLOYMENT_CHECK_INTERVAL = 5  # 检查间隔（秒）
LOG_DIR = "/home/gitlab-runner/running/ocr-customs-java"
LOG_FILE = os.getenv("AI_TEST_LOG_FILE", os.path.join(LOG_DIR, "ai_test.log"))


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


def build_request_data(http_method: str, full_path: str, params: Dict[str, object]) -> Tuple[
    str, Optional[dict], Optional[dict]]:
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

def parse_controller_precise(controllers: List[str]) -> List[Dict]:
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
    test_cases = []

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
            print(f"  响应预览: {preview}")

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
    print("【阶段 1】检测 Controller 变更")
    controllers = find_changed_controllers()

    if not controllers:
        print("【AI 测试】无 Controller 变更")
        return

    for c in controllers:
        print("  -", c)

    print("\n【阶段 2】方法 / Mapping / Body 级精准定位")
    test_cases = parse_controller_precise(controllers)

    if not test_cases:
        print("\n【AI 测试】未找到需要测试的接口")
        return

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