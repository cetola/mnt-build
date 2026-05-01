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
    pacman -Scc --noconfirm

RUN useradd -m -u ${UID} -G wheel -s /bin/bash ${USERNAME} && \
    echo '%wheel ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers

USER ${USERNAME}
WORKDIR /home/${USERNAME}/mnt-build

# aarch64 cross-compilation sysroot files (libssl, libcrypto, libz, libzstd,
# libbrotli*, and openssl/ headers) are downloaded and staged on the host by
# build-container.sh --with-arm-libs before this image is built.  Without that
# flag the directory contains only empty lib/ and include/ subdirs and this
# instruction is a no-op.
COPY arm-libs-staging/ /usr/aarch64-linux-gnu/

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
