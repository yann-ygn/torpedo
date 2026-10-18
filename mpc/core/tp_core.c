#define _GNU_SOURCE
#include "tp_core.h"

#include <errno.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "tp_dev.h"
#include "tp_hid.h"

enum { OP_PARAM, OP_SETUP, OP_PRESET, OP_CAB, OP_IR, OP_STORE };
typedef struct {
    uint8_t kind, index;   /* index: the parameter, setting, IR slot or preset to store (0 for a preset or cab selection) */
    int value;
} op_t;
#define MAX_OPS (TP_NPARAMS + TP_NSETUP + 5)   /* one per kind and index (one store at a time) */
#define TP_STORE_TIMEOUT_MS 6000   /* storing a preset was answered after ~2.1 s (device.py STORE_TIMEOUT) */

typedef struct {
    int count;
    tp_cab_t items[TP_MAX_BANK];
} bank_t;

typedef struct {
    char name[TP_NAME_LEN];
    int arcade;
} name_t;

static const int WAVES[2] = {TP_P_SIMUHP_WAVE0, TP_P_SIMUHP_WAVE1};
static const int FOLDERS[2] = {TP_P_SIMUHP_FOLDER_IR_0, TP_P_SIMUHP_FOLDER_IR_1};

struct tp_core {
    tp_core_config_t cfg;
    pthread_t thread;
    pthread_mutex_t lock;
    pthread_cond_t wake;   /* the worker: a queued change, or stop */
    pthread_cond_t idle;   /* tp_core_wait_idle: the queue is empty */
    int stop;
    /* under the lock; the lists are only written by the worker, which reads them without it */
    int connected;
    unsigned generation;
    char serial[9], firmware[8];
    int params[TP_NPARAMS], setup[TP_NSETUP];   /* the device's, as last published */
    int want_param[TP_NPARAMS], want_setup[TP_NSETUP];   /* queued or being sent; -1: none */
    uint8_t meter_in, meter_out, clip;
    op_t ops[MAX_OPS];
    int nops, busy;
    tp_changes_t changes;
    unsigned revision;   /* +1 whenever changes are noted */
    int storing, store_status, store_slot;   /* see tp_state_t */
    unsigned store_seq;
    char store_name[TP_NAME_MAX + 1];   /* the name the queued store gives the buffer first, or "" */
    bank_t cabs, irs;
    name_t names[TP_PRESETS + 1];   /* [TP_EDIT_BUFFER]: the edit buffer */
    /* worker only */
    tp_dev_t dev;
    bank_t stage;   /* a bank being read */
    int saw_edit, saw_source;   /* during one operation: `0c 03 80`, `0c 04 n` */
};

static void deadline_in(struct timespec *t, int ms)
{
    clock_gettime(CLOCK_MONOTONIC, t);
    t->tv_sec += ms / 1000;
    t->tv_nsec += ms % 1000 * 1000000L;
    if (t->tv_nsec >= 1000000000L) {
        t->tv_sec++;
        t->tv_nsec -= 1000000000L;
    }
}

static void wait_ms(tp_core_t *c, pthread_cond_t *cond, int ms)
{
    struct timespec t;
    deadline_in(&t, ms);
    pthread_cond_timedwait(cond, &c->lock, &t);
}

static void copy_str(char *dst, const uint8_t *src, int n, int size)   /* stops at NUL; stale bytes follow */
{
    int i = 0;
    for (; i < n && i < size - 1 && src[i]; i++)
        dst[i] = src[i];
    dst[i] = 0;
}

/* --- shared state (under the lock) ------------------------------------------------------- */

static int vis_param(const tp_core_t *c, int i)
{
    return c->want_param[i] >= 0 ? c->want_param[i] : c->params[i];
}

static int vis_setup(const tp_core_t *c, int i)
{
    return c->want_setup[i] >= 0 ? c->want_setup[i] : c->setup[i];
}

/* An operation is done: drop the queued values it was sending, unless a newer one is queued. */
static void settle(tp_core_t *c, const op_t *op)
{
    int *want = NULL;
    switch (op->kind) {
    case OP_PARAM: want = &c->want_param[op->index]; break;
    case OP_SETUP: want = &c->want_setup[op->index]; break;
    case OP_PRESET: want = &c->want_setup[TP_S_SETUP_PRESET]; break;
    case OP_CAB: want = &c->want_param[TP_P_SIMUHP_SPEAKER]; break;
    case OP_IR: want = &c->want_param[WAVES[op->index]]; break;
    case OP_STORE: return;   /* nothing is shown ahead of it */
    }
    if (*want == op->value)
        *want = -1;
}

