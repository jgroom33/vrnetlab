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

# Expose standard ports and hostfwd ports for up to 9 VMs (50225-50233 for SSH, 54243-54251 for Docker)
EXPOSE 22 179 225 443 830 9339 50225 50226 50227 50228 50229 50230 50231 50232 50233 54243 54244 54245 54246 54247 54248 54249 54250 54251