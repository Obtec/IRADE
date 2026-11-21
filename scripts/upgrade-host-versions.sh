#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/upgrade-host-versions.sh [--remove-older]" >&2
  exit 1
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "This script currently targets x86_64 Linux." >&2
  exit 1
fi

remove_older=0
if [[ "${#}" -gt 1 ]]; then
  echo "Usage: sudo bash scripts/upgrade-host-versions.sh [--remove-older]" >&2
  exit 2
fi
if [[ "${#}" -eq 1 ]]; then
  if [[ "$1" != "--remove-older" ]]; then
    echo "Usage: sudo bash scripts/upgrade-host-versions.sh [--remove-older]" >&2
    exit 2
  fi
  remove_older=1
fi

host_upgrade_tmp="$(mktemp -d)"
trap 'rm -rf "$host_upgrade_tmp"' EXIT

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl xz-utils build-essential pkg-config perl \
  libcurl4-gnutls-dev libexpat1-dev libssl-dev zlib1g-dev

# Install the exact Temurin JDK release and verify its published checksum.
temurin_tag='jdk-27.0.1.1+4'
temurin_asset='OpenJDK27U-jdk_x64_linux_hotspot_27.0.1.1_4.tar.gz'
temurin_base="https://github.com/adoptium/temurin27-binaries/releases/download/${temurin_tag//+/%2B}"
curl --fail --location --silent --show-error \
  "${temurin_base}/${temurin_asset}" \
  -o "$host_upgrade_tmp/$temurin_asset"
curl --fail --location --silent --show-error \
  "${temurin_base}/${temurin_asset}.sha256.txt" \
  -o "$host_upgrade_tmp/${temurin_asset}.sha256.txt"
(cd "$host_upgrade_tmp" && sha256sum --check "${temurin_asset}.sha256.txt")

temurin_home='/usr/lib/jvm/temurin-27.0.1.1+4'
if [[ ! -x "$temurin_home/bin/java" ]]; then
  install -d -o root -g root "$temurin_home"
  tar -xzf "$host_upgrade_tmp/$temurin_asset" \
    --strip-components=1 -C "$temurin_home"
fi
temurin_version="$("$temurin_home/bin/java" -version 2>&1 | sed -n '1s/.*"\([^"]*\)".*/\1/p')"
dpkg --compare-versions "$temurin_version" ge 27.0.1.1

# Make Java 27 the host default; keep GraalVM as a separately selectable JDK.
for tool in java javac; do
  if [[ -x "$temurin_home/bin/$tool" ]]; then
    update-alternatives --remove-all "$tool" >/dev/null 2>&1 || true
    update-alternatives --install "/usr/bin/$tool" "$tool" "$temurin_home/bin/$tool" 2701
    update-alternatives --set "$tool" "$temurin_home/bin/$tool"
  fi
done
cat > /etc/profile.d/java27.sh <<EOF
export JAVA_HOME='$temurin_home'
export PATH="\$JAVA_HOME/bin:\$PATH"
EOF

# Install Oracle GraalVM 21 from its official latest-21 archive and verify checksum.
graal_url='https://download.oracle.com/graalvm/21/latest/graalvm-jdk-21_linux-x64_bin.tar.gz'
curl --fail --location --silent --show-error "$graal_url" \
  -o "$host_upgrade_tmp/graalvm-jdk-21.tar.gz"
graal_sha="$(curl --fail --location --silent --show-error "${graal_url}.sha256" | awk '{print $1}')"
printf '%s  %s\n' "$graal_sha" "$host_upgrade_tmp/graalvm-jdk-21.tar.gz" \
  | sha256sum --check

graal_stage="$host_upgrade_tmp/graalvm-21"
install -d "$graal_stage"
tar -xzf "$host_upgrade_tmp/graalvm-jdk-21.tar.gz" \
  --strip-components=1 -C "$graal_stage"
graal_version="$("$graal_stage/bin/java" -version 2>&1 | sed -n '1s/.*"\([^"]*\)".*/\1/p')"
dpkg --compare-versions "$graal_version" ge 21.0.13.1
graal_home="/opt/graalvm-${graal_version}"
if [[ -x "$graal_home/bin/java" ]]; then
  installed_graal_version="$("$graal_home/bin/java" -version 2>&1 | sed -n '1s/.*"\([^"]*\)".*/\1/p')"
  if [[ "$installed_graal_version" != "$graal_version" ]]; then
    echo "Refusing to replace existing GraalVM at $graal_home." >&2
    exit 1
  fi
  rm -rf "$graal_stage"
else
  mv "$graal_stage" "$graal_home"
fi
cat > /etc/profile.d/graalvm21.sh <<EOF
export GRAALVM_HOME='$graal_home'
export PATH="\$GRAALVM_HOME/bin:\$PATH"
EOF

# Build and install the official Git 2.56.1 source under /usr/local.
git_version='2.56.1'
curl --fail --location --silent --show-error \
  "https://www.kernel.org/pub/software/scm/git/git-${git_version}.tar.xz" \
  -o "$host_upgrade_tmp/git-${git_version}.tar.xz"
tar -xJf "$host_upgrade_tmp/git-${git_version}.tar.xz" -C "$host_upgrade_tmp"
make -C "$host_upgrade_tmp/git-${git_version}" -j"$(nproc)" \
  prefix=/usr/local NO_GETTEXT=YesPlease all
make -C "$host_upgrade_tmp/git-${git_version}" \
  prefix=/usr/local NO_GETTEXT=YesPlease install

if [[ "$remove_older" -eq 1 ]]; then
  if pgrep -x java >/dev/null 2>&1; then
    echo "Older Java installations were kept because a Java process is running." >&2
  else
    old_packages=()
    for old_package in \
      openjdk-11-jdk openjdk-11-jdk-headless openjdk-11-jre openjdk-11-jre-headless \
      openjdk-17-jdk openjdk-17-jdk-headless openjdk-17-jre openjdk-17-jre-headless; do
      if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$old_package" 2>/dev/null || true)" == installed ]]; then
        old_packages+=("$old_package")
      fi
    done
    if [[ "${#old_packages[@]}" -gt 0 ]]; then
      apt-get purge -y "${old_packages[@]}"
    fi
    old_jdk='/usr/lib/jvm/jdk-26.0.2.1'
    if [[ -d "$old_jdk" ]]; then
      for old_tool in "$old_jdk"/bin/*; do
        [[ -x "$old_tool" ]] || continue
        update-alternatives --remove "${old_tool##*/}" "$old_tool" \
          >/dev/null 2>&1 || true
      done
      rm -rf "$old_jdk"
    fi
  fi
  old_git_packages=()
  for old_package in git git-man; do
    if [[ "$(dpkg-query -W -f='${db:Status-Status}' "$old_package" 2>/dev/null || true)" == installed ]]; then
      old_git_packages+=("$old_package")
    fi
  done
  if [[ "${#old_git_packages[@]}" -gt 0 ]]; then
    apt-get purge -y "${old_git_packages[@]}"
  fi
fi

echo "Host Java: $(/usr/bin/java -version 2>&1 | head -n 1)"
echo "Host GraalVM: $graal_version"
echo "Host Git: $(/usr/local/bin/git --version)"
