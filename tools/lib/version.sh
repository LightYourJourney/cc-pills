# install.sh 与 doctor 共用的依赖版本比较，兼容 bash 3.2。
ver_ge() {
  local major minor min_major min_minor
  [[ $1 =~ ^([0-9]+)([.]([0-9]+))?([.][0-9]+)?(-[[:alnum:].-]+)?$ ]] || return 1
  major=${BASH_REMATCH[1]}; minor=${BASH_REMATCH[3]:-0}
  min_major=${2%%.*}; min_minor=${2#*.}; [ "$min_minor" = "$2" ] && min_minor=0
  [[ $min_major =~ ^[0-9]+$ && $min_minor =~ ^[0-9]+$ ]] || return 1
  [ "$major" -gt "$min_major" ] || { [ "$major" -eq "$min_major" ] && [ "$minor" -ge "$min_minor" ]; }
}
