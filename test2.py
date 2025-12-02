# ============================================================================
# 导入依赖模块
# ============================================================================
import asyncio  # 异步IO支持，用于任务队列和协程处理
import hashlib  # 哈希算法库，用于签名计算
import json  # JSON数据处理
import logging  # 日志记录
from logging.handlers import TimedRotatingFileHandler  # 按时间轮转的日志处理器
import contextlib  # 上下文管理工具
import pyautogui  # 自动化GUI操作库，用于模拟鼠标键盘
import pyperclip  # 剪贴板操作库
import random  # 随机数生成，用于模拟人工操作的随机性
import time  # 时间相关操作
from typing import List, Optional, Dict, Any  # 类型注解
from pathlib import Path  # 路径处理
from fastapi import FastAPI  # FastAPI框架，用于构建REST API
from fastapi.responses import JSONResponse  # JSON响应封装
from pydantic import BaseModel  # 数据模型验证
import uvicorn  # ASGI服务器，用于运行FastAPI应用

# ============================================================================
# 全局变量初始化
# ============================================================================
app = FastAPI()  # FastAPI应用实例
task_queue: asyncio.Queue = asyncio.Queue()  # 异步任务队列，FIFO顺序处理任务
worker_task: Optional[asyncio.Task] = None  # 后台任务处理协程的引用

# ============================================================================
# 安全配置
# ============================================================================
# 固定密钥仅用于服务端验签，需妥善保管，避免硬编码泄露
# 生产环境建议从环境变量或配置文件读取
SECRET_KEY = "xT7!rP9^Lq2@aZ5#sF8$yM3%vB1*eW6&Dn4^jC0$hR9!tK"

# ============================================================================
# 日志系统配置
# ============================================================================
logger = logging.getLogger("wechat_task")
# 避免重复添加处理器（防止重载时重复初始化）
if not logger.handlers:
    # 创建日志目录（如果不存在）
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # 定义日志格式：时间戳 + 日志级别 + 消息内容
    log_format = "[%(asctime)s] [%(levelname)s] %(message)s"
    formatter = logging.Formatter(log_format, "%Y-%m-%d %H:%M:%S")

    # 文件处理器：按天轮转，保留14天历史日志
    file_handler = TimedRotatingFileHandler(
        log_dir / "wechat_task.log",
        when="midnight",  # 每天午夜轮转
        backupCount=14,  # 保留14个备份文件
        encoding="utf-8",  # UTF-8编码支持中文
        utc=False,  # 使用本地时区
    )
    file_handler.suffix = "%Y-%m-%d.log"  # 备份文件命名格式
    file_handler.setFormatter(formatter)

    # 控制台处理器：同时输出到终端
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    # 设置日志级别并添加处理器
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# ============================================================================
# 工具函数
# ============================================================================
def build_response(status: int = 200, code: str = "200", msg: str = "SUCCESS"):
    """
    构建统一的API响应结构，便于客户端解析
    
    Args:
        status: HTTP状态码（实际返回仍为200，此字段用于业务状态）
        code: 业务状态码（字符串类型）
        msg: 响应消息
    
    Returns:
        dict: 包含status、code、msg的字典
    """
    return {"status": status, "code": str(code), "msg": msg}


# ============================================================================
# 数据模型定义
# ============================================================================
# 请求模型：增加 timestamp 与 sign（mentions 不参与验签）
class SecureTaskRequest(BaseModel):
    """
    安全的任务请求模型，包含签名验证所需字段
    
    Attributes:
        chatName: 目标群聊名称
        message: 要发送的消息内容
        mentions: @成员列表（不参与签名计算）
        timestamp: 请求时间戳（Unix时间戳，秒级）
        sign: 请求签名（SHA256摘要）
        sourceApp: 来源应用标识
    """
    chatName: str
    message: str
    mentions: List[str] = []
    timestamp: int
    sign: str
    sourceApp: str


