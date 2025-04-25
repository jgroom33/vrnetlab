FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1

RUN apt update && apt install -y \
    gdisk \
    && rm -rf /var/lib/apt/lists/* #Clean up cache to reduce image size

ARG IMAGE
COPY $IMAGE* /

# copy python scripts for launching VM
COPY *.py /

#need these files inside docker folder before running make
COPY OVMF_VARS_bkup.fd.gz /backup/
COPY CTM_ap.img.tar.gz /
COPY /partition_helper.sh /

EXPOSE 22 179 225 443 830 9339