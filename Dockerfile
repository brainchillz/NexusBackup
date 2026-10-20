FROM python:3.12-slim
RUN mkdir -p /var/lib/hvbackup /etc/hvbackup
COPY pyproject.toml README.md /src/
COPY hvbackup /src/hvbackup
RUN pip install --no-cache-dir /src && rm -rf /src
ENV HVB_DATA_DIR=/var/lib/hvbackup HVB_REPO=/var/lib/hvbackup/repo HVB_CONFIG=/etc/hvbackup/hvbackup.conf
EXPOSE 8460
VOLUME ["/var/lib/hvbackup"]
ENTRYPOINT ["hvbackup"]
CMD ["server"]