# ============================================================================
# 签名验证相关函数
# ============================================================================
def _normalize_param_value(value):
    """
    将参数值标准化为稳定的字符串格式，避免不同客户端序列化差异导致签名不一致
    
    Args:
        value: 待标准化的值（可能是字符串、列表、字典等）
    
    Returns:
        str: 标准化后的字符串
    """
    if isinstance(value, list):
        # 列表转为逗号分隔的字符串
        return ",".join(map(str, value))
    if isinstance(value, dict):
        # 字典转为JSON字符串（排序键，确保一致性）
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def make_sign(params):
    """
    生成请求签名
    
    签名算法：
    1. 排除 sign 和 mentions 字段
    2. 按 key 字母序排序
    3. 拼接为 "key1=value1&key2=value2&key=SECRET_KEY" 格式
    4. 计算 SHA256 摘要并转为大写
    
    Args:
        params: 参数字典
    
    Returns:
        str: SHA256 签名（大写十六进制字符串）
    """
    # 过滤掉不参与签名的字段，并按key排序
    sorted_items = sorted(
        (k, _normalize_param_value(v))
        for k, v in params.items()
        if k not in {"sign", "mentions"}
    )
    # 拼接参数字符串
    base_str = "&".join(f"{k}={v}" for k, v in sorted_items)
    # 追加密钥
    base_str += f"&key={SECRET_KEY}"
    logger.info("签名原文: %s", base_str)
    # 计算SHA256摘要并转为大写
    return hashlib.sha256(base_str.encode("utf-8")).hexdigest().upper()


def verify_sign(params):
    """
    验证请求签名和时间戳
    
    验证流程：
    1. 检查时间戳是否在有效期内（±5分钟）
    2. 检查签名字段是否存在
    3. 重新计算签名并与请求中的签名对比
    
    Args:
        params: 参数字典（包含timestamp和sign）
    
    Returns:
        bool: 验证通过返回True，否则返回False
    """
    # 解析并验证时间戳
    try:
        ts = int(params.get("timestamp", 0))
    except (TypeError, ValueError):
        logger.warning("timestamp 解析失败: %s", params.get("timestamp"))
        return False
    
    # 检查时间戳是否在5分钟内（防重放攻击）
    now = int(time.time())
    if abs(now - ts) > 300:
        logger.warning("请求超时: ts=%s now=%s", ts, now)
        return False
    
    # 检查签名字段是否存在
    sign_val = params.get("sign")
    if not sign_val:
        logger.warning("缺少 sign 字段")
        return False
    
    # 重新计算签名并对比
    expected = make_sign(params)
    if sign_val != expected:
        logger.warning("签名不匹配: 期望=%s 实际=%s", expected, sign_val)
        logger.warning("参数详情: %s", json.dumps(params, ensure_ascii=False))
        return False
    
    return True

# ============================================================================
# FastAPI 生命周期事件
# ============================================================================
@app.on_event("startup")
async def startup_event():
    """
    应用启动时初始化后台任务处理协程
    
    确保任务按 FIFO（先进先出）顺序处理，避免并发冲突
    """
    global worker_task
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(task_worker())
        logger.info("任务处理协程已启动，等待队列任务")


@app.on_event("shutdown")
async def shutdown_event():
    """
    应用关闭时优雅停止后台协程
    
    确保正在处理的任务完成，避免数据丢失
    """
    global worker_task
    if worker_task:
        worker_task.cancel()
        # 忽略取消异常，正常关闭
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        worker_task = None
        logger.info("任务处理协程已关闭")


# ============================================================================
# API 端点
# ============================================================================
@app.post("/run_task")
async def run_task_api(task: SecureTaskRequest):
    """
    接收任务请求的API端点
    
    处理流程：
    1. 验证请求签名和时间戳
    2. 将任务加入队列
    3. 返回队列状态
    
    Args:
        task: 任务请求对象（包含签名验证信息）
    
    Returns:
        JSONResponse: 统一格式的响应（验签失败返回401状态，成功返回队列信息）
    """
    payload = task.model_dump()
    
    # 验证签名和时间戳
    if not verify_sign(payload):
        # 按约定始终返回 200，具体状态通过自定义字段告知调用方
        return JSONResponse(
            status_code=200,
            content=build_response(status=401, code="401", msg="验签失败或请求超时"),
        )
    
    # 记录任务信息
    logger.info(
        "验签通过，收到任务 chatName=%s mentions=%d msg_len=%d sourceApp=%s",
        task.chatName,
        len(task.mentions),
        len(task.message),
        task.sourceApp,
    )
    
    # 将任务加入队列
    await task_queue.put(
        {
            "chatName": task.chatName,
            "message": task.message,
            "mentions": task.mentions,
            "timestamp": task.timestamp,
            "sourceApp": task.sourceApp,
        }
    )
    
    # 返回队列状态
    queue_size = task_queue.qsize()
    logger.info("任务入队完成 chatName=%s 当前排队=%d", task.chatName, queue_size)
    return build_response(msg=f"任务已入队，前方队列剩余 {max(queue_size - 1, 0)} 个")


