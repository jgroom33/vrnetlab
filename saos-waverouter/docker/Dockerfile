FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1

RUN apt update && apt install -y \
    gdisk \
    swtpm \
    && rm -rf /var/lib/apt/lists/* #Clean up cache to reduce image size

ARG IMAGE
COPY $IMAGE* /

# copy python scripts for launching VM
COPY *.py /
RUN uv pip install telnetlib3

#need these files inside docker folder before running make
COPY OVMF_VARS_bkup.fd.gz /backup/
COPY ap_disk/*.qcow2 /ap_disk/CTM_ap.qcow2
COPY /partition_helper.sh /

# Expose standard ports and hostfwd ports for up to 9 VMs
# Telnet console: 5000-5008, SSH: 50225-50233, Docker daemon: 54243-54251, GDB: 64444-64452
EXPOSE 22 179 225 443 830 9339 5000-5008 50225-50233 54243-54251 64444-64452 