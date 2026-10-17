#define _GNU_SOURCE
#include "tp_dev.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "tp_hid.h"

static long long now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000LL + t.tv_nsec / 1000000;
}

static void absorb(tp_dev_t *d, const uint8_t *p)
{
    if (p[0] == TP_MSG_PARAM && p[1] < TP_NPARAMS)
        d->params[p[1]] = p[2];
    else if (p[0] == TP_MSG_SETUP && p[1] < TP_NSETUP)
        d->setup[p[1]] = p[2];
}

int tp_open(tp_dev_t *d, const char *path)
{
    memset(d, 0, sizeof *d);
    for (int i = 0; i < TP_NPARAMS; i++)
        d->params[i] = -1;
    for (int i = 0; i < TP_NSETUP; i++)
        d->setup[i] = -1;
    d->timeout_ms = TP_TIMEOUT_MS;
    d->fd = tp_hid_open(path);
    if (d->fd < 0)
        return -1;
    const uint8_t poll_cmd = TP_MSG_POLL;
    if (tp_exchange(d, &poll_cmd, 1, NULL, NULL) < 0) {
        int err = errno;
        tp_close(d);
        errno = err;
        return -1;
    }
    return 0;
}

void tp_close(tp_dev_t *d)
{
    if (d->fd >= 0)
        close(d->fd);
    d->fd = -1;
}

int tp_exchange(tp_dev_t *d, const uint8_t *cmd, int n, tp_msg_fn until, void *ctx)
{
    const uint8_t poll_cmd = TP_MSG_POLL;
    uint8_t p[TP_REPORT_LEN];
    int done = !until;
    long long deadline = now_ms() + d->timeout_ms;
    if (tp_hid_write(d->fd, cmd, n) < 0)
        return -1;
    for (;;) {
        int r = tp_hid_read(d->fd, p, TP_READ_TIMEOUT_MS);
        if (r < 0)
            return -1;
        if (!r || p[0] == TP_MSG_EMPTY) {
            if (r) {
                d->meter_in = p[1];
                d->meter_out = p[2];
                d->clip = p[3] & (TP_CLIP_IN | TP_CLIP_OUT);
            }
            if (done)
                return 0;
        } else if (!(p[0] == TP_MSG_QUERY && p[1] == TP_HEARTBEAT)) {
            absorb(d, p);
            if (d->on_msg)
                d->on_msg(p, d->on_msg_ctx);
            done = done || until(p, ctx);
        }
        if (now_ms() > deadline) {
            errno = ETIMEDOUT;
            return -1;
        }
        if (tp_hid_write(d->fd, &poll_cmd, 1) < 0)
            return -1;
    }
}

static int is_info(const uint8_t *p, void *ctx)
{
    if (p[0] != TP_MSG_INFO)
        return 0;
    memcpy(ctx, p, TP_REPORT_LEN);
    return 1;
}

int tp_info(tp_dev_t *d, char serial[9], char firmware[8])
{
    const uint8_t cmd[] = {TP_MSG_QUERY, TP_QUERY_INFO};
    uint8_t p[TP_REPORT_LEN];
    if (tp_exchange(d, cmd, sizeof cmd, is_info, p) < 0)
        return -1;
    memcpy(serial, p + 7, 8);
    serial[8] = 0;
    snprintf(firmware, 8, "%u.%u", p[4], p[5]);   /* (?) bytes 4-5, as device.py reads them */
    return 0;
}

struct read_all {
    uint8_t type;
    int count, seen;
    uint8_t mask[32];
};

static int all_seen(const uint8_t *p, void *ctx)
{
    struct read_all *r = ctx;
    if (p[0] == r->type && !(r->mask[p[1] / 8] & 1 << p[1] % 8)) {
        r->mask[p[1] / 8] |= 1 << p[1] % 8;
        r->seen++;
    }
    return r->seen == r->count;
}

static int read_all(tp_dev_t *d, uint8_t query, uint8_t reply, int count)
{
    const uint8_t cmd[] = {TP_MSG_QUERY, query};
    struct read_all r = {.type = reply, .count = count};
    return tp_exchange(d, cmd, sizeof cmd, all_seen, &r);
}

int tp_read_params(tp_dev_t *d)
{
    return read_all(d, TP_QUERY_PARAMS, TP_MSG_PARAM, TP_NPARAMS);
}

int tp_read_setup(tp_dev_t *d)
{
    return read_all(d, TP_QUERY_SETUP, TP_MSG_SETUP, TP_NSETUP);
}
