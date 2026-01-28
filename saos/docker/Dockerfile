FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1


ARG IMAGE
COPY $IMAGE* /

# copy python scripts for launching VM
COPY *.py /
RUN uv pip install telnetlib3 scrapli-netconf

EXPOSE 22 179 225 443 830 9339
