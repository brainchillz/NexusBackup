# NexusBackup server image (command: hvbackup). Debian 13 base with the libguestfs tool chain, so
# conversion between VMware and Proxmox works in the container like on a native install.
# libguestfs boots a small helper VM (the "appliance") to edit guest disks; that needs the kernel
# in this image, and the appliance itself is built on first use into the data volume (about a
# minute, once). Map /dev/kvm into the container (compose.kvm.yml) for full speed; without it the
# appliance runs under software emulation, several times slower but working.
FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -q && apt-get install -y -q --no-install-recommends \
        qemu-utils virt-v2v libguestfs-tools guestfs-tools linux-image-amd64 \
    && rm -rf /var/lib/apt/lists/* /var/tmp/* /tmp/* \
    && chmod 0644 /boot/vmlinuz-* \
    && mkdir -p /var/lib/hvbackup /etc/hvbackup
COPY pyproject.toml README.md /src/
COPY hvbackup /src/hvbackup
RUN pip install --no-cache-dir /src && rm -rf /src
ENV HVB_DATA_DIR=/var/lib/hvbackup HVB_REPO=/var/lib/hvbackup/repo HVB_CONFIG=/etc/hvbackup/hvbackup.conf \
    LIBGUESTFS_BACKEND=direct LIBGUESTFS_CACHEDIR=/var/lib/hvbackup/cache
EXPOSE 8460
VOLUME ["/var/lib/hvbackup"]
ENTRYPOINT ["hvbackup"]
CMD ["server"]
