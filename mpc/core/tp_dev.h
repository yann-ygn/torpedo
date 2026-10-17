/* The Torpedo over tp_hid, like src/torpedo/device.py's Torpedo: a cache of the current preset's
 * parameters and the setup values, updated from every message the device sends.
 *
 * The device answers each OUT report with exactly one IN report: the next queued message, or an
 * `05` report with the meters when its queue is empty. So after a command we poll with `7e`
 * until the expected reply has arrived and the queue is empty (PROTOCOL.md "Polling"). */
#pragma once
#include <stdint.h>

#include "tp_tables.h"

#define TP_TIMEOUT_MS 2000
#define TP_READ_TIMEOUT_MS 100

/* Called for each message the device sends during an exchange (not `05` or the heartbeat). */
typedef int (*tp_msg_fn)(const uint8_t *report, void *ctx);

typedef struct {
    int fd;
    int timeout_ms;
    int params[TP_NPARAMS];   /* -1 until read */
    int setup[TP_NSETUP];
    uint8_t meter_in, meter_out, clip;   /* from the latest `05` */
    tp_msg_fn on_msg;   /* optional: sees every message (front-panel changes); return value unused */
    void *on_msg_ctx;
} tp_dev_t;

int tp_open(tp_dev_t *d, const char *path);   /* opens and flushes the device's queue */
void tp_close(tp_dev_t *d);
/* Send cmd, then poll until `until` returned nonzero for a message (if given) and the queue is
 * empty. -1 with errno ETIMEDOUT after timeout_ms, or the transport's errno (ENODEV: unplugged). */
int tp_exchange(tp_dev_t *d, const uint8_t *cmd, int n, tp_msg_fn until, void *ctx);
int tp_info(tp_dev_t *d, char serial[9], char firmware[8]);
int tp_read_params(tp_dev_t *d);
int tp_read_setup(tp_dev_t *d);
