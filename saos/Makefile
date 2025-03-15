VENDOR=ciena
NAME=saos
IMAGE_GLOB=*.qcow2
IMAGE=meta_10-10-01-0182-core-x86-64-disk.qcow2

# match versions like:
# 10-10-01-0182
VERSION=$(shell echo $(IMAGE) | sed -e 's/meta_//' | sed -e 's/-core-x86-64-disk.qcow2//')

-include ../makefile-sanity.include
-include ../makefile.include
