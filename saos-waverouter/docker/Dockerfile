FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1

RUN apt update && apt install -y \
    gdisk \
    swtpm \
    && rm -rf /var/lib/apt/lists/* #Clean up cache to reduce image size

ARG IMAGE
COPY $IMAGE* /

# copy python scripts for launching VM
COPY *.py /

#need these files inside docker folder before running make
COPY OVMF_VARS_bkup.fd.gz /backup/
COPY ap_disk/*.qcow2 /ap_disk/CTM_ap.qcow2
COPY /partition_helper.sh /

EXPOSE 22 179 225 443 830 9339