/* Copy the worker's view of the device to the shared state, noting what changed for the plugin. */
static void publish(tp_core_t *c, const op_t *done)
{
    int old_p[TP_NPARAMS], old_s[TP_NSETUP];
    for (int i = 0; i < TP_NPARAMS; i++)
        old_p[i] = vis_param(c, i);
    for (int i = 0; i < TP_NSETUP; i++)
        old_s[i] = vis_setup(c, i);
    memcpy(c->params, c->dev.params, sizeof c->params);
    memcpy(c->setup, c->dev.setup, sizeof c->setup);
    c->meter_in = c->dev.meter_in;
    c->meter_out = c->dev.meter_out;
    c->clip = c->dev.clip;
    if (done)
        settle(c, done);
    int moved = 0;
    for (int i = 0; i < TP_NPARAMS; i++)
        if (vis_param(c, i) != old_p[i]) {
            c->changes.params |= 1ULL << i;
            moved = 1;
        }
    for (int i = 0; i < TP_NSETUP; i++)
        if (vis_setup(c, i) != old_s[i]) {
            c->changes.setup |= 1U << i;
            moved = 1;
        }
    c->revision += moved;
}

static void forget_queue(tp_core_t *c)
{
    c->nops = 0;
    for (int i = 0; i < TP_NPARAMS; i++)
        c->want_param[i] = -1;
    for (int i = 0; i < TP_NSETUP; i++)
        c->want_setup[i] = -1;
    pthread_cond_broadcast(&c->idle);
}

/* --- device I/O (worker, without the lock) ----------------------------------------------- */

static int on_msg(const uint8_t *p, void *ctx)
{
    tp_core_t *c = ctx;
    if (p[0] == TP_MSG_SETUP && p[1] == TP_SETUP_PRESET && p[2] == TP_EDIT_BUFFER)
        c->saw_edit = 1;
    else if (p[0] == TP_MSG_SETUP && p[1] == TP_SETUP_PRESET_SOURCE)
        c->saw_source = 1;
    else if (p[0] == TP_MSG_NAME && p[1] <= TP_EDIT_BUFFER) {   /* `03 idx arcade name` */
        pthread_mutex_lock(&c->lock);
        copy_str(c->names[p[1]].name, p + 3, TP_REPORT_LEN - 3, TP_NAME_LEN);
        c->names[p[1]].arcade = p[2];
        c->changes.flags |= TP_CHANGED_NAMES;
        c->revision++;
        pthread_mutex_unlock(&c->lock);
    }
    return 0;
}

static int is_echo(const uint8_t *p, void *ctx)
{
    return !memcmp(p, ctx, 3);
}

static int send_param(tp_core_t *c, int i, int v)
{
    tp_dev_t *d = &c->dev;
    if (d->params[i] == v)
        return 0;   /* the unit wouldn't answer */
    if (d->params[TP_P_PRESET_MODE] == TP_ARCADE && !tp_params[i].arcade)
        return 0;   /* it would drop it silently (a mode change queued before failed) */
    const uint8_t cmd[] = {TP_MSG_PARAM, i, v};
    if (!tp_exchange(d, cmd, sizeof cmd, is_echo, (void *)cmd))
        return 0;
    return errno == ETIMEDOUT ? tp_read_params(d) : -1;   /* refused after all: read back */
}

static int send_setup(tp_core_t *c, int i, int v)
{
    if (c->dev.setup[i] == v)
        return 0;
    const uint8_t cmd[] = {TP_MSG_SETUP, i, v};
    if (!tp_exchange(&c->dev, cmd, sizeof cmd, is_echo, (void *)cmd))
        return 0;
    return errno == ETIMEDOUT ? tp_read_setup(&c->dev) : -1;
}

