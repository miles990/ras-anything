#!/bin/sh
# label.sh TRACKING: the label code, as "<checksum> <bytes>"
printf %s "$1" | cksum
