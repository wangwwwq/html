import asyncio
import hashlib
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import contextlib
import pyautogui
import pyperclip
import random
import time
from typing import List, Optional, Dict, Any
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

app = FastAPI()
task_queue: asyncio.Queue = asyncio.Queue()
worker_task: Optional[asyncio.Task] = None

# 固定密钥仅用于服务端验签，需妥善保管，避免硬编码泄露
SECRET_KEY = "xT7!rP9^Lq2@aZ5#sF8$yM3%vB1*eW6&Dn4^jC0$hR9!tK"
logger = logging.getLogger("wechat_task")
if not logger.handlers:
    log_dir = Path(__file__).resolve().parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_format = "[%(asctime)s] [%(levelname)s] %(message)s"
    formatter = logging.Formatter(log_format, "%Y-%m-%d %H:%M:%S")

    file_handler = TimedRotatingFileHandler(
        log_dir / "wechat_task.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
        utc=False,
    )
    file_handler.suffix = "%Y-%m-%d.log"
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def build_response(status: int = 200, code: str = "200", msg: str = "SUCCESS"):
    """统一响应结构，便于客户端解析"""
    return {"status": status, "code": str(code), "msg": msg}


# 请求模型：增加 timestamp 与 sign（mentions 不参与验签）
class SecureTaskRequest(BaseModel):
    chatName: str
    message: str
    mentions: List[str] = []
    timestamp: int
    sign: str
    sourceApp: str


def _normalize_param_value(value):
    """把列表/字典转成稳定字符串，避免不同客户端序列化差异"""
    if isinstance(value, list):
        return ",".join(map(str, value))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def make_sign(params):
    """按 key 排序后拼接字符串 + 秘钥，最后计算 SHA256 大写摘要（跳过 sign 与 mentions）"""
    sorted_items = sorted(
        (k, _normalize_param_value(v))
        for k, v in params.items()
        if k not in {"sign", "mentions"}
    )
    base_str = "&".join(f"{k}={v}" for k, v in sorted_items)
    base_str += f"&key={SECRET_KEY}"
    logger.info("签名原文: %s", base_str)
    return hashlib.sha256(base_str.encode("utf-8")).hexdigest().upper()


def verify_sign(params):
    """校验时间戳防重放，并核对签名是否一致"""
    try:
        ts = int(params.get("timestamp", 0))
    except (TypeError, ValueError):
        logger.warning("timestamp 解析失败: %s", params.get("timestamp"))
        return False
    now = int(time.time())
    if abs(now - ts) > 300:
        logger.warning("请求超时: ts=%s now=%s", ts, now)
        return False
    sign_val = params.get("sign")
    if not sign_val:
        logger.warning("缺少 sign 字段")
        return False
    expected = make_sign(params)
    if sign_val != expected:
        logger.warning("签名不匹配: 期望=%s 实际=%s", expected, sign_val)
        logger.warning("参数详情: %s", json.dumps(params, ensure_ascii=False))
        return False
    return True

@app.on_event("startup")
async def startup_event():
    """启动后台协程，保证任务按 FIFO 队列消费"""
    global worker_task
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(task_worker())
        logger.info("任务处理协程已启动，等待队列任务")


@app.on_event("shutdown")
async def shutdown_event():
    """优雅关闭后台协程，确保资源释放"""
    global worker_task
    if worker_task:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        worker_task = None
        logger.info("任务处理协程已关闭")


@app.post("/run_task")
async def run_task_api(task: SecureTaskRequest):
    payload = task.model_dump()
    if not verify_sign(payload):
        # 按约定始终返回 200，具体状态通过自定义字段告知调用方
        return JSONResponse(
            status_code=200,
            content=build_response(status=401, code="401", msg="验签失败或请求超时"),
        )
    logger.info(
        "验签通过，收到任务 chatName=%s mentions=%d msg_len=%d sourceApp=%s",
        task.chatName,
        len(task.mentions),
        len(task.message),
        task.sourceApp,
    )
    await task_queue.put(
        {
            "chatName": task.chatName,
            "message": task.message,
            "mentions": task.mentions,
            "timestamp": task.timestamp,
            "sourceApp": task.sourceApp,
        }
    )
    queue_size = task_queue.qsize()
    logger.info("任务入队完成 chatName=%s 当前排队=%d", task.chatName, queue_size)
    return build_response(msg=f"任务已入队，前方队列剩余 {max(queue_size - 1, 0)} 个")


async def task_worker():
    """后台协程：保持先进先出顺序逐个执行任务"""
    while True:
        task_data: Dict[str, Any] = await task_queue.get()
        chat_name = task_data["chatName"]
        try:
            loop = asyncio.get_running_loop()
            logger.info("开始处理队首任务 chatName=%s 剩余队列=%d", chat_name, task_queue.qsize())
            await loop.run_in_executor(
                None,
                run_task,
                chat_name,
                task_data["message"],
                task_data["mentions"],
                task_data.get("sourceApp", "unknown"),
            )
            await asyncio.sleep(5)
            logger.info("任务完成 chatName=%s 当前等待=%d", chat_name, task_queue.qsize())
        except Exception as exc:
            logger.exception("任务执行失败 chatName=%s: %s", chat_name, exc)
        finally:
            task_queue.task_done()