static int send_preset(tp_core_t *c, int n)
{
    if (c->dev.setup[TP_S_SETUP_PRESET] == n)
        return 0;   /* already there and unedited: no answer */
    const uint8_t cmd[] = {TP_MSG_SETUP, TP_SETUP_PRESET, n};
    if (!tp_exchange(&c->dev, cmd, sizeof cmd, is_echo, (void *)cmd))
        return 0;
    if (errno != ETIMEDOUT)
        return -1;
    return tp_read_setup(&c->dev) < 0 || tp_read_params(&c->dev) < 0 ? -1 : 0;
}

static int find_mic(const tp_cab_t *cab, const char *name)
{
    for (int i = 0; i < 8; i++)
        if (!strcmp(cab->mics[i], name))
            return i;
    return -1;
}

static int send_cab(tp_core_t *c, int cab)   /* device.py select_cab */
{
    const int mics[2] = {TP_P_SIMUHP_MIC, TP_P_SIMUHP_MIC_2};
    char names[2][TP_MIC_SLOT + 1] = {"", ""};
    int old = c->dev.params[TP_P_SIMUHP_SPEAKER];
    int arcade = c->dev.params[TP_P_PRESET_MODE] == TP_ARCADE;
    for (int m = 0; m < 2; m++) {
        int mic = c->dev.params[mics[m]];
        if ((!arcade || tp_params[mics[m]].arcade) && old >= 0 && old < c->cabs.count && mic >= 0 && mic < 8)
            strcpy(names[m], c->cabs.items[old].mics[mic]);
    }
    if (send_param(c, TP_P_SIMUHP_SPEAKER, cab) < 0)
        return -1;
    for (int m = 0; m < 2; m++) {
        int mic = names[m][0] ? find_mic(&c->cabs.items[cab], names[m]) : -1;
        if (mic >= 0 && send_param(c, mics[m], mic) < 0)
            return -1;
    }
    return 0;
}

static int send_store(tp_core_t *c, int n);

static int run_op(tp_core_t *c, const op_t *op)
{
    switch (op->kind) {
    case OP_PARAM: return send_param(c, op->index, op->value);
    case OP_SETUP: return send_setup(c, op->index, op->value);
    case OP_PRESET: return send_preset(c, op->value);
    case OP_CAB: return send_cab(c, op->value);
    case OP_IR:
        if (send_param(c, FOLDERS[op->index], 0) < 0)
            return -1;
        return send_param(c, WAVES[op->index], op->value);
    case OP_STORE: return send_store(c, op->value);
    }
    return 0;
}

struct names_seen {
    int count;
    uint8_t seen[TP_PRESETS + 1];
};

static int all_names(const uint8_t *p, void *ctx)   /* on_msg stores them */
{
    struct names_seen *s = ctx;
    if (p[0] == TP_MSG_NAME && p[1] <= TP_EDIT_BUFFER && !s->seen[p[1]]) {
        s->seen[p[1]] = 1;
        s->count++;
    }
    return s->count == TP_PRESETS + 1;
}

static int read_names(tp_core_t *c)
{
    const uint8_t cmd[] = {TP_MSG_QUERY, TP_QUERY_NAMES, TP_NAMES_ALL};
    struct names_seen s = {0};
    return tp_exchange(&c->dev, cmd, sizeof cmd, all_names, &s);
}

static int is_stored(const uint8_t *p, void *ctx)   /* `03 n arcade name`, or `0c 03 n` (not if n was already loaded) */
{
    int n = *(int *)ctx;
    return (p[0] == TP_MSG_NAME && p[1] == n) || (p[0] == TP_MSG_SETUP && p[1] == TP_SETUP_PRESET && p[2] == n);
}

static int is_renamed(const uint8_t *p, void *ctx)   /* the echo: `03 80 arcade name` */
{
    const char *name = ctx;
    size_t n = strlen(name);
    return p[0] == TP_MSG_NAME && p[1] == TP_EDIT_BUFFER && !memcmp(p + 3, name, n) && !p[3 + n];
}

/* device.py rename_buffer: `03 80 name`, echoed; renaming it to the name it has gets no answer, so it isn't sent. */
static int rename_buffer(tp_core_t *c, const char *name)
{
    uint8_t cmd[2 + TP_NAME_MAX] = {TP_MSG_NAME, TP_EDIT_BUFFER};
    size_t n = strlen(name);
    pthread_mutex_lock(&c->lock);
    int same = !strcmp(c->names[TP_EDIT_BUFFER].name, name);
    pthread_mutex_unlock(&c->lock);
    if (same)
        return 0;
    memcpy(cmd + 2, name, n);
    return tp_exchange(&c->dev, cmd, 2 + n, is_renamed, (void *)name);
}