# ============================================================================
# 任务处理逻辑
# ============================================================================
async def task_worker():
    """
    后台任务处理协程
    
    从队列中按 FIFO 顺序取出任务并执行，确保任务串行处理避免冲突。
    使用线程池执行器运行同步的GUI操作，避免阻塞事件循环。
    """
    while True:
        # 从队列中获取任务（如果队列为空会阻塞等待）
        task_data: Dict[str, Any] = await task_queue.get()
        chat_name = task_data["chatName"]
        
        try:
            # 获取当前事件循环
            loop = asyncio.get_running_loop()
            logger.info("开始处理队首任务 chatName=%s 剩余队列=%d", chat_name, task_queue.qsize())
            
            # 在线程池中执行同步的GUI操作（避免阻塞异步事件循环）
            await loop.run_in_executor(
                None,  # 使用默认线程池
                run_task,  # 要执行的函数
                chat_name,
                task_data["message"],
                task_data["mentions"],
                task_data.get("sourceApp", "unknown"),
            )
            
            # 任务完成后等待5秒，给界面一些缓冲时间
            await asyncio.sleep(5)
            logger.info("任务完成 chatName=%s 当前等待=%d", chat_name, task_queue.qsize())
            
        except Exception as exc:
            # 记录异常但不中断工作循环
            logger.exception("任务执行失败 chatName=%s: %s", chat_name, exc)
        finally:
            # 标记任务完成（用于task_done计数）
            task_queue.task_done()


def run_task(chatName: str, message: str, mentions: List[str], sourceApp: str):
    """
    执行微信消息发送任务的核心函数
    
    通过模拟鼠标键盘操作，自动化完成以下步骤：
    1. 点击搜索框
    2. 输入群聊名称并进入会话
    3. 粘贴消息内容
    4. @指定成员
    5. 发送消息
    
    所有操作都加入随机延迟，模拟人工操作的自然节奏。
    
    Args:
        chatName: 目标群聊名称
        message: 要发送的消息内容
        mentions: 需要@的成员列表
        sourceApp: 来源应用标识（用于日志追踪）
    """
    logger.info(
        "开始执行任务 chatName=%s mention_count=%d sourceApp=%s",
        chatName,
        len(mentions),
        sourceApp,
    )
    
    # 获取屏幕分辨率，用于鼠标移动边界检查
    screenWidth, screenHeight = pyautogui.size()
    logger.info("当前屏幕分辨率 %sx%s", screenWidth, screenHeight)

    # 在搜索框区域内随机选择点击位置（增加操作的自然性）
    # 目标范围是（86, 26）到（235, 45）
    target_x = random.randint(86, 235)
    target_y = random.randint(26, 45)
    logger.info("目标输入区域随机坐标: (%s, %s)", target_x, target_y)

    # 初始停顿让界面充分加载，避免误触其他元素
    pyautogui.sleep(10)
    
    # 获取当前鼠标位置，作为移动起点
    currentMouseX, currentMouseY = pyautogui.position()
    logger.info("当前鼠标位置: (%s, %s)", currentMouseX, currentMouseY)

    # 缓慢移动鼠标到搜索框位置（模拟人工操作）
    move_mouse_slowly_to_target(currentMouseX, currentMouseY, target_x, target_y, screenWidth, screenHeight)
    
    # 点击搜索框
    pyautogui.click()
    pyautogui.sleep(random.uniform(1, 5))  # 点击后稍作停顿，模仿观察界面

    # 输入群聊名称（使用随机输入间隔模拟打字速度）
    pyautogui.write(chatName, interval=random.uniform(0.2, 0.5))
    pyautogui.sleep(random.uniform(0.2, 0.5))
    
    # 第一次回车：确认搜索
    pyautogui.press('enter')
    pyautogui.sleep(random.uniform(0.2, 0.5))
    
    # 第二次回车：进入会话（如果搜索结果唯一）
    pyautogui.press('enter')
    pyautogui.sleep(random.uniform(0.2, 0.5))

    # 粘贴消息正文（使用剪贴板提高效率，避免输入错误）
    pyautogui.sleep(random.uniform(1, 2))  # 粘贴前停顿，模拟思考
    pyperclip.copy(message)
    time.sleep(random.uniform(0.5, 1))
    pyautogui.hotkey('ctrl', 'v')  # 粘贴消息
    time.sleep(random.uniform(0.5, 1))

    # 逐个处理@成员列表
    confirm_mentions(mentions)

    # 最后发送消息
    time.sleep(random.uniform(0.8, 1.5))  # 发送前稍作停顿
    pyautogui.press('enter')
    pyautogui.sleep(random.uniform(0.5, 1.2))
    logger.info("任务执行完成 chatName=%s sourceApp=%s", chatName, sourceApp)


