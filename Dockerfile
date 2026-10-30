# NexusBackup server image (command: hvbackup). Debian 13 base with the libguestfs tool chain, so
# conversion between VMware and Proxmox works in the container like on a native install.
# libguestfs boots a small helper VM (the "appliance") to edit guest disks; that needs the kernel
# in this image, and the appliance itself is built on first use into the data volume (about a
# minute, once). Map /dev/kvm into the container (compose.kvm.yml) for full speed; without it the
# appliance runs under software emulation, several times slower but working.
# Stage 1: rhsrvany.exe and pnp_wait.exe, the first-boot helpers libguestfs needs inside Windows
# guests (GPL, github.com/rwmjones/rhsrvany), cross-built with mingw so the image converts
# Windows guests without a download. Pinned to a commit; see windows-tools.yml for the same build.
FROM debian:13-slim AS rhsrvany
ENV DEBIAN_FRONTEND=noninteractive
ARG RHSRVANY_REF=74fc2f5
RUN apt-get update -q && apt-get install -y -q --no-install-recommends git ca-certificates mingw-w64 autoconf automake make \
    && git clone -q https://github.com/rwmjones/rhsrvany.git /src && cd /src && git checkout -q $RHSRVANY_REF \
    && autoreconf -fi && ./configure --host=i686-w64-mingw32 && make \
    && i686-w64-mingw32-strip RHSrvAny/rhsrvany.exe pnp_wait/pnp_wait.exe

FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && apt-get install -y -q --no-install-recommends \
        qemu-utils virt-v2v libguestfs-tools guestfs-tools linux-image-amd64 libwin-hivex-perl \
    && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/* \
    && chmod 0644 /boot/vmlinuz-* \
    && mkdir -p /var/lib/hvbackup /etc/hvbackup
COPY --from=rhsrvany /src/RHSrvAny/rhsrvany.exe /src/pnp_wait/pnp_wait.exe /usr/share/virt-tools/
COPY pyproject.toml README.md /src/
COPY hvbackup /src/hvbackup
RUN pip install --no-cache-dir /src && rm -rf /src
ENV HVB_DATA_DIR=/var/lib/hvbackup HVB_REPO=/var/lib/hvbackup/repo HVB_CONFIG=/etc/hvbackup/hvbackup.conf \
    LIBGUESTFS_BACKEND=direct LIBGUESTFS_CACHEDIR=/var/lib/hvbackup/cache
EXPOSE 8460
VOLUME ["/var/lib/hvbackup"]
ENTRYPOINT ["hvbackup"]
CMD ["server"]
