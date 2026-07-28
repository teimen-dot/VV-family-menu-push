#!/bin/bash
# 一键同步到 GitHub
# 用法: 在终端运行 ./sync.sh 或双击此文件
cd "$(dirname "$0")"

echo "🔄 同步到 GitHub..."

git add -A
git commit -m "update: $(date '+%Y-%m-%d %H:%M')"
git push origin main

if [ $? -eq 0 ]; then
  echo "✅ 同步完成！GitHub Actions 会在明天 10:30 自动推送菜单。"
else
  echo "❌ 同步失败，请检查错误信息。"
  echo "  如果是认证问题，请运行: gh auth login"
fi
