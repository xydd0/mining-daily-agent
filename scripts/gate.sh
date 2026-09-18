#!/usr/bin/env bash
#
# 项目质量门禁：四项检查，全部通过才算绿。
#
# 这是门禁的**单一事实来源**。CLAUDE.md、.pre-commit-config.yaml 与
# .github/workflows/ci.yml 都调它——不要在那些地方重复列出命令，否则改一处漏一处，
# 「本地过了但 CI 挂了」就会重演。
#
# 用法：bash scripts/gate.sh
#
# 刻意不加 `set -e`：四项都跑完再汇总，一次就能看到全部问题，比逐个试错省事。
# 任一项失败则以非零码退出。
set -uo pipefail

# 切到仓库根目录：ruff / mypy / pytest 的配置解析都依赖 cwd。
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

checks=(
  "ruff check|uv run ruff check ."
  "ruff format --check|uv run ruff format --check"
  "mypy|uv run mypy"
  "pytest|uv run pytest"
)

failed=""
for entry in "${checks[@]}"; do
  name="${entry%%|*}"
  command="${entry#*|}"
  printf '\n=== %s ===\n' "$name"
  if ! bash -c "$command"; then
    failed="$failed $name"
  fi
done

printf '\n=== 汇总 ===\n'
if [ -z "$failed" ]; then
  printf '全部通过：%d 项\n' "${#checks[@]}"
  exit 0
fi
printf '未通过：%s\n' "${failed# }"
exit 1