/* device.py save: the buffer renamed if asked (and only stored once the unit said so), then `04 73 n` stores it. Whether
 * it took is checked by reading the names back (the stored one must be the buffer's), also when the unit didn't
 * answer in time. */
static int send_store(tp_core_t *c, int n)
{
    tp_dev_t *d = &c->dev;
    char name[TP_NAME_MAX + 1];
    int sent = 0, r = 0, lost = 0;   /* lost: unplugged, the connection is dropped and the store counts as failed */
    pthread_mutex_lock(&c->lock);
    memcpy(name, c->store_name, sizeof name);
    pthread_mutex_unlock(&c->lock);
    if (name[0] && (r = rename_buffer(c, name)) < 0)
        lost = errno != ETIMEDOUT;
    if (!r) {
        const uint8_t cmd[] = {TP_MSG_QUERY, TP_QUERY_STORE, n};
        sent = 1;
        d->timeout_ms = TP_STORE_TIMEOUT_MS;
        if ((r = tp_exchange(d, cmd, sizeof cmd, is_stored, &n)) < 0)
            lost = errno != ETIMEDOUT;
        d->timeout_ms = TP_TIMEOUT_MS;
    }
    if (!lost) {
        r = tp_read_setup(d) < 0 || read_names(c) < 0 ? -1 : 0;
        lost = r < 0 && errno != ETIMEDOUT;
    }
    pthread_mutex_lock(&c->lock);
    int ok = sent && r == 0 && !strcmp(c->names[n].name, c->names[TP_EDIT_BUFFER].name)
             && (!name[0] || !strcmp(c->names[n].name, name));
    c->storing = -1;
    c->store_status = ok ? TP_STORE_OK : TP_STORE_FAILED;
    c->store_slot = n;
    c->store_seq++;
    c->revision++;
    pthread_mutex_unlock(&c->lock);
    return lost ? -1 : 0;
}

struct bank_read {
    bank_t *bank;
    uint8_t number;
};

/* `06 bank idx part ...` records (protocol.py parse_cab), until `04 10 bank`. */
static int bank_record(const uint8_t *p, void *ctx)
{
    struct bank_read *r = ctx;
    if (p[0] == TP_MSG_QUERY && p[1] == TP_BANK_END && p[2] == r->number)
        return 1;
    if (p[0] != TP_MSG_CAB || p[1] != r->number)
        return 0;
    tp_cab_t *cab = &r->bank->items[p[2]];   /* p[2] < TP_MAX_BANK */
    if (p[2] >= r->bank->count)
        r->bank->count = p[2] + 1;
    switch (p[3]) {
    case 0: copy_str(cab->name, p + 7, TP_REPORT_LEN - 7, sizeof cab->name); break;
    case 1:
        for (int m = 0; m < 3; m++)
            copy_str(cab->mics[m], p + 28 + m * TP_MIC_SLOT, TP_MIC_SLOT, sizeof cab->mics[m]);
        break;
    case 2:
        for (int m = 0; m < 5; m++)
            copy_str(cab->mics[3 + m], p + 4 + m * TP_MIC_SLOT, TP_MIC_SLOT, sizeof cab->mics[m]);
        break;
    case 3: copy_str(cab->maker, p + 4, 32, sizeof cab->maker); break;
    }
    return 0;
}

static int read_bank(tp_core_t *c, uint8_t number, bank_t *dest, unsigned flag)
{
    const uint8_t cmd[] = {TP_MSG_QUERY, TP_QUERY_BANK, number};
    struct bank_read r = {&c->stage, number};
    memset(&c->stage, 0, sizeof c->stage);
    if (tp_exchange(&c->dev, cmd, sizeof cmd, bank_record, &r) < 0)
        return -1;
    pthread_mutex_lock(&c->lock);
    memcpy(dest, &c->stage, sizeof *dest);
    c->changes.flags |= flag;
    c->revision++;
    pthread_mutex_unlock(&c->lock);
    return 0;
}

