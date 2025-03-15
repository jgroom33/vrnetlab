FROM ghcr.io/srl-labs/vrnetlab-base:0.2.1


ARG IMAGE
COPY $IMAGE* /

# copy python scripts for launching VM
COPY *.py /

EXPOSE 22 830
