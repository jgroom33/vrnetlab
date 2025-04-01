FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1


ARG IMAGE
COPY $IMAGE* /

# copy python scripts for launching VM
COPY *.py /

#copy OVMF_CODE-pure-efi.fd for now, need this file inside docker folder before running make
COPY *.fd /

EXPOSE 22 179 225 443 830 9339