#!/usr/bin/env bash
# 生成 GitHub Release 说明：版本镜像信息 + 每个提交的短 hash、链接与完整 message。
#
# 用法：REPOSITORY=owner/repo VERSION=1.2.3 TAG=v1.2.3 OUTPUT=/path/notes.md \
#       ./.github/scripts/generate_release_notes.sh
#
# 可选：RANGE 显式指定提交范围，默认取“上一版本标签..HEAD”。
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

: "${REPOSITORY:?缺少 REPOSITORY，例如 akiakise/AzurLaneLevelTrace}"
: "${VERSION:?缺少 VERSION}"
: "${OUTPUT:?缺少 OUTPUT}"
TAG="${TAG:-v$VERSION}"

if [ -z "${RANGE:-}" ]; then
  previous_tag="$(git describe --tags --abbrev=0 --match 'v*' --exclude "$TAG" HEAD 2>/dev/null || true)"
  if [ -n "$previous_tag" ]; then
    RANGE="$previous_tag..HEAD"
    echo "提交范围：$RANGE（上一版本 $previous_tag）" >&2
  else
    RANGE="HEAD"
    echo "未找到上一版本标签，列出全部历史提交" >&2
  fi
fi

export REPOSITORY VERSION RANGE OUTPUT
python3 - <<'PY'
import os
import subprocess

repository = os.environ["REPOSITORY"]
version = os.environ["VERSION"]
commit_range = os.environ["RANGE"]
output = os.environ["OUTPUT"]

log = subprocess.run(
    ["git", "log", "--reverse", "--format=%H%x1f%h%x1f%s%x1f%b%x1e", commit_range],
    check=True,
    capture_output=True,
    text=True,
).stdout

lines = [
    f"镜像：`akise/azur-lane-level-trace:{version}`、`akise/azur-lane-level-trace:latest`",
    "",
    "## 提交记录",
    "",
]

count = 0
for record in log.split("\x1e"):
    record = record.strip("\n")
    if not record.strip():
        continue
    full_sha, short_sha, subject, body = record.split("\x1f", 3)
    url = f"https://github.com/{repository}/commit/{full_sha}"
    lines.append(f"- [`{short_sha}`]({url}) {subject}".rstrip())
    for body_line in body.strip().splitlines():
        lines.append(f"  {body_line}".rstrip())
    count += 1

if count == 0:
    lines.append("- 本版本没有新的提交")

lines.append("")
with open(output, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines))

print(f"已生成 {output}，包含 {count} 个提交")
PY