def confirm_mentions(mentions: List[str]):
    """
    依次@指定成员并确认
    
    处理流程：
    1. 过滤空值和重复值
    2. 对每个成员：输入空格+@符号，粘贴成员名，回车确认
    3. 每个操作之间加入随机延迟，模拟人工操作
    
    Args:
        mentions: 需要@的成员昵称列表
    """
    seen = set()  # 用于去重，避免重复@同一人
    
    # 如果没有@名单，直接返回
    if not mentions:
        logger.info("本次任务无 @ 名单，直接跳过")
        return
    
    logger.info("开始处理 @ 名单，总计 %d 人", len(mentions))
    
    for name in mentions:
        # 去除首尾空格
        key = name.strip()
        
        # 跳过空值和重复值
        if not key or key in seen:
            if not key:
                logger.debug("检测到空昵称，跳过")
            else:
                logger.debug("检测到重复昵称 %s，跳过", key)
            continue
        
        seen.add(key)
        
        # 每个人名之间加随机停顿（1~2.5秒），模拟观察名单的行为
        pyautogui.sleep(random.uniform(1, 2.5))
        
        # 使用粘贴方式输入成员名，降低输入错误概率
        pyperclip.copy(key)
        
        # 输入空格和@符号（触发@功能）
        pyautogui.write(" ", interval=random.uniform(0.05, 0.1))
        pyautogui.write("@", interval=random.uniform(0.05, 0.1))
        pyautogui.sleep(random.uniform(0.1, 0.2))
        
        # 粘贴成员名
        pyautogui.hotkey('ctrl', 'v')
        pyautogui.sleep(random.uniform(0.2, 0.5))
        
        # 回车确认@该成员
        pyautogui.press('enter')
        pyautogui.sleep(random.uniform(1, 2.5))  # 每次确认后等待，保持人工节奏


# ============================================================================
# 鼠标控制函数
# ============================================================================
def move_mouse_slowly_to_target(start_x, start_y, end_x, end_y, screenWidth, screenHeight):
    """
    缓慢且平滑地移动鼠标到目标位置
    
    使用分步移动算法，模拟人工鼠标移动的自然轨迹：
    1. 计算当前位置到目标的方向和距离
    2. 每次移动随机步长（12-30像素）
    3. 加入随机抖动，使轨迹更自然
    4. 确保不超出屏幕边界
    5. 每步之间短暂延迟，保持平滑感
    
    Args:
        start_x: 起始X坐标
        start_y: 起始Y坐标
        end_x: 目标X坐标
        end_y: 目标Y坐标
        screenWidth: 屏幕宽度（用于边界检查）
        screenHeight: 屏幕高度（用于边界检查）
    """
    x, y = start_x, start_y
    tolerance = 3  # 接近目标坐标的容忍范围（像素），避免最后一步突然跳动
    max_steps = 200  # 最大移动步数，防止无限循环
    steps = 0
    
    # 循环移动直到接近目标或达到最大步数
    while (abs(x - end_x) > tolerance or abs(y - end_y) > tolerance) and steps < max_steps:
        # 计算到目标的距离向量
        dx = end_x - x
        dy = end_y - y
        
        # 每次移动的随机步长（12-30像素），提升移动速度
        step = random.randint(12, 30)
        
        # 计算到目标的直线距离
        distance = (dx ** 2 + dy ** 2) ** 0.5
        
        if distance < step:
            # 如果距离小于步长，直接移动到目标
            x, y = end_x, end_y
        else:
            # 按比例移动，并加入随机抖动使轨迹更自然
            ratio = step / distance
            jitter_x = random.randint(-3, 3)  # 随机抖动（-3到3像素）
            jitter_y = random.randint(-3, 3)
            x += int(dx * ratio) + jitter_x
            y += int(dy * ratio) + jitter_y
        
        # 确保坐标不超出屏幕边界
        x = max(0, min(screenWidth - 1, x))
        y = max(0, min(screenHeight - 1, y))
        
        # 执行鼠标移动
        pyautogui.moveTo(x, y)
        steps += 1
        
        # 每步之间短暂延迟（5-20毫秒），保持平滑感
        time.sleep(random.uniform(0.005, 0.02))
    
    # 最后确保鼠标精确移动到目标位置
    pyautogui.moveTo(end_x, end_y)


# ============================================================================
# 程序入口
# ============================================================================
if __name__ == "__main__":
    """
    启动FastAPI应用服务器
    
    配置说明：
    - host="0.0.0.0": 监听所有网络接口，允许外部访问
    - port=8000: 服务端口号
    - reload=True: 开发模式，代码修改后自动重载
    """
    uvicorn.run("test2:app", host="0.0.0.0", port=8000, reload=True)
