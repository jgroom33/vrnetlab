FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1


ARG IMAGE
COPY $IMAGE* /

# copy python scripts for launching VM
COPY *.py /

#need these files inside docker folder before running make
COPY OVMF_VARS_bkup.fd.gz /backup/
COPY WR1-CTM_ap.img.tar /

EXPOSE 22 179 225 443 830 9339