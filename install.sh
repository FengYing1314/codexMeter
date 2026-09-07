#!/bin/sh
# 链接当前源码目录，不覆盖用户已有的插件安装。
set -eu

if [ "$#" -ne 0 ]; then
    echo '用法：sh install.sh' >&2
    exit 2
fi

command -v python3 >/dev/null 2>&1 || {
    echo '需要 Python 3.11 或更高版本。' >&2
    exit 1
}
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11 或更高版本。")'

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}
target_dir=$config_dir/DankMaterialShell/plugins/codexMeter

if [ -e "$target_dir" ] || [ -L "$target_dir" ]; then
    if [ -L "$target_dir" ] && [ "$(readlink -f -- "$target_dir")" = "$source_dir" ]; then
        echo 'Codex Meter 已链接到当前目录。'
        exit 0
    fi
    echo "安装目录已存在，请先检查并备份：$target_dir" >&2
    exit 1
fi

mkdir -p -- "$(dirname -- "$target_dir")"
ln -s -- "$source_dir" "$target_dir"
echo "已安装：$target_dir"
echo '请在 DMS 设置中启用 Codex 用量，并将它添加到 DankBar。'