static int connect_dev(tp_core_t *c)
{
    char found[300], serial[9], firmware[8];
    const char *path = c->cfg.path;
    if (!path) {
        if (tp_hid_find(TP_SYSFS_HIDRAW, found, sizeof found) < 0)
            return -1;
        path = found;
    }
    if (tp_open(&c->dev, path) < 0)
        return -1;
    c->dev.on_msg = on_msg;
    c->dev.on_msg_ctx = c;
    if (tp_info(&c->dev, serial, firmware) || tp_read_setup(&c->dev) || tp_read_params(&c->dev)
        || read_names(c) || read_bank(c, 0, &c->cabs, TP_CHANGED_CABS)
        || read_bank(c, TP_IR_BANK, &c->irs, TP_CHANGED_IRS)) {   /* IR folder 0 */
        tp_close(&c->dev);
        return -1;
    }
    pthread_mutex_lock(&c->lock);
    memcpy(c->serial, serial, sizeof serial);
    memcpy(c->firmware, firmware, sizeof firmware);
    publish(c, NULL);
    c->connected = 1;
    c->generation++;
    c->changes.params = (1ULL << TP_NPARAMS) - 1;   /* everything is new */
    c->changes.setup = (1U << TP_NSETUP) - 1;
    c->changes.flags |= TP_CHANGED_CONNECTION;
    c->revision++;
    pthread_mutex_unlock(&c->lock);
    return 0;
}

static void *worker(void *arg)
{
    tp_core_t *c = arg;
    const uint8_t poll_cmd = TP_MSG_POLL;
    pthread_mutex_lock(&c->lock);
    while (!c->stop) {
        if (!c->connected) {
            pthread_mutex_unlock(&c->lock);
            int ok = !connect_dev(c);
            pthread_mutex_lock(&c->lock);
            if (!ok && !c->stop)
                wait_ms(c, &c->wake, c->cfg.retry_ms);
            continue;
        }
        if (!c->nops)
            wait_ms(c, &c->wake, c->cfg.poll_ms);
        if (c->stop)
            break;
        op_t op;
        int have = c->nops > 0;
        if (have) {
            op = c->ops[0];
            memmove(c->ops, c->ops + 1, --c->nops * sizeof *c->ops);
            c->busy = 1;
        }
        pthread_mutex_unlock(&c->lock);

        c->saw_edit = c->saw_source = 0;
        int r = have ? run_op(c, &op) : tp_exchange(&c->dev, &poll_cmd, 1, NULL, NULL);
        /* Back on the panel to an edit it kept ("A001*"): `0c 03 80` alone, so read the source. */
        if (!r && c->saw_edit && !c->saw_source)
            r = tp_read_setup(&c->dev);

        pthread_mutex_lock(&c->lock);
        publish(c, have ? &op : NULL);
        c->busy = 0;
        if (r < 0) {   /* unplugged (or unresponsive): start over */
            tp_close(&c->dev);
            if (c->storing >= 0) {   /* queued, or cut short before it could say */
                c->store_status = TP_STORE_FAILED;
                c->store_slot = c->storing;
                c->store_seq++;
                c->storing = -1;
            }
            c->connected = 0;
            c->changes.flags |= TP_CHANGED_CONNECTION;
            c->revision++;
            forget_queue(c);
        }
        if (!c->nops)
            pthread_cond_broadcast(&c->idle);
    }
    pthread_mutex_unlock(&c->lock);
    tp_close(&c->dev);
    return NULL;
}

/* --- API ----------------------------------------------------------------------------------- */

tp_core_t *tp_core_start(const tp_core_config_t *cfg)
{
    tp_core_t *c = calloc(1, sizeof *c);
    if (!c)
        return NULL;
    c->cfg = *cfg;
    if (c->cfg.poll_ms <= 0)
        c->cfg.poll_ms = 100;
    if (c->cfg.retry_ms <= 0)
        c->cfg.retry_ms = 1000;
    c->dev.fd = -1;
    c->storing = -1;
    forget_queue(c);
    pthread_condattr_t attr;
    pthread_condattr_init(&attr);
    pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
    pthread_mutex_init(&c->lock, NULL);
    pthread_cond_init(&c->wake, &attr);
    pthread_cond_init(&c->idle, &attr);
    pthread_condattr_destroy(&attr);
    int err = pthread_create(&c->thread, NULL, worker, c);
    if (err) {
        pthread_cond_destroy(&c->idle);
        pthread_cond_destroy(&c->wake);
        pthread_mutex_destroy(&c->lock);
        free(c);
        errno = err;
        return NULL;
    }
    return c;
}

