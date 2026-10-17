/* torpedo-probe: checks the C transport on the MPC (or any Linux box) before the plugin relies on it.
 *   torpedo-probe [PATH]                 info, setup and the current preset's 51 parameters
 *   torpedo-probe --listen SECS [PATH]   then what the unit sends on its own (front panel) and the meters
 *   torpedo-probe --find [--sysfs DIR]   the hidraw node found in sysfs
 * PATH defaults to the node found in sysfs; "unix:<socket>" talks to tests/fakebridge.py.
 * Read-only: sends nothing but 7e, 04 01, 04 0a and 04 0b. */
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "tp_dev.h"
#include "tp_hid.h"

static const char usage[] = "usage: torpedo-probe [--listen SECS] [--find] [--sysfs DIR] [PATH]\n";

static int print_msg(const uint8_t *p, void *ctx)
{
    (void)ctx;
    if (p[0] == TP_MSG_PARAM && p[1] < TP_NPARAMS)
        printf("param %s %d\n", tp_params[p[1]].name, p[2]);
    else if (p[0] == TP_MSG_SETUP && p[1] < TP_NSETUP)
        printf("setup %s %d\n", tp_setup_names[p[1]], p[2]);
    else
        printf("msg %02x %02x %02x %02x\n", p[0], p[1], p[2], p[3]);
    fflush(stdout);
    return 0;
}

static double now_s(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

/* Poll every 0.1 s like the TUI: messages as they come, the meters when they change. */
static int listen_for(tp_dev_t *d, double secs)
{
    const uint8_t poll_cmd = TP_MSG_POLL;
    int in = -1, out = -1, clip = -1;
    double end = now_s() + secs;
    d->on_msg = print_msg;
    printf("listening %.1f s\n", secs);
    fflush(stdout);
    while (now_s() < end) {
        if (tp_exchange(d, &poll_cmd, 1, NULL, NULL) < 0)
            return -1;
        if (d->meter_in != in || d->meter_out != out || d->clip != clip) {
            in = d->meter_in, out = d->meter_out, clip = d->clip;
            printf("meters in %d out %d clip %d\n", in, out, clip);
            fflush(stdout);
        }
        usleep(100000);
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *sysfs = TP_SYSFS_HIDRAW, *path = NULL;
    double listen = 0;
    int find = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--find"))
            find = 1;
        else if (!strcmp(argv[i], "--sysfs") && i + 1 < argc)
            sysfs = argv[++i];
        else if (!strcmp(argv[i], "--listen") && i + 1 < argc)
            listen = atof(argv[++i]);
        else if (argv[i][0] != '-' && !path)
            path = argv[i];
        else {
            fputs(usage, stderr);
            return 2;
        }
    }
    char found[300];
    if (!path) {
        if (tp_hid_find(sysfs, found, sizeof found) < 0) {
            fprintf(stderr, "torpedo-probe: no Torpedo Live in %s\n", sysfs);
            return 1;
        }
        path = found;
    }
    if (find) {
        puts(path);
        return 0;
    }

    tp_dev_t d;
    char serial[9], firmware[8];
    if (tp_open(&d, path) < 0) {
        fprintf(stderr, "torpedo-probe: cannot open %s: %s\n", path, strerror(errno));
        return 1;
    }
    int ok = !tp_info(&d, serial, firmware) && !tp_read_setup(&d) && !tp_read_params(&d);
    if (ok) {
        printf("path %s\nserial %s\nfirmware %s\n", path, serial, firmware);
        for (int i = 0; i < TP_NSETUP; i++)
            printf("setup %s %d\n", tp_setup_names[i], d.setup[i]);
        for (int i = 0; i < TP_NPARAMS; i++)
            printf("param %s %d\n", tp_params[i].name, d.params[i]);
        printf("meters in %d out %d clip %d\n", d.meter_in, d.meter_out, d.clip);
        fflush(stdout);
        if (listen > 0)
            ok = !listen_for(&d, listen);
    }
    if (!ok)
        fprintf(stderr, "torpedo-probe: %s: %s\n", path, strerror(errno));
    tp_close(&d);
    return ok ? 0 : 1;
}
