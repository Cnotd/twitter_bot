#!/bin/bash
# Owly X 情报监控系统 - Linux 服务器启动脚本

set -e

cd "$(dirname "$0")"

echo "========================================"
echo "  Owly X 情报监控系统"
echo "========================================"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python3 未安装"
    exit 1
fi

# 创建虚拟环境（首次运行）
if [ ! -d ".venv" ]; then
    echo "[SETUP] 创建虚拟环境..."
    python3 -m venv .venv
fi

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
echo "[SETUP] 安装依赖..."
pip install -r tw_requirements.txt -q

# 检查 .env
if [ ! -f ".env" ]; then
    echo "[WARN] .env 文件不存在，将使用 tw_config.py 中的默认配置"
fi

# 启动守护模式
echo "[START] 启动情报监控（每5分钟一轮）..."
nohup python3 tw_main.py --daemon --interval 300 > logs/daemon.log 2>&1 &
PID=$!
echo "[OK] 进程已启动，PID: $PID"
echo "[OK] 日志文件: logs/daemon.log"
echo ""
echo "停止服务: kill $PID"
echo "查看日志: tail -f logs/daemon.log"
