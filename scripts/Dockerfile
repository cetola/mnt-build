FROM archlinux:latest

ARG UID=1001
ARG USERNAME=builder
ARG TZ=America/Los_Angeles

RUN pacman -Sy --noconfirm archlinux-keyring && \
    pacman-key --init && \
    pacman-key --populate archlinux && \
    pacman -Syu --noconfirm && \
    pacman -S --noconfirm base base-devel tzdata && \
    ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime && \
    echo "${TZ}" > /etc/timezone && \
    pacman -S --noconfirm \
        dracut git bc kmod inetutils cpio perl tar xz wget python python-pip vim sudo \
        aarch64-linux-gnu-binutils aarch64-linux-gnu-gcc aarch64-linux-gnu-gdb \
        dtc libelf flex bison openssl rsync curl arch-install-scripts && \
    pacman -S --noconfirm \
        parted qemu-user-static qemu-user-static-binfmt && \
    pacman -S --noconfirm \
        gdb strace lsof pciutils usbutils iproute2 net-tools \
        man-db man-pages less which file htop tmux unzip zip && \
    pacman -S --noconfirm qemu-full && \
    pacman -Scc --noconfirm

RUN useradd -m -u ${UID} -G wheel -s /bin/bash ${USERNAME} && \
    echo '%wheel ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers

USER ${USERNAME}
WORKDIR /home/${USERNAME}/mnt-build

# Install target OpenSSL headers/libs from Arch Linux ARM package into the
# aarch64 sysroot so cross-built sign-file can compile/link.
RUN set -euo pipefail && \
    cd /tmp && \
    CORE_DB_URL="http://mirror.archlinuxarm.org/aarch64/core/core.db" && \
    curl -fL "${CORE_DB_URL}" -o /tmp/core.db && \
    OPENSSL_DESC_ENTRY="$(bsdtar -tf /tmp/core.db | grep -E '^openssl-[^/]+/desc$' | head -n1)" && \
    if [ -z "${OPENSSL_DESC_ENTRY}" ]; then echo "Failed to find openssl entry in core.db" >&2; exit 1; fi && \
    OPENSSL_PKG_FILENAME="$(bsdtar -xOf /tmp/core.db "${OPENSSL_DESC_ENTRY}" | awk 'found{print; exit} /^%FILENAME%$/{found=1}')" && \
    if [ -z "${OPENSSL_PKG_FILENAME}" ]; then echo "Failed to read %FILENAME% for openssl from core.db" >&2; exit 1; fi && \
    OPENSSL_PKG_URL="http://mirror.archlinuxarm.org/aarch64/core/${OPENSSL_PKG_FILENAME}" && \
    curl -fL "${OPENSSL_PKG_URL}" -o /tmp/openssl-aarch64.pkg.tar && \
    rm -rf /tmp/openssl-pkg && mkdir -p /tmp/openssl-pkg && \
    bsdtar -xpf /tmp/openssl-aarch64.pkg.tar -C /tmp/openssl-pkg && \
    sudo mkdir -p /usr/aarch64-linux-gnu/include /usr/aarch64-linux-gnu/lib && \
    sudo cp -a /tmp/openssl-pkg/usr/include/openssl /usr/aarch64-linux-gnu/include/ && \
    sudo cp -a /tmp/openssl-pkg/usr/lib/libcrypto* /usr/aarch64-linux-gnu/lib/ && \
    sudo cp -a /tmp/openssl-pkg/usr/lib/libssl* /usr/aarch64-linux-gnu/lib/ && \
    rm -rf /tmp/openssl-aarch64.pkg.tar /tmp/core.db /tmp/openssl-pkg

# Install aarch64 compression libs that the updated libcrypto.so now depends on
# (zlib, zstd, brotli are new dynamic deps as of OpenSSL 3.x on Arch Linux ARM)
RUN set -euo pipefail && \
    MIRRORS=( \
      "http://nj.us.mirror.archlinuxarm.org/aarch64" \
      "http://ca.us.mirror.archlinuxarm.org/aarch64" \
      "http://fl.us.mirror.archlinuxarm.org/aarch64" \
      "http://eu.mirror.archlinuxarm.org/aarch64" \
      "http://de.mirror.archlinuxarm.org/aarch64" \
      "http://de3.mirror.archlinuxarm.org/aarch64" \
      "http://mirror.archlinuxarm.org/aarch64" \
    ) && \
    SYSROOT=/usr/aarch64-linux-gnu && \
    for repo_pkg in "core/zlib" "core/zstd" "extra/brotli"; do \
      repo="${repo_pkg%%/*}"; pkg="${repo_pkg##*/}"; \
      resolved=false; \
      for MIRROR in "${MIRRORS[@]}"; do \
        curl -fsSL "${MIRROR}/${repo}/${repo}.db" -o /tmp/repo.db 2>/dev/null || continue; \
        entry="$(bsdtar -tf /tmp/repo.db | grep -E "^${pkg}-[^/]+/desc$" | head -n1 || true)"; \
        [ -n "${entry}" ] || { echo "  ${pkg} not in ${MIRROR}/${repo}, trying next" >&2; continue; }; \
        file="$(bsdtar -xOf /tmp/repo.db "${entry}" | awk 'found{print;exit} /^%FILENAME%$/{found=1}' || true)"; \
        [ -n "${file}" ] || { echo "  No FILENAME for ${pkg} at ${MIRROR}, trying next" >&2; continue; }; \
        curl -fL "${MIRROR}/${repo}/${file}" -o /tmp/aarch64-pkg.tar || continue; \
        resolved=true; \
        break; \
      done; \
      [ "${resolved}" = "true" ] || { echo "Package ${pkg} not resolved from any mirror" >&2; exit 1; }; \
      rm -rf /tmp/aarch64-pkg-dir && mkdir /tmp/aarch64-pkg-dir; \
      bsdtar --no-same-owner -xf /tmp/aarch64-pkg.tar -C /tmp/aarch64-pkg-dir; \
      sudo find /tmp/aarch64-pkg-dir/usr/lib -maxdepth 1 -name 'lib*.so*' -exec cp -a {} "${SYSROOT}/lib/" \;; \
      rm -rf /tmp/repo.db /tmp/aarch64-pkg.tar /tmp/aarch64-pkg-dir; \
    done

RUN set -euo pipefail && \
    cd /tmp && \
    git clone https://aur.archlinux.org/yay.git && \
    cd yay && \
    makepkg -si --noconfirm && \
    yay -S --noconfirm --needed bmaptool && \
    rm -rf /tmp/yay

RUN git config --global user.email "build@mntar.ch" && \
    git config --global user.name "MNT-Build Bot" && \
    git config --global --add safe.directory '*'