def run_task(chatName: str, message: str, mentions: List[str], sourceApp: str):
    logger.info(
        "开始执行任务 chatName=%s mention_count=%d sourceApp=%s",
        chatName,
        len(mentions),
        sourceApp,
    )
    screenWidth, screenHeight = pyautogui.size()
    logger.info("当前屏幕分辨率 %sx%s", screenWidth, screenHeight)

    # 目标范围是（160, 40）到（360, 60）
    target_x = random.randint(86, 235)
    target_y = random.randint(26, 45)
    logger.info("目标输入区域随机坐标: (%s, %s)", target_x, target_y)

    # 初始停顿让界面充分加载，避免误触
    pyautogui.sleep(10)
    currentMouseX, currentMouseY = pyautogui.position()
    logger.info("当前鼠标位置: (%s, %s)", currentMouseX, currentMouseY)

    # 人工缓慢移动到输入框附近再点击
    move_mouse_slowly_to_target(currentMouseX, currentMouseY, target_x, target_y, screenWidth, screenHeight)
    pyautogui.click()
    pyautogui.sleep(random.uniform(1, 5))  # 点击后稍作停顿，模仿观察界面

    # 输入群聊名并进入会话
    pyautogui.write(chatName, interval=random.uniform(0.2, 0.5))
    pyautogui.sleep(random.uniform(0.2, 0.5))
    pyautogui.press('enter')
    pyautogui.sleep(random.uniform(0.2, 0.5))
    pyautogui.press('enter')
    pyautogui.sleep(random.uniform(0.2, 0.5))

    # 粘贴正文前停顿，模拟思考
    pyautogui.sleep(random.uniform(1, 2))
    pyperclip.copy(message)
    time.sleep(random.uniform(0.5, 1))
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(random.uniform(0.5, 1))

    # 逐个确认@名单
    confirm_mentions(mentions)

    # 最后发送消息
    time.sleep(random.uniform(0.8, 1.5))
    pyautogui.press('enter')
    pyautogui.sleep(random.uniform(0.5, 1.2))
    logger.info("任务执行完成 chatName=%s sourceApp=%s", chatName, sourceApp)


def confirm_mentions(mentions: List[str]):
    """依次输入@并回车确认每位成员，自动跳过重复名字且通过粘贴完成输入"""
    seen = set()
    if not mentions:
        logger.info("本次任务无 @ 名单，直接跳过")
        return
    logger.info("开始处理 @ 名单，总计 %d 人", len(mentions))
    for name in mentions:
        key = name.strip()
        if not key or key in seen:
            if not key:
                logger.debug("检测到空昵称，跳过")
            else:
                logger.debug("检测到重复昵称 %s，跳过", key)
            continue
        seen.add(key)
        # 每个人名之间加随机停顿（1~2.5秒），模拟观察名单的行为
        pyautogui.sleep(random.uniform(1, 2.5))
        # 用粘贴替代手打，降低输入错误概率
        pyperclip.copy(key)
        pyautogui.write(" ", interval=random.uniform(0.05, 0.1))
        pyautogui.write("@", interval=random.uniform(0.05, 0.1))
        pyautogui.sleep(random.uniform(0.1, 0.2))
        pyautogui.hotkey('ctrl', 'v')
        pyautogui.sleep(random.uniform(0.2, 0.5))
        # 每次确认一个@后都敲回车并等待1~2.5秒，保持人工节奏
        pyautogui.press('enter')
        pyautogui.sleep(random.uniform(1, 2.5))


# 缓慢且随机地移动鼠标到目标区域内的随机点，保证首步也平滑
def move_mouse_slowly_to_target(start_x, start_y, end_x, end_y, screenWidth, screenHeight):
    x, y = start_x, start_y
    tolerance = 3  # 接近目标坐标的容忍范围，避免最后一步突然跳动
    max_steps = 200
    steps = 0
    while (abs(x - end_x) > tolerance or abs(y - end_y) > tolerance) and steps < max_steps:
        dx = end_x - x
        dy = end_y - y
        # 增大每次移动的最大步长，提升速度
        step = random.randint(12, 30)
        distance = (dx ** 2 + dy ** 2) ** 0.5
        if distance < step:
            x, y = end_x, end_y
        else:
            ratio = step / distance
            # 加入随机抖动
            jitter_x = random.randint(-3, 3)
            jitter_y = random.randint(-3, 3)
            x += int(dx * ratio) + jitter_x
            y += int(dy * ratio) + jitter_y
        # 保证不超出屏幕
        x = max(0, min(screenWidth - 1, x))
        y = max(0, min(screenHeight - 1, y))
        pyautogui.moveTo(x, y)
        steps += 1
        # 缩短每步的等待时间，仍保留人手感
        time.sleep(random.uniform(0.005, 0.02))
    # 最后确保在目标区域内
    pyautogui.moveTo(end_x, end_y)


if __name__ == "__main__":
    uvicorn.run("test2:app", host="0.0.0.0", port=8000, reload=True)
