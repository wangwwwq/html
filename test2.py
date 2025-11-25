import pyautogui
import pyperclip
import random
import time
from fastapi import FastAPI, Request
from pydantic import BaseModel
import uvicorn

app = FastAPI()

class TaskRequest(BaseModel):
    chatName: str
    message: str

@app.post("/run_task")
async def run_task_api(task: TaskRequest):
    run_task(task.chatName, task.message)
    # sleep 一会儿确保任务完成
    time.sleep(10)
    return {"status": "success"}


def run_task(chatName: str, message: str):
    screenWidth, screenHeight = pyautogui.size()
    currentMouseX, currentMouseY = pyautogui.position()

    # 目标范围是（160, 40）到（360, 60）
    target_x = random.randint(86, 235)
    target_y = random.randint(26, 45)

    pyautogui.sleep(10)

    move_mouse_slowly_to_target(currentMouseX, currentMouseY, target_x, target_y, screenWidth, screenHeight)
    pyautogui.click()
    pyautogui.sleep(random.uniform(1, 5))

    pyautogui.write(chatName, interval=random.uniform(0.2, 0.5))
    pyautogui.sleep(random.uniform(0.2, 0.5))
    pyautogui.press('enter')
    pyautogui.sleep(random.uniform(0.2, 0.5))
    pyautogui.press('enter')

    pyautogui.sleep(random.uniform(1, 2))
    pyperclip.copy(message)
    time.sleep(random.uniform(0.5, 1))
    pyautogui.hotkey('ctrl', 'v')

    time.sleep(random.uniform(1, 5))
    pyautogui.press('enter')



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
