#!/usr/bin/env bash
# =============================================================================
# AI 科研论文工作台 —— 一键部署脚本
#
# 本地模式（默认）：
#   bash deploy.sh
#   → docker compose up -d --build → 轮询 /api/health → 打印可访问的 URL
#
# 远程模式（设置 SERVER_HOST 后自动进入）：
#   SERVER_HOST=1.2.3.4 SERVER_USER=root bash deploy.sh
#   → 用 rsync/tar 同步代码到远端 → 远端 docker compose up -d --build → 打印 URL
#
# 可选环境变量：
#   HOST_PORT        宿主端口，默认 8765（也可写在 .env 里）
#   SERVER_HOST      远端服务器 IP / 域名（设置后进入远程模式）
#   SERVER_USER      SSH 用户，默认 root
#   SERVER_PORT      SSH 端口，默认 22
#   SERVER_DIR       远端部署目录，默认 ~/researchbench
#   SERVER_HOST_PORT 远端对外端口，默认同 HOST_PORT
#
# 更多说明见 README「部署到服务器 / 本地 HTML 调用远程服务」一节。
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

say()  { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m[deploy]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[deploy]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[deploy][错误]\033[0m %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# 0. 读配置（.env 只取需要的键，避免 source 遇到特殊字符出错）
# -----------------------------------------------------------------------------
env_get() {
  local key="$1" file="${2:-$PROJECT_DIR/.env}"
  [ -f "$file" ] || return 0
  sed -n "s/^[[:space:]]*${key}[[:space:]]*=[[:space:]]*//p" "$file" | tail -n 1 | tr -d '\r'
}

HOST_PORT="${HOST_PORT:-$(env_get HOST_PORT)}"
HOST_PORT="${HOST_PORT:-8765}"
CONTAINER_PORT=8765

SERVER_HOST="${SERVER_HOST:-}"
SERVER_USER="${SERVER_USER:-root}"
SERVER_PORT="${SERVER_PORT:-22}"
SERVER_DIR="${SERVER_DIR:-~/researchbench}"
SERVER_HOST_PORT="${SERVER_HOST_PORT:-$HOST_PORT}"

# 让容器以当前宿主用户的 uid/gid 运行，避免挂载目录被改成 root 属主。
# 注意：bash 里 UID 是只读变量，只能 export 不能重新赋值；GID 可能未设置，显式赋值。
GID="$(id -g)"
export UID GID
say "宿主 UID/GID = $UID/$GID（用于避免 data/ 被改成 root 属主）"

# -----------------------------------------------------------------------------
# 1. 前置检查
# -----------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "未检测到 docker，请先安装 Docker Desktop / Docker Engine。"
docker compose version >/dev/null 2>&1 \
  || die "未检测到 'docker compose'（需要 Docker Compose v2 插件），请升级 Docker。"
docker info >/dev/null 2>&1 \
  || die "Docker 守护进程未运行，请先启动 Docker（macOS: 打开 Docker Desktop）。"

if [ ! -f "$PROJECT_DIR/.env" ]; then
  if [ -f "$PROJECT_DIR/.env.example" ]; then
    warn "未找到 .env，已根据 .env.example 自动生成（请按需修改 ADMIN_PASSWORD 等配置）。"
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  else
    die "缺少 .env 和 .env.example，无法继续。"
  fi
fi

ADMIN_PASSWORD_VALUE="$(env_get ADMIN_PASSWORD)"
if [ -n "$ADMIN_PASSWORD_VALUE" ]; then
  ok "管理员密码保护：已启用（写操作需要 X-Admin-Password；GET/检索不受影响）"
else
  warn "管理员密码保护：未启用（ADMIN_PASSWORD 为空）。公网部署强烈建议设置！"
fi

# -----------------------------------------------------------------------------
# 2. 端口检查：被占用时判断是不是本服务已在运行
# -----------------------------------------------------------------------------
port_in_use() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && return 0
  fi
  (exec 3<>"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1 && return 0
  return 1
}

health_check() {
  curl -fsS --max-time 3 "http://127.0.0.1:$1/api/health" >/dev/null 2>&1
}

check_local_port() {
  if port_in_use "$HOST_PORT"; then
    if health_check "$HOST_PORT"; then
      warn "端口 $HOST_PORT 上已有本服务在运行（/api/health 正常），将执行滚动更新。"
    else
      die "端口 $HOST_PORT 已被其他程序占用。请修改 .env 里的 HOST_PORT 后重试，
       或用 lsof -nP -iTCP:$HOST_PORT -sTCP:LISTEN 查看占用者。"
    fi
  fi
}

wait_healthy() {
  local port="$1" probe="$2" i
  say "等待服务就绪（轮询 http://127.0.0.1:$port/api/health）..."
  for i in $(seq 1 60); do
    if $probe; then
      ok "服务已就绪"
      return 0
    fi
    sleep 2
  done
  return 1
}

# -----------------------------------------------------------------------------
# 3. 本机 IP 探测，用于打印可访问 URL
# -----------------------------------------------------------------------------
detect_lan_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then           # macOS
    for iface in en0 en1 en2 en3; do
      ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
      [ -n "$ip" ] && break
    done
  fi
  if [ -z "$ip" ] && command -v ip >/dev/null 2>&1; then # Linux
    ip="$(ip -4 addr show scope global | sed -n 's/.*inet \([0-9.]*\)\/.*/\1/p' | head -n 1)"
  fi
  if [ -z "$ip" ] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi
  echo "$ip"
}

print_urls() {
  local host="$1" port="$2" lan base
  if [ "$host" = "local" ]; then
    base="127.0.0.1"
  else
    base="$host"
  fi
  echo ""
  ok "================ 部署完成 ================"
  ok "访问地址：  http://$base:$port"
  if [ "$host" = "local" ]; then
    lan="$(detect_lan_ip)"
    [ -n "$lan" ] && ok "局域网访问： http://$lan:$port  （同一 Wi-Fi 下的其他设备可用）"
  fi
  ok "健康检查：  http://$base:$port/api/health"
  ok "服务接口：  http://$base:$port/api/services/capabilities"
  echo ""
  warn "若公网访问不通，请检查：服务器防火墙 / 云厂商安全组是否放行 TCP $port 端口。"
  warn "停止服务： docker compose down    查看日志： docker compose logs -f"
}

# -----------------------------------------------------------------------------
# 4. 本地部署
# -----------------------------------------------------------------------------
deploy_local() {
  say "模式：本地部署"
  check_local_port
  say "开始构建镜像（首次较慢，后续会命中缓存）..."
  docker compose up -d --build || die "镜像构建 / 容器启动失败，请查看上方 Docker 输出。"

  if ! wait_healthy "$HOST_PORT" "health_check $HOST_PORT"; then
    echo ""
    warn "服务未在预期时间内就绪，以下是最近的容器日志："
    docker compose logs --tail=60 || true
    die "启动失败。可执行 'docker compose logs -f' 查看完整日志。"
  fi
  print_urls "local" "$HOST_PORT"
}

# -----------------------------------------------------------------------------
# 5. 远程部署（通过 SSH 在远端执行同样的动作）
# -----------------------------------------------------------------------------
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o BatchMode=no"
ssh_run() { ssh -p "$SERVER_PORT" $SSH_OPTS "$SERVER_USER@$SERVER_HOST" "$@"; }

sync_code() {
  local remote_dir="$1"
  say "同步代码到 $SERVER_USER@$SERVER_HOST:$remote_dir"
  if command -v rsync >/dev/null 2>&1; then
    rsync -az --no-owner --no-group \
      --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
      --exclude '.workbuddy' --exclude '.DS_Store' --exclude 'data/' \
      -e "ssh -p $SERVER_PORT $SSH_OPTS" \
      "$PROJECT_DIR/" "$SERVER_USER@$SERVER_HOST:$remote_dir/" \
      || die "代码同步失败（rsync）。"
  else
    warn "本机没有 rsync，改用 tar + ssh 同步（效果相同，稍慢）。"
    tar czf - \
      --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
      --exclude '.workbuddy' --exclude '.DS_Store' --exclude './data' \
      -C "$PROJECT_DIR" . \
      | ssh -p "$SERVER_PORT" $SSH_OPTS "$SERVER_USER@$SERVER_HOST" \
        "mkdir -p '$remote_dir' && tar xzf - -C '$remote_dir'" \
      || die "代码同步失败（tar over ssh）。"
  fi
}

deploy_remote() {
  say "模式：远程部署 → $SERVER_USER@$SERVER_HOST:$SERVER_PORT"
  command -v ssh >/dev/null 2>&1 || die "未检测到 ssh 命令。"
  command -v curl >/dev/null 2>&1 || die "未检测到 curl 命令（用于健康检查）。"

  # 远端目录：展开 ~
  SERVER_DIR="$(ssh_run "echo $SERVER_DIR" | tail -n 1)"
  [ -n "$SERVER_DIR" ] || die "无法获取远端部署目录路径。"
  say "远端目录：$SERVER_DIR"

  # 远端环境检查
  ssh_run "command -v docker >/dev/null 2>&1" \
    || die "远端未安装 docker，请先在服务器上安装 Docker 与 Compose v2 插件。"
  ssh_run "docker compose version >/dev/null 2>&1" \
    || die "远端未安装 docker compose v2 插件。"
  ssh_run "docker info >/dev/null 2>&1" \
    || die "远端 Docker 守护进程未运行。"

  sync_code "$SERVER_DIR"

  # 远端端口检查：被别的程序占用就直接报错，避免构建半天才发现端口冲突
  local port_state=""
  if ssh_run "command -v lsof >/dev/null 2>&1"; then
    port_state="$(ssh_run "bash -lc 'if lsof -nP -iTCP:$SERVER_HOST_PORT -sTCP:LISTEN >/dev/null 2>&1; then \
      if curl -fsS --max-time 3 http://127.0.0.1:$SERVER_HOST_PORT/api/health >/dev/null 2>&1; \
      then echo PORT_MINE; else echo PORT_OTHER; fi; else echo PORT_FREE; fi'" | tail -n 1)"
    case "$port_state" in
      PORT_MINE)  warn "远端端口 $SERVER_HOST_PORT 上已有本服务在运行，将执行滚动更新。" ;;
      PORT_OTHER) die "远端端口 $SERVER_HOST_PORT 已被其他程序占用，请更换 SERVER_HOST_PORT。" ;;
      *)          ok "远端端口 $SERVER_HOST_PORT 可用。" ;;
    esac
  else
    warn "远端没有 lsof，跳过端口占用检查。"
  fi

  # 远端 .env（保留服务器上已有的 .env，不覆盖用户配置）
  ssh_run "cd '$SERVER_DIR' && [ -f .env ] || cp .env.example .env" \
    || die "远端准备 .env 失败。"

  say "在远端构建并启动容器（HOST_PORT=$SERVER_HOST_PORT UID=$UID GID=$GID）..."
  # shellcheck disable=SC2016
  ssh_run "cd '$SERVER_DIR' && HOST_PORT='$SERVER_HOST_PORT' UID='$UID' GID='$GID' \
    docker compose up -d --build" \
    || die "远端构建 / 启动失败，请查看上方输出（或到服务器上执行 docker compose logs -f）。"

  say "等待远端服务就绪..."
  local i
  for i in $(seq 1 60); do
    if ssh_run "curl -fsS --max-time 3 http://127.0.0.1:$SERVER_HOST_PORT/api/health >/dev/null 2>&1"; then
      ok "远端服务已就绪"
      print_urls "$SERVER_HOST" "$SERVER_HOST_PORT"
      return 0
    fi
    sleep 2
  done
  warn "远端服务未在预期时间内就绪，以下是远端容器日志："
  ssh_run "cd '$SERVER_DIR' && docker compose logs --tail=60" || true
  die "远端启动失败，请到服务器上执行 'cd $SERVER_DIR && docker compose logs -f' 排查。"
}

# -----------------------------------------------------------------------------
# 6. 入口
# -----------------------------------------------------------------------------
case "${1:-}" in
  -h|--help)
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
esac

if [ -n "$SERVER_HOST" ]; then
  deploy_remote
else
  deploy_local
fi