void tp_core_stop(tp_core_t *c)
{
    if (!c)
        return;
    pthread_mutex_lock(&c->lock);
    c->stop = 1;
    pthread_cond_signal(&c->wake);
    pthread_mutex_unlock(&c->lock);
    pthread_join(c->thread, NULL);
    pthread_cond_destroy(&c->idle);
    pthread_cond_destroy(&c->wake);
    pthread_mutex_destroy(&c->lock);
    free(c);
}

void tp_core_state(tp_core_t *c, tp_state_t *s)
{
    pthread_mutex_lock(&c->lock);
    s->connected = c->connected;
    s->generation = c->generation;
    memcpy(s->serial, c->serial, sizeof s->serial);
    memcpy(s->firmware, c->firmware, sizeof s->firmware);
    for (int i = 0; i < TP_NPARAMS; i++)
        s->params[i] = vis_param(c, i);
    for (int i = 0; i < TP_NSETUP; i++)
        s->setup[i] = vis_setup(c, i);
    s->meter_in = c->meter_in;
    s->meter_out = c->meter_out;
    s->clip = c->clip;
    s->storing = c->storing;
    s->store_status = c->store_status;
    s->store_slot = c->store_slot;
    s->store_seq = c->store_seq;
    pthread_mutex_unlock(&c->lock);
}

unsigned tp_core_revision(tp_core_t *c)
{
    pthread_mutex_lock(&c->lock);
    unsigned r = c->revision;
    pthread_mutex_unlock(&c->lock);
    return r;
}

void tp_core_take_changes(tp_core_t *c, tp_changes_t *ch)
{
    pthread_mutex_lock(&c->lock);
    *ch = c->changes;
    memset(&c->changes, 0, sizeof c->changes);
    pthread_mutex_unlock(&c->lock);
}

/* Under the lock. Another value for the same thing replaces the queued one, at the end. */
static int enqueue(tp_core_t *c, int kind, int index, int value)
{
    for (int i = 0; i < c->nops; i++)
        if (c->ops[i].kind == kind && c->ops[i].index == index) {
            memmove(c->ops + i, c->ops + i + 1, (c->nops - i - 1) * sizeof *c->ops);
            c->nops--;
            break;
        }
    c->ops[c->nops++] = (op_t){kind, index, value};
    pthread_cond_signal(&c->wake);
    return 0;
}

static int param_hi(const tp_core_t *c, int i)   /* a bank index: the unit's bank size (protocol.py BANK_PARAMS) */
{
    if (i == TP_P_SIMUHP_SPEAKER)
        return c->cabs.count - 1;
    if (i == WAVES[0] || i == WAVES[1])
        return c->irs.count - 1;
    return tp_params[i].hi;
}

static int fail(tp_core_t *c, int err)
{
    pthread_mutex_unlock(&c->lock);
    errno = err;
    return -1;
}

static int arcade(const tp_core_t *c)
{
    return vis_param(c, TP_P_PRESET_MODE) == TP_ARCADE;
}

