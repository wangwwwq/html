#!/usr/bin/env bash
set -e

JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64
MAVEN_HOME=/home/wangweiqing/apache-maven-3.8.8
export PATH=$JAVA_HOME/bin:$MAVEN_HOME/bin:$PATH

PROJECT_DIR="/home/wangweiqing/ocrai/ocr-customs-java"
SCRIPT_DIR="$PROJECT_DIR/scripts"
SERVICE_DIR="/home/wangweiqing/ocrai"

echo "================ 部署开始 ================"

echo "▶ Java & Maven 环境检查"
java -version
mvn -v

echo "▶ 清理旧的 Jar 包：$SERVICE_DIR"
rm -f "$SERVICE_DIR"/*.jar || true

cd "$PROJECT_DIR"

echo "▶ 更新源码并记录提交区间"
OLD_HEAD=$(git rev-parse HEAD)
git pull --ff-only
NEW_HEAD=$(git rev-parse HEAD)

export BASE_COMMIT="$OLD_HEAD"
export HEAD_COMMIT="$NEW_HEAD"

echo "▶ 代码变更区间："
echo "  BASE_COMMIT=$BASE_COMMIT"
echo "  HEAD_COMMIT=$HEAD_COMMIT"

echo "▶ Maven 打包（跳过测试）"
mvn clean package -DskipTests

JAR_NAME=$(ls target/*.jar | grep -v original | head -n 1)
[ -z "$JAR_NAME" ] && { echo "❌ 未找到 Jar"; exit 1; }

cp "$JAR_NAME" "$SERVICE_DIR/"
cd "$SERVICE_DIR"

echo "✅ 使用 Jar 包：$(basename "$JAR_NAME")"

if [ -f pid ]; then
  OLD_PID=$(cat pid)
  if ps -p "$OLD_PID" >/dev/null 2>&1; then
    echo "▶ 停止旧进程 PID=$OLD_PID"
    kill "$OLD_PID"
    sleep 2
    ps -p "$OLD_PID" && kill -9 "$OLD_PID"
  fi
fi

echo "▶ 启动新服务"
nohup java -Xmx4G -Duser.timezone=Asia/Shanghai \
  -jar "$(basename "$JAR_NAME")" > output.log 2>&1 &

echo $! > pid
echo "✅ 新服务已启动，PID=$(cat pid)"

sleep 5

echo "================ AI 测试阶段 ================"
python3 "$SCRIPT_DIR/ai_curl_tests.py" \
  || echo "⚠️ AI 测试失败（未阻断部署）"

echo "================ 部署完成 ================"
tail -n 50 output.log
