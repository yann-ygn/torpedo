/* Transport: the Torpedo's hidraw node, one 64-byte report each way (PROTOCOL.md "Transport").
 * A path "unix:<socket>" connects to a SOCK_SEQPACKET socket instead: tests/fakebridge.py serves
 * the emulator (torpedo.fake) there. Every function returns -1 with errno set on failure. */
#pragma once
#include <stddef.h>
#include <stdint.h>

#define TP_SYSFS_HIDRAW "/sys/class/hidraw"

/* Find the Torpedo's /dev/hidrawN by its HID_ID and HID_NAME in sysfs (ST's VID is shared, so the
 * name is checked too). 0 when found, -1 (errno ENODEV) when not. */
int tp_hid_find(const char *sysfs, char *path, size_t len);
int tp_hid_open(const char *path);   /* a file descriptor */
/* Send one report: payload (n <= 64 bytes) zero-padded, after report ID 0. */
int tp_hid_write(int fd, const uint8_t *payload, int n);
/* 1: one report read into report[64] (zero-padded if shorter), 0: nothing within timeout_ms. */
int tp_hid_read(int fd, uint8_t *report, int timeout_ms);