int tp_core_set_param(tp_core_t *c, int i, int v)
{
    pthread_mutex_lock(&c->lock);
    if (!c->connected)
        return fail(c, ENOTCONN);
    if (i < 0 || i >= TP_NPARAMS || !tp_params[i].writable || v < tp_params[i].lo || v > param_hi(c, i))
        return fail(c, EINVAL);
    if (arcade(c) && !tp_params[i].arcade)
        return fail(c, EPERM);
    c->want_param[i] = v;
    enqueue(c, OP_PARAM, i, v);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

int tp_core_set_setup(tp_core_t *c, int i, int v)
{
    pthread_mutex_lock(&c->lock);
    if (!c->connected)
        return fail(c, ENOTCONN);
    if (i < 0 || i >= TP_NSETUP || v < 0 || v > tp_setup_max[i])
        return fail(c, EINVAL);
    c->want_setup[i] = v;
    enqueue(c, OP_SETUP, i, v);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

int tp_core_select_preset(tp_core_t *c, int n)
{
    pthread_mutex_lock(&c->lock);
    if (!c->connected)
        return fail(c, ENOTCONN);
    if (n < 0 || n >= TP_PRESETS)
        return fail(c, EINVAL);
    c->want_setup[TP_S_SETUP_PRESET] = n;
    enqueue(c, OP_PRESET, 0, n);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

int tp_core_select_cab(tp_core_t *c, int cab)
{
    pthread_mutex_lock(&c->lock);
    if (!c->connected)
        return fail(c, ENOTCONN);
    if (cab < 0 || cab >= c->cabs.count)
        return fail(c, EINVAL);
    c->want_param[TP_P_SIMUHP_SPEAKER] = cab;
    enqueue(c, OP_CAB, 0, cab);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

int tp_core_select_ir(tp_core_t *c, int slot, int ir)
{
    pthread_mutex_lock(&c->lock);
    if (!c->connected)
        return fail(c, ENOTCONN);
    if (slot < 0 || slot > 1 || ir < 0 || ir >= c->irs.count)
        return fail(c, EINVAL);
    if (arcade(c))
        return fail(c, EPERM);
    c->want_param[WAVES[slot]] = ir;
    enqueue(c, OP_IR, slot, ir);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

static int valid_name(const char *s)   /* protocol.py check_name, without uppercasing it */
{
    size_t n = strlen(s);
    return n >= 1 && n <= TP_NAME_MAX && strspn(s, TP_NAME_CHARS) == n;
}

int tp_core_store(tp_core_t *c, int n, const char *name)
{
    pthread_mutex_lock(&c->lock);
    if (!c->connected)
        return fail(c, ENOTCONN);
    if (n < 0 || n >= TP_PRESETS || (name && name[0] && !valid_name(name)))
        return fail(c, EINVAL);
    if (c->storing >= 0)
        return fail(c, EBUSY);
    snprintf(c->store_name, sizeof c->store_name, "%s", name ? name : "");
    c->storing = n;   /* from now on until it ends: the screen says so */
    c->revision++;
    enqueue(c, OP_STORE, n, n);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

int tp_core_wait_idle(tp_core_t *c, int timeout_ms)
{
    struct timespec t;
    deadline_in(&t, timeout_ms);
    pthread_mutex_lock(&c->lock);
    while (c->nops || c->busy)
        if (pthread_cond_timedwait(&c->idle, &c->lock, &t) == ETIMEDOUT)
            return fail(c, ETIMEDOUT);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

static int list_count(tp_core_t *c, const bank_t *b)
{
    pthread_mutex_lock(&c->lock);
    int n = b->count;
    pthread_mutex_unlock(&c->lock);
    return n;
}

int tp_core_cab_count(tp_core_t *c)
{
    return list_count(c, &c->cabs);
}

int tp_core_ir_count(tp_core_t *c)
{
    return list_count(c, &c->irs);
}

int tp_core_cab(tp_core_t *c, int i, tp_cab_t *out)
{
    pthread_mutex_lock(&c->lock);
    if (i < 0 || i >= c->cabs.count)
        return fail(c, ERANGE);
    *out = c->cabs.items[i];
    pthread_mutex_unlock(&c->lock);
    return 0;
}

static void copy_out(char *buf, int len, const char *s)
{
    if (len > 0)
        snprintf(buf, len, "%s", s);
}

int tp_core_ir_name(tp_core_t *c, int i, char *buf, int len)
{
    pthread_mutex_lock(&c->lock);
    if (i < 0 || i >= c->irs.count)
        return fail(c, ERANGE);
    copy_out(buf, len, c->irs.items[i].name);
    pthread_mutex_unlock(&c->lock);
    return 0;
}

int tp_core_preset_name(tp_core_t *c, int n, char *buf, int len, int *arcade_out)
{
    pthread_mutex_lock(&c->lock);
    if (n < 0 || n > TP_EDIT_BUFFER)
        return fail(c, ERANGE);
    copy_out(buf, len, c->names[n].name);
    if (arcade_out)
        *arcade_out = c->names[n].arcade;
    pthread_mutex_unlock(&c->lock);
    return 0;
}
