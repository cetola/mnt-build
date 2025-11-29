FROM archlinux:latest

RUN pacman -Sy --noconfirm archlinux-keyring && \
    pacman-key --init && \
    pacman-key --populate archlinux && \
    pacman -Syu --noconfirm && \
    pacman -S --noconfirm base base-devel && \
    pacman -S --noconfirm \
        dracut git bc kmod inetutils cpio perl tar xz wget python python-pip vim \
        aarch64-linux-gnu-binutils aarch64-linux-gnu-gcc aarch64-linux-gnu-gdb \
        dtc libelf flex bison openssl rsync && \
    pacman -S --noconfirm \
        gdb strace lsof pciutils usbutils iproute2 net-tools \
        man-db man-pages less which file htop tmux unzip zip && \
    pacman -S --noconfirm qemu-full && \
    pacman -Scc --noconfirm


RUN useradd -m -G wheel -s /bin/bash builder && \
    echo '%wheel ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers

RUN git config --global user.email "build@bot.net" && \
    git config --global user.name "Build Bot"

USER builder
WORKDIR /home/builder/mnt-build

RUN git config --global user.email "build@bot.net" && \
    git config --global user.name "Build Bot"
