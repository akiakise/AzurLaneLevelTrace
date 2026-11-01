#!/usr/bin/env bash
# 本地版本号升级工具：只修改 VERSION，不创建提交。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="${VERSION_FILE:-$SCRIPT_DIR/VERSION}"
PART=""
ASSUME_YES=0

usage() {
  cat <<'EOF'
用法：bump_version.sh [minor|major|patch] [-y|--yes]

  minor   小版本：1.0.1 -> 1.1.0
  major   大版本：1.0.1 -> 2.0.0
  patch   修订版本：1.0.1 -> 1.0.2
  -y, --yes  跳过确认，直接写入
  -h, --help 显示本帮助

省略升级类型时进入交互选择；工具只修改 VERSION，由你自行提交并推送。
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    minor|major|patch)
      if [ -n "$PART" ]; then
        echo "升级类型只能指定一次" >&2
        exit 2
      fi
      PART="$1"
      ;;
    -y|--yes)
      ASSUME_YES=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数：$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ ! -f "$VERSION_FILE" ]; then
  echo "找不到 VERSION 文件：$VERSION_FILE" >&2
  exit 1
fi

CURRENT="$(tr -d '[:space:]' < "$VERSION_FILE")"
if ! printf '%s' "$CURRENT" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "VERSION 必须是 <major>.<minor>.<patch> 形式，当前为：$CURRENT" >&2
  exit 1
fi

IFS='.' read -r CURRENT_MAJOR CURRENT_MINOR CURRENT_PATCH <<< "$CURRENT"

choose_part() {
  printf '请选择升级类型：\n'
  printf '  1. 小版本（minor）\n'
  printf '  2. 大版本（major）\n'
  printf '  3. 修订版本（patch）\n'
  while true; do
    printf '输入序号：' >&2
    if ! read -r choice; then
      echo "未接收到输入，已取消。" >&2
      exit 1
    fi
    case "$choice" in
      1|minor) PART="minor"; return 0 ;;
      2|major) PART="major"; return 0 ;;
      3|patch) PART="patch"; return 0 ;;
      *) echo "无效输入，请输入 1、2 或 3。" >&2 ;;
    esac
  done
}

[ -n "$PART" ] || choose_part

case "$PART" in
  major)
    NEXT_MAJOR=$((10#$CURRENT_MAJOR + 1))
    NEXT="$NEXT_MAJOR.0.0"
    LABEL="大版本"
    ;;
  minor)
    NEXT_MINOR=$((10#$CURRENT_MINOR + 1))
    NEXT="$CURRENT_MAJOR.$NEXT_MINOR.0"
    LABEL="小版本"
    ;;
  patch)
    NEXT_PATCH=$((10#$CURRENT_PATCH + 1))
    NEXT="$CURRENT_MAJOR.$CURRENT_MINOR.$NEXT_PATCH"
    LABEL="修订版本"
    ;;
esac

printf '当前版本：%s\n' "$CURRENT"
printf '升级类型：%s（%s）\n' "$LABEL" "$PART"
printf '新版本：  %s\n' "$NEXT"

if [ "$ASSUME_YES" -ne 1 ]; then
  printf '确认写入 %s？[y/N] ' "$VERSION_FILE"
  if ! read -r answer; then
    echo "未接收到输入，已取消。" >&2
    exit 1
  fi
  case "$answer" in
    y|Y|yes|YES) ;;
    *)
      echo "已取消，未修改 VERSION。"
      exit 0
      ;;
  esac
fi

TEMP_FILE="$(mktemp "$VERSION_FILE.XXXXXX")"
trap 'rm -f "$TEMP_FILE"' EXIT
printf '%s\n' "$NEXT" > "$TEMP_FILE"
mv "$TEMP_FILE" "$VERSION_FILE"
trap - EXIT

echo "已写入 $VERSION_FILE"
echo "下一步：git add VERSION && git commit -m 'chore(release): v$NEXT' && git push"
