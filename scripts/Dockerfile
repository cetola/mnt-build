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
