#!/bin/sh
# 链接当前源码目录，不覆盖用户已有的插件安装。
set -eu

usage() {
    echo '用法：sh install.sh [--dms-major 1|2]' >&2
    exit 2
}

dms_major=
case "$#" in
    0) ;;
    2)
        [ "$1" = '--dms-major' ] || usage
        case "$2" in 1|2) dms_major=$2 ;; *) usage ;; esac
        ;;
    *) usage ;;
esac

command -v python3 >/dev/null 2>&1 || {
    echo '需要 Python 3.11 或更高版本。' >&2
    exit 1
}
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11 或更高版本。")'

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
config_dir=${XDG_CONFIG_HOME:-"$HOME/.config"}

if [ -z "$dms_major" ]; then
    if command -v dms >/dev/null 2>&1; then
        if ! dms_version=$(dms version 2>/dev/null); then
            echo '无法查询 DMS 版本，请用 --dms-major 1 或 2 指定安装目标。' >&2
            exit 1
        fi
        dms_major=$(printf '%s\n' "$dms_version" | sed -n 's/^dms v\([0-9][0-9]*\)\..*/\1/p')
        case "$dms_major" in
            1|2) ;;
            *)
                echo '无法识别或尚不支持该 DMS 主版本，请用 --dms-major 1 或 2 指定安装目标。' >&2
                exit 1
                ;;
        esac
    else
        # 尚未安装 DMS 时，采用当前 2.x 的配置目录。
        dms_major=2
    fi
fi

case "$dms_major" in
    1) target_dir=$config_dir/DankMaterialShell/plugins/codexMeter ;;
    2) target_dir=$config_dir/dms/plugins/codexMeter ;;
esac

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
